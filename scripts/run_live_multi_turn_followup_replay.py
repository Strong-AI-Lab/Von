"""Run the multi-turn follow-up replay family on the real /von/generate path.

Each case from ``scripts/live_multi_turn_followup_prompt_bank.json`` runs its
turns sequentially in ONE chat session, so conversation-context behaviour
(referent reuse, obligation carry-forward suppression, answer grounding) is
exercised the way live users hit it. Per turn, the runner captures the visible
answer plus the persisted turn-execution evidence and evaluates the bank's
expectation keys; the oracle is generic and evidence-based — it never encodes
domain policy.

Motivating issues: JVNAUTOSCI-2563 (obligation carry-forward), JVNAUTOSCI-2564
(stale-referent answer grounding), JVNAUTOSCI-2553/2557 (degraded-success
ingestion). Programme: JVNAUTOSCI-1894 / JVNAUTOSCI-2532.

Usage:
    python scripts/run_live_multi_turn_followup_replay.py \
        --case arxiv_ingest_followup_family \
        --output-json artifacts/multi_turn_replay/arxiv_family.json

By default this targets the agent-test server convention (http://127.0.0.1:5010,
/health must report agent_test_instance=true). Use --base-url plus
--allow-non-agent-test-server only when intentionally driving the interactive
server.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.run_authenticated_browser_workflow_replay import (
    ReplayCase,
    _as_mapping,
    _request_json,
    _safe_text,
    apply_target_session_context,
    build_workflow_capability_preflight,
    collect_run_environment,
    create_replay_chat_session,
    establish_browser_test_session,
    extract_observed_workflow_ids,
    extract_selected_workflow_ids,
    extract_visible_answer,
    poll_replay_task,
    require_agent_test_server,
    submit_background_generate,
)

BANK_PATH = Path(__file__).with_name("live_multi_turn_followup_prompt_bank.json")
BANK_SCHEMA_VERSION = "live_multi_turn_followup_prompt_bank.v1"
REPORT_SCHEMA_VERSION = "live_multi_turn_followup_replay_report.v1"
DEFAULT_BASE_URL = "http://127.0.0.1:5010"
KNOWN_TURN_ROLES = (
    "task",
    "bare_followup",
    "different_target_followup",
    "explicit_continuation",
    "multi_target",
)
KNOWN_EXPECTATION_KEYS = frozenset(
    {
        "expected_workflow_id",
        "require_completed",
        "forbid_decisions",
        "require_prior_obligation_suppression",
        "forbid_unsatisfied_obligation_sources",
        "allow_obligation_carry_forward",
        "response_must_mention_any",
        "response_must_not_mention",
        "response_must_mention_all_groups",
        "notes",
        "assessment_scope",
        "response_distinct_match",
        "requires_manual_review",
    }
)
DEFAULT_FORBIDDEN_OBLIGATION_SOURCES = (
    "turn_expected_outcome_conditional_required_tools",
)


def load_bank(path: Path = BANK_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_bank(payload)
    return payload


def validate_bank(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != BANK_SCHEMA_VERSION:
        raise ValueError(
            f"Unexpected bank schema_version: {payload.get('schema_version')!r}"
        )
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Bank has no cases.")
    seen_ids: set[str] = set()
    for case in cases:
        case_id = _safe_text(case.get("case_id"))
        if not case_id:
            raise ValueError("Case missing case_id.")
        if case_id in seen_ids:
            raise ValueError(f"Duplicate case_id: {case_id}")
        seen_ids.add(case_id)
        turns = case.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"Case {case_id} has no turns.")
        for index, turn in enumerate(turns):
            role = _safe_text(turn.get("role"))
            if role not in KNOWN_TURN_ROLES:
                raise ValueError(
                    f"Case {case_id} turn {index} has unknown role {role!r}."
                )
            if not _safe_text(turn.get("prompt")):
                raise ValueError(f"Case {case_id} turn {index} has an empty prompt.")
            expectations = turn.get("expectations") or {}
            if expectations.get("assessment_scope", "workflow_integration") not in {
                "outcome",
                "workflow_integration",
            }:
                raise ValueError(
                    f"Case {case_id} turn {index}: invalid assessment_scope"
                )
            distinct_match = expectations.get("response_distinct_match")
            if distinct_match is not None:
                if (
                    not isinstance(distinct_match, Mapping)
                    or not isinstance(distinct_match.get("pattern"), str)
                    or not distinct_match["pattern"]
                    or type(distinct_match.get("minimum")) is not int
                    or distinct_match["minimum"] < 1
                ):
                    raise ValueError(
                        f"Case {case_id} turn {index}: invalid response_distinct_match"
                    )
                re.compile(distinct_match["pattern"])
            unknown = set(expectations) - KNOWN_EXPECTATION_KEYS
            if unknown:
                raise ValueError(
                    f"Case {case_id} turn {index} has unknown expectation keys: "
                    f"{sorted(unknown)}"
                )


# --------------------------------------------------------------------------
# Evidence extraction (generic; reads persisted turn-record surfaces)
# --------------------------------------------------------------------------


def _find_turn_execution_record(payload: Any, *, depth: int = 0) -> dict[str, Any]:
    if depth > 6 or not isinstance(payload, Mapping):
        return {}
    record = payload.get("turn_execution_record")
    if isinstance(record, Mapping) and (
        "decision" in record or "execution" in record or "required_effects" in record
    ):
        return dict(record)
    for key in ("result", "llm_debug", "llm_debug_data", "task_result"):
        found = _find_turn_execution_record(payload.get(key), depth=depth + 1)
        if found:
            return found
    return {}


def _turn_record_request_id(turn_record: Mapping[str, Any]) -> str | None:
    for path in (
        ("request_id",),
        ("turn_id",),
        ("execution", "request_id"),
        ("execution", "turn_id"),
        ("execution", "summary", "request_id"),
        ("execution", "summary", "turn_id"),
    ):
        cursor: Any = turn_record
        for key in path:
            if not isinstance(cursor, Mapping):
                cursor = None
                break
            cursor = cursor.get(key)
        text = _safe_text(cursor)
        if text:
            return text
    return None


def _turn_record_matches_request(
    turn_record: Mapping[str, Any],
    request_id: str | None,
    *,
    require_record_request_id: bool,
) -> bool:
    clean_request_id = _safe_text(request_id)
    if not clean_request_id:
        return True
    record_request_id = _turn_record_request_id(turn_record)
    if not record_request_id:
        return not require_record_request_id
    return record_request_id == clean_request_id


def _lookup_projected_turn_record(
    *,
    request_id: str,
    namespace: str | None,
    attempts: int = 3,
    sleep_seconds: float = 0.5,
) -> dict[str, Any]:
    clean_request_id = _safe_text(request_id)
    if not clean_request_id:
        return {}
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    except Exception:
        pass
    try:
        from src.backend.services.turn_execution_record_service import (
            get_turn_execution_record_projection,
        )
    except Exception:
        return {}

    namespace_candidates = [_safe_text(namespace) or None]
    if namespace_candidates[0] is not None:
        namespace_candidates.append(None)
    bounded_attempts = max(1, int(attempts or 1))
    for attempt_index in range(bounded_attempts):
        for namespace_candidate in namespace_candidates:
            try:
                record = get_turn_execution_record_projection(
                    request_id=clean_request_id,
                    namespace=namespace_candidate,
                )
            except Exception:
                record = {}
            if isinstance(record, Mapping) and record:
                return dict(record)
        if attempt_index + 1 < bounded_attempts and sleep_seconds > 0:
            time.sleep(max(0.0, float(sleep_seconds)))
    return {}


def fetch_turn_record(
    *,
    session: requests.Session,
    base_url: str,
    chat_session_id: str,
    request_id: str,
    namespace: str | None = None,
    task_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Locate the persisted turn execution record for the just-finished turn.

    Prefer the copy embedded in the task result; fall back to the newest
    assistant history debug entry for the session, then the canonical projected
    turn-execution record keyed by request id.
    """

    clean_request_id = _safe_text(request_id)
    record = _find_turn_execution_record(task_result)
    if record and _turn_record_matches_request(
        record,
        clean_request_id,
        require_record_request_id=False,
    ):
        return record
    try:
        history = _request_json(
            session,
            "GET",
            f"{base_url}/von/history",
            params={"session_id": chat_session_id, "tail_limit": 4},
        )
    except Exception:
        history = {}
    entries = history.get("history") or history.get("messages") or []
    for entry in reversed(list(entries) if isinstance(entries, list) else []):
        location = _as_mapping(entry.get("history_location"))
        history_index = location.get("history_index")
        if _safe_text(entry.get("role")) != "assistant" or history_index is None:
            continue
        try:
            debug_payload = _request_json(
                session,
                "GET",
                f"{base_url}/von/history/debug",
                params={
                    "session_id": chat_session_id,
                    "history_index": history_index,
                },
            )
        except Exception:
            continue
        record = _find_turn_execution_record(
            _as_mapping(debug_payload.get("llm_debug_data"))
        )
        if record and _turn_record_matches_request(
            record,
            clean_request_id,
            require_record_request_id=True,
        ):
            return record
    return _lookup_projected_turn_record(
        request_id=clean_request_id,
        namespace=namespace,
    )


