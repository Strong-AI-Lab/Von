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
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.run_authenticated_browser_workflow_replay import (
    ReplayCase,
    apply_target_session_context,
    collect_run_environment,
    create_replay_chat_session,
    establish_browser_test_session,
    extract_selected_workflow_ids,
    extract_visible_answer,
    poll_replay_task,
    require_agent_test_server,
    submit_background_generate,
    _as_mapping,
    _request_json,
    _safe_text,
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
                raise ValueError(f"Case {case_id} turn {index} has unknown role {role!r}.")
            if not _safe_text(turn.get("prompt")):
                raise ValueError(f"Case {case_id} turn {index} has an empty prompt.")
            expectations = turn.get("expectations") or {}
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


def fetch_turn_record(
    *,
    session: requests.Session,
    base_url: str,
    chat_session_id: str,
    task_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Locate the persisted turn execution record for the just-finished turn.

    Prefer the copy embedded in the task result; fall back to the newest
    assistant history debug entry for the session.
    """

    record = _find_turn_execution_record(task_result)
    if record:
        return record
    try:
        history = _request_json(
            session,
            "GET",
            f"{base_url}/von/history",
            params={"session_id": chat_session_id, "tail_limit": 4},
        )
    except Exception:
        return {}
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
        if record:
            return record
    return {}


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

    Returns {verdict: pass|fail|inconclusive, checks: [...]} where every check
    records what was expected, what was observed, and whether it held. Checks
    that need evidence the run could not capture are inconclusive, not silent.
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

    if expectations.get("require_completed"):
        status = (_safe_text(terminal_status) or "").lower()
        add("require_completed", status == "completed", status, "completed")

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
        if not turn_record:
            add("forbid_decisions", None, "turn record unavailable", forbid_decisions)
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
        if not turn_record:
            add(
                "forbid_unsatisfied_obligation_sources",
                None,
                "turn record unavailable",
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
        if not turn_record:
            add(
                "require_prior_obligation_suppression",
                None,
                "turn record unavailable",
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
                bool(suppressed) or not inherited_unsatisfied,
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

    outcomes = {check["outcome"] for check in checks}
    verdict = (
        "fail"
        if "fail" in outcomes
        else ("inconclusive" if "inconclusive" in outcomes else "pass")
    )
    return {"verdict": verdict, "checks": checks}


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
            cancel_on_timeout=True,
        )
        task_result = _as_mapping(evidence.get("task_result"))
        last_task_status = _as_mapping(evidence.get("last_task_status"))
        terminal_status = _safe_text(last_task_status.get("status"))
        visible_answer = extract_visible_answer(task_result)
        turn_record = fetch_turn_record(
            session=session,
            base_url=base_url,
            chat_session_id=chat_session_id,
            task_result=task_result,
        )
        selected_workflow_ids = extract_selected_workflow_ids(
            task_result,
            turn_record,
            evidence.get("last_progress"),
        )
        evaluation = evaluate_turn_expectations(
            expectations=expectations,
            visible_answer=visible_answer,
            terminal_status=terminal_status,
            selected_workflow_ids=selected_workflow_ids,
            turn_record=turn_record,
        )
        turn_reports.append(
            {
                "turn_index": turn_index,
                "role": role,
                "prompt": prompt,
                "client_request_id": client_request_id,
                "task_id": task_id,
                "terminal_status": terminal_status,
                "visible_answer": visible_answer,
                "selected_workflow_ids": selected_workflow_ids,
                "turn_record_decision": _safe_text(turn_record.get("decision")) or None,
                "turn_record_available": bool(turn_record),
                "obligation_carry_forward": _carry_forward_projection(turn_record),
                "evaluation": evaluation,
            }
        )
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
    parser.add_argument("--timeout-seconds", type=float, default=1500.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--user-concept-id", default=None)
    parser.add_argument("--organisation-concept-id", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument(
        "--allow-non-agent-test-server",
        action="store_true",
        help="Permit a base URL whose /health does not report agent_test_instance.",
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

    return 0 if all(r["overall_verdict"] == "pass" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