_TURN_RECORD_VISIBLE_ANSWER_PATHS: tuple[tuple[str, ...], ...] = (
    ("response_surfaces", "user_visible_response", "text"),
    ("requested_evidence_lineage", "final_response", "text_checked"),
    ("requested_evidence_lineage", "final_response", "text"),
    ("requested_evidence_lineage", "final_response", "response_text"),
    ("requested_evidence_lineage", "final_response", "preview"),
    ("requested_evidence_lineage", "final_response", "text_checked_preview"),
    ("final_response", "text"),
    ("final_response", "response_text"),
    ("final_response", "preview"),
    ("final_response", "content"),
    ("final_answer_synthesis", "final_answer"),
    ("final_answer_synthesis", "response_text"),
    ("final_answer_synthesis", "text"),
    ("execution", "summary", "response_surfaces", "user_visible_response", "text"),
    (
        "execution",
        "summary",
        "requested_evidence_lineage",
        "final_response",
        "text_checked",
    ),
    (
        "execution",
        "summary",
        "requested_evidence_lineage",
        "final_response",
        "text_checked_preview",
    ),
)


def _turn_record_text_at_path(
    payload: Mapping[str, Any],
    path: Sequence[str],
) -> str | None:
    cursor: Any = payload
    for key in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(key)
    text = _safe_text(cursor)
    return text or None


def extract_visible_answer_from_turn_record(
    turn_record: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Extract final user-visible text from persisted turn-record surfaces.

    The background task result can expose a selected-workflow snapshot that is
    older than the final answer synthesis. The turn record's response surfaces
    and requested-evidence lineage are closer to what the answer gate actually
    checked, so the replay oracle should prefer them when available.
    """

    for path in _TURN_RECORD_VISIBLE_ANSWER_PATHS:
        text = _turn_record_text_at_path(turn_record, path)
        if text:
            return text, ".".join(path)
    return None, None


def _derive_target_namespace(
    *,
    target_session_context: Mapping[str, Any],
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> str | None:
    session_context = _as_mapping(target_session_context.get("session_context"))
    user_id = _safe_text(session_context.get("user_id")) or _safe_text(user_concept_id)
    org_id = _safe_text(session_context.get("organisation_id")) or _safe_text(
        organisation_concept_id
    )
    if not user_id:
        return None
    try:
        from src.backend.services.namespace_service import derive_namespace_for_actor

        return derive_namespace_for_actor(user_id, org_id)
    except Exception:
        return None


def _unsatisfied_obligations(turn_record: Mapping[str, Any]) -> list[dict[str, Any]]:
    obligations: list[dict[str, Any]] = []
    for effect in turn_record.get("required_effects") or []:
        if not isinstance(effect, Mapping):
            continue
        ledger = _as_mapping(effect.get("required_tool_obligations"))
        for obligation in ledger.get("obligations") or []:
            if isinstance(obligation, Mapping) and obligation.get("satisfied") is False:
                obligations.append(dict(obligation))
    return obligations


def _obligation_sources(obligation: Mapping[str, Any]) -> list[str]:
    sources = [
        _safe_text(source)
        for source in (obligation.get("sources") or [])
        if _safe_text(source)
    ]
    single = _safe_text(obligation.get("source"))
    if single and single not in sources:
        sources.append(single)
    return sources


def _carry_forward_projection(turn_record: Mapping[str, Any]) -> dict[str, Any]:
    summary = _as_mapping(_as_mapping(turn_record.get("execution")).get("summary"))
    return _as_mapping(summary.get("expected_outcome_obligation_carry_forward"))


def _has_obligation_ledger(turn_record: Mapping[str, Any]) -> bool:
    """An absent retired ledger is not evidence of an empty obligation set."""
    return any(
        isinstance(effect, Mapping)
        and isinstance(effect.get("required_tool_obligations"), Mapping)
        and isinstance(effect["required_tool_obligations"].get("obligations"), list)
        for effect in turn_record.get("required_effects") or []
    )


def _selected_workflow_execution_summary(
    turn_record: Mapping[str, Any],
) -> dict[str, Any]:
    execution = _as_mapping(turn_record.get("execution"))
    selected_trace = _as_mapping(execution.get("selected_workflow_trace"))
    return _as_mapping(selected_trace.get("workflow_execution_summary"))


# --------------------------------------------------------------------------
# Oracle (pure; unit-tested)
# --------------------------------------------------------------------------


def evaluate_turn_expectations(
    *,
    expectations: Mapping[str, Any],
    visible_answer: str | None,
    terminal_status: str | None,
    selected_workflow_ids: Sequence[str],
    turn_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one turn's evidence against its bank expectations.

    Returns verdict, checks and separate integration_checks. Every check records
    what was expected, observed and whether it held. Unavailable evidence and
    outstanding source-grounded review are inconclusive, not silent passes.
    """

    checks: list[dict[str, Any]] = []
    answer = (visible_answer or "").lower()

    def add(name: str, ok: bool | None, observed: Any, expected: Any) -> None:
        checks.append(
            {
                "check": name,
                "outcome": "pass" if ok else ("inconclusive" if ok is None else "fail"),
                "expected": expected,
                "observed": observed,
            }
        )

    # Built-in invariant, independent of bank expectations: the visible answer
    # must be user-facing prose, never a raw payload envelope. Truncated
    # payloads (e.g. a preview cut mid-JSON) still start like JSON, so the
    # structural prefix counts even when json.loads fails.
    raw_answer_text = (visible_answer or "").strip()
    if raw_answer_text:
        looks_like_raw_payload = False
        if raw_answer_text.startswith(("{", "[")):
            try:
                parsed_answer = json.loads(raw_answer_text)
            except ValueError:
                parsed_answer = None
            looks_like_raw_payload = isinstance(parsed_answer, (dict, list)) or (
                raw_answer_text[:80]
                .replace(" ", "")
                .replace("\n", "")
                .startswith(('{"', "[{", "['", '["'))
            )
        if looks_like_raw_payload:
            add(
                "visible_answer_not_raw_payload",
                False,
                raw_answer_text[:160],
                "user-facing prose, not a JSON/tool payload",
            )

    if expectations.get("require_completed"):
        status = (_safe_text(terminal_status) or "").lower()
        add("require_completed", status == "completed", status, "completed")
        add("answer_available", bool(raw_answer_text), bool(raw_answer_text), True)

    # A background task can reach its transport-level ``completed`` state after
    # Von has safely returned a grounded non-success.  That is not capability
    # success.  Whenever the persisted completion gate is present, require its
    # own canonical acceptance signal so a timeout or represented workflow
    # failure cannot be reported green merely because polling terminated.
    completion_gate = _as_mapping(turn_record.get("completion_gate"))
    if completion_gate:
        safe_to_claim_completion = completion_gate.get("safe_to_claim_completion")
        add(
            "completion_gate_safe_to_claim_completion",
            safe_to_claim_completion is True,
            {
                "decision": completion_gate.get("decision"),
                "decision_reason": completion_gate.get("decision_reason"),
                "safe_to_claim_completion": safe_to_claim_completion,
                "requires_follow_up": completion_gate.get("requires_follow_up"),
                "blocking_failure_codes": list(
                    completion_gate.get("blocking_failure_codes") or []
                ),
            },
            "persisted completion gate safe_to_claim_completion=true",
        )

    # Preserve the stricter historical workflow-integration observation. Outcome
    # cases separate this diagnostic below: a recovered action failure is not
    # itself evidence that the requested end state failed.
    selected_execution_summary = _selected_workflow_execution_summary(turn_record)
    if selected_execution_summary:
        failed_action_ids = [
            _safe_text(action_id)
            for action_id in (selected_execution_summary.get("failed_action_ids") or [])
            if _safe_text(action_id)
        ]
        raw_failure_count = selected_execution_summary.get("action_failure_count")
        action_failure_count = (
            raw_failure_count if isinstance(raw_failure_count, int) else None
        )
        selected_workflow_clean = not failed_action_ids and action_failure_count in {
            None,
            0,
        }
        add(
            "selected_workflow_has_no_failed_actions",
            selected_workflow_clean,
            {
                "workflow_id": selected_execution_summary.get("workflow_id"),
                "action_failure_count": action_failure_count,
                "failed_action_ids": failed_action_ids,
                "first_failing_state_id": selected_execution_summary.get(
                    "first_failing_state_id"
                ),
                "first_failing_action_id": selected_execution_summary.get(
                    "first_failing_action_id"
                ),
            },
            "selected workflow execution has zero failed actions",
        )

    expected_workflow = _safe_text(expectations.get("expected_workflow_id"))
    if expected_workflow:
        add(
            "expected_workflow_id",
            expected_workflow in set(selected_workflow_ids),
            list(selected_workflow_ids),
            expected_workflow,
        )

    forbid_decisions = [
        _safe_text(item).lower()
        for item in (expectations.get("forbid_decisions") or [])
        if _safe_text(item)
    ]
    if forbid_decisions:
        decision = (_safe_text(turn_record.get("decision")) or "").lower()
        if not decision:
            add("forbid_decisions", None, "decision unavailable", forbid_decisions)
        else:
            add(
                "forbid_decisions",
                decision not in forbid_decisions,
                decision,
                f"not in {forbid_decisions}",
            )

    forbidden_sources = tuple(
        _safe_text(item)
        for item in (
            expectations.get("forbid_unsatisfied_obligation_sources")
            or (
                DEFAULT_FORBIDDEN_OBLIGATION_SOURCES
                if expectations.get("require_prior_obligation_suppression")
                else ()
            )
        )
        if _safe_text(item)
    )
    if forbidden_sources:
        if not _has_obligation_ledger(turn_record):
            add(
                "forbid_unsatisfied_obligation_sources",
                None,
                "obligation ledger unavailable",
                list(forbidden_sources),
            )
        else:
            offending = [
                {
                    "tool_name": _safe_text(obligation.get("tool_name")),
                    "sources": _obligation_sources(obligation),
                }
                for obligation in _unsatisfied_obligations(turn_record)
                if any(
                    source in forbidden_sources
                    for source in _obligation_sources(obligation)
                )
            ]
            add(
                "forbid_unsatisfied_obligation_sources",
                not offending,
                offending,
                f"no unsatisfied obligation sourced from {list(forbidden_sources)}",
            )

    if expectations.get("require_prior_obligation_suppression"):
        if not _has_obligation_ledger(turn_record) and not _carry_forward_projection(
            turn_record
        ).get("suppressed_prior_obligations"):
            add(
                "require_prior_obligation_suppression",
                None,
                "suppression and obligation ledger unavailable",
                "suppression recorded or no inherited unsatisfied obligations",
            )
        else:
            projection = _carry_forward_projection(turn_record)
            suppressed = list(projection.get("suppressed_prior_obligations") or [])
            inherited_unsatisfied = [
                obligation
                for obligation in _unsatisfied_obligations(turn_record)
                if any(
                    source in DEFAULT_FORBIDDEN_OBLIGATION_SOURCES
                    for source in _obligation_sources(obligation)
                )
            ]
            add(
                "require_prior_obligation_suppression",
                not inherited_unsatisfied,
                {
                    "suppressed_prior_obligations": suppressed,
                    "inherited_unsatisfied_count": len(inherited_unsatisfied),
                },
                "suppression telemetry present or no inherited unsatisfied obligations",
            )

    mention_any = [
        _safe_text(item)
        for item in (expectations.get("response_must_mention_any") or [])
        if _safe_text(item)
    ]
    if mention_any:
        hit = next((item for item in mention_any if item.lower() in answer), None)
        add("response_must_mention_any", hit is not None, hit, mention_any)

    must_not = [
        _safe_text(item)
        for item in (expectations.get("response_must_not_mention") or [])
        if _safe_text(item)
    ]
    if must_not:
        leaked = [item for item in must_not if item.lower() in answer]
        add("response_must_not_mention", not leaked, leaked, f"none of {must_not}")

    groups = expectations.get("response_must_mention_all_groups") or []
    if groups:
        group_results = []
        all_ok = True
        for group in groups:
            tokens = [_safe_text(item) for item in group if _safe_text(item)]
            hit = next((token for token in tokens if token.lower() in answer), None)
            group_results.append({"group": tokens, "hit": hit})
            if hit is None:
                all_ok = False
        add("response_must_mention_all_groups", all_ok, group_results, groups)

    distinct_match = expectations.get("response_distinct_match")
    if distinct_match:
        matches = sorted(
            {
                match.group(0).casefold()
                for match in re.finditer(
                    distinct_match["pattern"], raw_answer_text, re.IGNORECASE
                )
            }
        )
        add(
            "response_distinct_match",
            len(matches) >= distinct_match["minimum"],
            matches,
            distinct_match,
        )

    if expectations.get("requires_manual_review"):
        add(
            "manual_outcome_review",
            None,
            "not assessed by this runner",
            expectations["requires_manual_review"],
        )

    # An outcome case may succeed using another route or recover after a failed
    # action. Preserve those integration observations without making them the
    # outcome oracle. Existing integration callers retain their old contract.
    integration_checks = []
    if expectations.get("assessment_scope") == "outcome":
        integration_names = {
            "expected_workflow_id",
            "selected_workflow_has_no_failed_actions",
        }
        integration_checks = [
            check for check in checks if check["check"] in integration_names
        ]
        checks = [check for check in checks if check["check"] not in integration_names]

    outcomes = {check["outcome"] for check in checks}
    verdict = (
        "fail"
        if "fail" in outcomes
        else ("inconclusive" if "inconclusive" in outcomes else "pass")
    )
    return {
        "verdict": verdict,
        "checks": checks,
        "integration_checks": integration_checks,
    }


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def run_case(
    *,
    case: Mapping[str, Any],
    base_url: str,
    model: str | None,
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    cancel_on_observer_expiry: bool = False,
) -> dict[str, Any]:
    case_id = _safe_text(case.get("case_id")) or "unknown_case"
    session = requests.Session()
    window_session_id = f"multi-turn-replay-{uuid.uuid4()}"
    auth_login = establish_browser_test_session(
        session=session,
        base_url=base_url,
        window_session_id=window_session_id,
        timeout_seconds=45.0,
    )
    target_session_context = apply_target_session_context(
        session=session,
        base_url=base_url,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    target_namespace = _derive_target_namespace(
        target_session_context=target_session_context,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    chat_session = create_replay_chat_session(
        session=session,
        base_url=base_url,
        case_id=f"multi-turn {case_id}",
    )
    chat_session_id = _safe_text(chat_session.get("session_id"))

    turn_reports: list[dict[str, Any]] = []
    for turn_index, turn in enumerate(case.get("turns") or []):
        prompt = _safe_text(turn.get("prompt"))
        role = _safe_text(turn.get("role"))
        expectations = _as_mapping(turn.get("expectations"))
        client_request_id = f"multi-turn-{case_id}-{turn_index}-{uuid.uuid4()}"
        replay_case = ReplayCase(
            case_id=f"{case_id}#{turn_index}:{role}",
            prompt=prompt,
            expected_workflow_id=_safe_text(expectations.get("expected_workflow_id"))
            or None,
            expected_progress_fact_ids=(),
            expected_contract_ids=(),
        )
        submission = submit_background_generate(
            session=session,
            base_url=base_url,
            case=replay_case,
            client_request_id=client_request_id,
            conversation_session_id=chat_session_id,
            gmail_profile=None,
            model=model,
            presenter_mode=False,
            thinking_card_mode="off",
        )
        task_id = _safe_text(submission.get("task_id")) or client_request_id
        evidence = poll_replay_task(
            session=session,
            base_url=base_url,
            task_id=task_id,
            request_id=client_request_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            cancel_on_timeout=cancel_on_observer_expiry,
        )
        task_result = _as_mapping(evidence.get("task_result"))
        last_task_status = _as_mapping(evidence.get("last_task_status"))
        terminal_status = _safe_text(last_task_status.get("status"))
        turn_record = fetch_turn_record(
            session=session,
            base_url=base_url,
            chat_session_id=chat_session_id,
            request_id=client_request_id,
            namespace=target_namespace,
            task_result=task_result,
        )
        turn_record_visible_answer, turn_record_visible_answer_source = (
            extract_visible_answer_from_turn_record(turn_record)
        )
        task_result_visible_answer = extract_visible_answer(task_result)
        visible_answer = turn_record_visible_answer or task_result_visible_answer
        visible_answer_source = (
            f"turn_record.{turn_record_visible_answer_source}"
            if turn_record_visible_answer_source
            else ("task_result" if task_result_visible_answer else None)
        )
        selected_workflow_ids = extract_selected_workflow_ids(
            task_result,
            turn_record,
            evidence.get("last_progress"),
        )
        observed_workflow_ids = extract_observed_workflow_ids(
            task_result,
            turn_record,
            evidence.get("last_progress"),
        )
        workflow_evidence_ids = list(
            dict.fromkeys([*selected_workflow_ids, *observed_workflow_ids])
        )
        observation_pending = bool(evidence.get("observation_pending")) and (
            terminal_status not in {"completed", "failed", "cancelled"}
        )
        if observation_pending:
            evaluation = {
                "verdict": "inconclusive",
                "checks": [
                    {
                        "check": "server_task_terminal_state_observed",
                        "outcome": "inconclusive",
                        "expected": "terminal task evidence",
                        "observed": terminal_status or "pending",
                    }
                ],
            }
        else:
            evaluation = evaluate_turn_expectations(
                expectations=expectations,
                visible_answer=visible_answer,
                terminal_status=terminal_status,
                selected_workflow_ids=workflow_evidence_ids,
                turn_record=turn_record,
            )
        turn_reports.append(
            {
                "turn_index": turn_index,
                "role": role,
                "prompt": prompt,
                "client_request_id": client_request_id,
                "task_id": task_id,
                "observation_pending": observation_pending,
                "reconciliation": dict(_as_mapping(evidence.get("reconciliation"))),
                "terminal_status": terminal_status,
                "visible_answer": visible_answer,
                "visible_answer_source": visible_answer_source,
                "answer_evidence_surface": "server_record_or_task_result",
                "browser_delivery": "not_observed",
                "selected_workflow_ids": selected_workflow_ids,
                "observed_workflow_ids": observed_workflow_ids,
                "workflow_evidence_ids": workflow_evidence_ids,
                "turn_record_decision": _safe_text(turn_record.get("decision")) or None,
                "turn_record_available": bool(turn_record),
                "obligation_carry_forward": _carry_forward_projection(turn_record),
                "evaluation": evaluation,
            }
        )
        if observation_pending:
            # Later turns depend on this turn's completed conversation state.
            # Leave the task running and preserve the exact reconciliation
            # identifiers instead of submitting a competing follow-up.
            break
        if evaluation["verdict"] == "fail" and role == "task":
            # Later turns depend on the task turn's referents; keep the partial
            # evidence but stop driving a session whose premise failed.
            break
        time.sleep(1.0)

    verdicts = [report["evaluation"]["verdict"] for report in turn_reports]
    overall = (
        "fail"
        if "fail" in verdicts
        else ("inconclusive" if "inconclusive" in verdicts else "pass")
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "case_id": case_id,
        "domain": _safe_text(case.get("domain")) or None,
        "motivating_issues": list(case.get("motivating_issues") or []),
        "base_url": base_url,
        "window_session_id": window_session_id,
        "chat_session_id": chat_session_id,
        "auth_login_success": bool(auth_login.get("success")),
        "target_session_context_ready": bool(
            target_session_context.get("target_session_context_ready", True)
        ),
        "run_environment": collect_run_environment(
            session=session,
            base_url=base_url,
        ),
        "turn_count": len(turn_reports),
        "turns": turn_reports,
        "overall_verdict": overall,
        "claim_scope": "bank expectations over server evidence; not browser delivery or reliability certification",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Case id(s) to run; default runs every case in the bank.",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1500.0,
        help=(
            "Per-turn observation window. A still-live turn is reported "
            "inconclusive and is not cancelled by default."
        ),
    )
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--user-concept-id", default=None)
    parser.add_argument("--organisation-concept-id", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument(
        "--cancel-on-observer-expiry",
        action="store_true",
        help=(
            "Explicitly cancel a still-live server task when the replay's "
            "observation window elapses. The default is pending/inconclusive."
        ),
    )
    parser.add_argument(
        "--allow-non-agent-test-server",
        action="store_true",
        help="Permit a base URL whose /health does not report agent_test_instance.",
    )
    parser.add_argument(
        "--run-despite-workflow-capability-preflight-blocker",
        action="store_true",
        help=(
            "Diagnostic override: submit turns even though the workflow "
            "capability index preflight reports discovery unavailable."
        ),
    )
    args = parser.parse_args(argv)

    preflight_session = requests.Session()
    environment = collect_run_environment(
        session=preflight_session,
        base_url=args.base_url,
    )
    require_agent_test_server(
        environment=environment,
        base_url=args.base_url,
        allow_non_agent_test_server=args.allow_non_agent_test_server,
    )
    workflow_capability_preflight = build_workflow_capability_preflight(
        session=preflight_session,
        base_url=args.base_url,
    )
    if workflow_capability_preflight.get("workflow_discovery_available") is not True:
        preflight_pending = bool(
            workflow_capability_preflight.get("observation_pending")
        )
        blocker_payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "verdict": "inconclusive" if preflight_pending else "blocked",
            "blocker_type": (
                "workflow_capability_index_pending"
                if preflight_pending
                else "workflow_capability_index_blocker"
            ),
            "workflow_capability_preflight": workflow_capability_preflight,
        }
        print(
            "Workflow capability index preflight reports discovery unavailable; "
            "a replay submitted now would test operational collapse, not "
            "conversation behaviour."
        )
        if not args.run_despite_workflow_capability_preflight_blocker:
            if args.output_json:
                blocker_path = Path(args.output_json)
                blocker_path.parent.mkdir(parents=True, exist_ok=True)
                blocker_path.write_text(
                    json.dumps(blocker_payload, indent=2, default=str),
                    encoding="utf-8",
                )
                print(f"Blocker report written to {blocker_path}")
            return 0 if preflight_pending else 2
        print(
            "Continuing anyway because "
            "--run-despite-workflow-capability-preflight-blocker was passed."
        )

    bank = load_bank()
    cases = list(bank.get("cases") or [])
    if args.cases:
        wanted = set(args.cases)
        cases = [case for case in cases if _safe_text(case.get("case_id")) in wanted]
        missing = wanted - {_safe_text(case.get("case_id")) for case in cases}
        if missing:
            raise SystemExit(f"Unknown case id(s): {sorted(missing)}")

    reports = []
    for case in cases:
        report = run_case(
            case=case,
            base_url=args.base_url,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            user_concept_id=args.user_concept_id,
            organisation_concept_id=args.organisation_concept_id,
            cancel_on_observer_expiry=bool(args.cancel_on_observer_expiry),
        )
        reports.append(report)
        print(
            f"[{report['case_id']}] overall={report['overall_verdict']} "
            f"turns={report['turn_count']} session={report['chat_session_id']}"
        )
        for turn in report["turns"]:
            print(
                f"  - turn {turn['turn_index']} ({turn['role']}): "
                f"{turn['evaluation']['verdict']} status={turn['terminal_status']}"
            )

    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "bank_schema_version": bank.get("schema_version"),
        "workflow_capability_preflight": workflow_capability_preflight,
        "case_reports": reports,
    }
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Report written to {output_path}")

    return (
        0
        if all(r["overall_verdict"] in {"pass", "inconclusive"} for r in reports)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
