"""Response-surface reconciliation support for turn telemetry.

The helpers in this module compare deterministic text fingerprints across the
surfaces that may describe a turn's answer.  They deliberately do not decide
whether an answer is semantically correct; replay and critic workflows own that
policy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

TURN_RESPONSE_SURFACES_SCHEMA_VERSION = "turn_response_surfaces.v1"
_DEFAULT_TEXT_LIMIT = 4000
_NON_SUCCESS_GATE_DECISIONS = frozenset(
    {
        "blocked",
        "error",
        "failed",
        "failure",
        "follow_up_required",
        "incomplete",
        "needs_replay",
        "partial",
        "retrying",
    }
)
_PASS_CRITIC_VERDICTS = frozenset({"pass", "passed", "success", "succeeded"})


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _bounded_text(value: Any, *, limit: int) -> dict[str, Any] | None:
    text = _safe_str(value)
    if text is None:
        return None
    bounded_limit = max(0, int(limit))
    return {
        "text": text[:bounded_limit],
        "char_count": len(text),
        "sha256": _hash_text(text),
        "truncated": len(text) > bounded_limit,
    }


def _first_path(payload: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        cursor: Any = payload
        found = True
        for key in path:
            if not isinstance(cursor, Mapping):
                found = False
                break
            cursor = cursor.get(key)
        if found and cursor not in (None, ""):
            return cursor
    return None


def _text_surface(
    *,
    kind: str,
    source: str,
    text: Any,
    limit: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    bounded = _bounded_text(text, limit=limit)
    if bounded is None:
        return None
    surface = {
        "kind": kind,
        "source": source,
        "text_available": True,
        **bounded,
    }
    if isinstance(extra, Mapping):
        surface.update({str(key): value for key, value in extra.items()})
    return surface


def _hash_surface(
    *,
    kind: str,
    source: str,
    sha256: Any,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    digest = _safe_str(sha256)
    if digest is None:
        return None
    surface = {
        "kind": kind,
        "source": source,
        "text_available": False,
        "sha256": digest.lower(),
    }
    if isinstance(extra, Mapping):
        surface.update({str(key): value for key, value in extra.items()})
    return surface


def _surface_from_payload(
    *,
    kind: str,
    source: str,
    payload: Mapping[str, Any],
    limit: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    text = _first_path(
        payload,
        ("text",),
        ("response_text",),
        ("preview",),
        ("content",),
    )
    surface = _text_surface(
        kind=kind,
        source=source,
        text=text,
        limit=limit,
        extra=extra,
    )
    if surface is not None:
        return surface
    return _hash_surface(
        kind=kind,
        source=source,
        sha256=_first_path(payload, ("response_sha256",), ("sha256",)),
        extra=extra,
    )


def _user_visible_surface(
    *,
    target_message: Mapping[str, Any],
    limit: int,
) -> dict[str, Any] | None:
    history_location = _mapping(target_message.get("history_location"))
    return _text_surface(
        kind="user_visible_response",
        source="chat_history.target_message.content",
        text=target_message.get("content"),
        limit=limit,
        extra={"history_location": history_location or None},
    )


def _recorded_final_response_surface(
    *,
    turn_record: Mapping[str, Any],
    limit: int,
) -> dict[str, Any] | None:
    final_response = _mapping(turn_record.get("final_response"))
    if not final_response:
        return None
    return _surface_from_payload(
        kind="recorded_final_response",
        source="turn_execution_record.final_response",
        payload=final_response,
        limit=limit,
    )


def _selected_workflow_response_surface(
    *,
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
    limit: int,
) -> dict[str, Any] | None:
    candidates: tuple[tuple[Mapping[str, Any], tuple[str, ...], str], ...] = (
        (
            turn_record,
            ("completion_report", "response_text"),
            "turn_execution_record.completion_report.response_text",
        ),
        (
            turn_record,
            ("execution", "summary", "custom_workflow_execution", "response_text"),
            "turn_execution_record.execution.summary.custom_workflow_execution.response_text",
        ),
        (
            turn_record,
            (
                "workflow_routing_diagnostics",
                "dispatch",
                "custom_workflow_execution",
                "response_text",
            ),
            "turn_execution_record.workflow_routing_diagnostics.dispatch.custom_workflow_execution.response_text",
        ),
        (
            diagnostics,
            ("completion_report", "response_text"),
            "turn_execution_diagnostics.completion_report.response_text",
        ),
        (
            diagnostics,
            (
                "workflow_routing_diagnostics",
                "dispatch",
                "custom_workflow_execution",
                "response_text",
            ),
            "turn_execution_diagnostics.workflow_routing_diagnostics.dispatch.custom_workflow_execution.response_text",
        ),
    )
    for payload, path, source in candidates:
        text = _first_path(payload, path)
        surface = _text_surface(
            kind="selected_workflow_response",
            source=source,
            text=text,
            limit=limit,
        )
        if surface is not None:
            return surface
    return None


def _critic_payload(
    *,
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
    limit: int,
) -> dict[str, Any] | None:
    critic = _mapping(turn_record.get("critic")) or _mapping(diagnostics.get("critic"))
    verdict = _mapping(critic.get("verdict")) or _mapping(
        diagnostics.get("critic_verdict")
    )
    if not verdict:
        return None

    assessment = _bounded_text(verdict.get("assessment_summary"), limit=limit)
    result: dict[str, Any] = {
        "source": (
            "turn_execution_record.critic.verdict"
            if _mapping(turn_record.get("critic"))
            else "turn_execution_diagnostics.critic_verdict"
        ),
        "verdict": _safe_str(verdict.get("verdict")),
        "confidence": verdict.get("confidence"),
        "has_unresolved_checks": _safe_bool(verdict.get("has_unresolved_checks")),
        "unresolved_check_count": verdict.get("unresolved_check_count"),
    }
    if assessment is not None:
        result["assessment_summary"] = assessment
    response_sha = _safe_str(
        _first_path(
            verdict,
            ("evaluated_response_sha256",),
            ("response_sha256",),
            ("final_response_sha256",),
        )
    )
    if response_sha:
        result["evaluated_response_sha256"] = response_sha.lower()
    return result


def _completion_gate_payload(
    *,
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    gate = _mapping(turn_record.get("completion_gate")) or _mapping(
        diagnostics.get("completion_gate")
    )
    if not gate:
        return None
    evidence_payload = _mapping(gate.get("evidence_payload"))
    execution_blocker = _mapping(evidence_payload.get("execution_signal_blocker"))
    return {
        "source": (
            "turn_execution_record.completion_gate"
            if _mapping(turn_record.get("completion_gate"))
            else "turn_execution_diagnostics.completion_gate"
        ),
        "decision": _safe_str(gate.get("decision")),
        "decision_reason": _safe_str(gate.get("decision_reason")),
        "safe_to_claim_completion": _safe_bool(gate.get("safe_to_claim_completion")),
        "requires_follow_up": _safe_bool(gate.get("requires_follow_up")),
        "terminal_outcome": _safe_str(gate.get("terminal_outcome"))
        or _safe_str(evidence_payload.get("terminal_outcome")),
        "evaluation_basis": _safe_str(evidence_payload.get("evaluation_basis")),
        "blocking_failure_codes": [
            code
            for code in (gate.get("blocking_failure_codes") or [])
            if isinstance(code, str) and code.strip()
        ]
        or [
            code
            for code in (evidence_payload.get("blocking_failure_codes") or [])
            if isinstance(code, str) and code.strip()
        ],
        "execution_signal_blocker": (
            {
                "failure_code": _safe_str(execution_blocker.get("failure_code")),
                "status": _safe_str(execution_blocker.get("status")),
                "status_reason": _safe_str(execution_blocker.get("status_reason")),
                "workflow_id": _safe_str(execution_blocker.get("workflow_id")),
            }
            if execution_blocker
            else None
        ),
    }


def _surface_sha(surface: Mapping[str, Any] | None) -> str | None:
    if not isinstance(surface, Mapping):
        return None
    digest = _safe_str(surface.get("sha256"))
    return digest.lower() if digest else None


def _existing_surface(
    surfaces_payload: Mapping[str, Any],
    name: str,
) -> dict[str, Any] | None:
    surface = _mapping(surfaces_payload.get(name))
    if not surface:
        return None
    if _surface_sha(surface) or _safe_str(surface.get("text")):
        return surface
    return None


def _compare_surfaces(
    left: Mapping[str, Any] | None,
    right: Mapping[str, Any] | None,
) -> bool | None:
    left_sha = _surface_sha(left)
    right_sha = _surface_sha(right)
    if not left_sha or not right_sha:
        return None
    return left_sha == right_sha


def _normalised(value: Any) -> str | None:
    text = _safe_str(value)
    return text.lower() if text else None


def _build_consistency(
    *,
    user_visible_response: Mapping[str, Any] | None,
    recorded_final_response: Mapping[str, Any] | None,
    selected_workflow_response: Mapping[str, Any] | None,
    completion_gate: Mapping[str, Any] | None,
    critic: Mapping[str, Any] | None,
) -> dict[str, Any]:
    user_matches_recorded = _compare_surfaces(
        user_visible_response,
        recorded_final_response,
    )
    user_matches_workflow = _compare_surfaces(
        user_visible_response,
        selected_workflow_response,
    )
    recorded_matches_workflow = _compare_surfaces(
        recorded_final_response,
        selected_workflow_response,
    )

    disagreement_codes: list[str] = []
    if user_matches_recorded is False:
        disagreement_codes.append(
            "user_visible_response_differs_from_turn_record_final_response"
        )
    if user_matches_workflow is False:
        disagreement_codes.append(
            "user_visible_response_differs_from_selected_workflow_response"
        )
    if recorded_matches_workflow is False:
        disagreement_codes.append(
            "turn_record_final_response_differs_from_selected_workflow_response"
        )

    gate_decision = _normalised(
        completion_gate.get("decision") if isinstance(completion_gate, Mapping) else None
    )
    critic_verdict = _normalised(
        critic.get("verdict") if isinstance(critic, Mapping) else None
    )
    if (
        gate_decision in _NON_SUCCESS_GATE_DECISIONS
        and critic_verdict in _PASS_CRITIC_VERDICTS
    ):
        disagreement_codes.append("critic_pass_with_completion_gate_non_success")

    scoring_caveats: list[str] = []
    if gate_decision in _NON_SUCCESS_GATE_DECISIONS and user_visible_response:
        scoring_caveats.append("completion_gate_non_success_with_user_visible_response")
    if (
        selected_workflow_response
        and user_visible_response
        and user_matches_workflow is False
    ):
        scoring_caveats.append("selected_workflow_response_differs_from_visible_answer")

    if disagreement_codes:
        status = "inconsistent"
    elif not user_visible_response and not recorded_final_response:
        status = "insufficient_response_surface_evidence"
    else:
        status = "consistent"

    return {
        "status": status,
        "disagreement_codes": disagreement_codes,
        "scoring_caveats": scoring_caveats,
        "agreement": {
            "user_visible_matches_recorded_final_response": user_matches_recorded,
            "user_visible_matches_selected_workflow_response": user_matches_workflow,
            "recorded_final_response_matches_selected_workflow_response": (
                recorded_matches_workflow
            ),
        },
    }


def build_turn_response_surface_reconciliation(
    *,
    target_message: Mapping[str, Any] | None = None,
    turn_record: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    max_text_chars: int = _DEFAULT_TEXT_LIMIT,
) -> dict[str, Any]:
    """Build deterministic response-surface evidence for replay scoring."""

    target_message_payload = _mapping(target_message)
    turn_record_payload = _mapping(turn_record)
    diagnostics_payload = _mapping(diagnostics)
    text_limit = max(0, int(max_text_chars or _DEFAULT_TEXT_LIMIT))

    user_visible_response = _user_visible_surface(
        target_message=target_message_payload,
        limit=text_limit,
    )
    recorded_final_response = _recorded_final_response_surface(
        turn_record=turn_record_payload,
        limit=text_limit,
    )
    selected_workflow_response = _selected_workflow_response_surface(
        diagnostics=diagnostics_payload,
        turn_record=turn_record_payload,
        limit=text_limit,
    )
    existing_surfaces = _mapping(diagnostics_payload.get("response_surfaces"))
    if user_visible_response is None:
        user_visible_response = _existing_surface(
            existing_surfaces,
            "user_visible_response",
        )
    if recorded_final_response is None:
        recorded_final_response = _existing_surface(
            existing_surfaces,
            "recorded_final_response",
        )
    if selected_workflow_response is None:
        selected_workflow_response = _existing_surface(
            existing_surfaces,
            "selected_workflow_response",
        )
    completion_gate = _completion_gate_payload(
        diagnostics=diagnostics_payload,
        turn_record=turn_record_payload,
    )
    critic = _critic_payload(
        diagnostics=diagnostics_payload,
        turn_record=turn_record_payload,
        limit=text_limit,
    )

    return {
        "schema_version": TURN_RESPONSE_SURFACES_SCHEMA_VERSION,
        "user_visible_response": user_visible_response,
        "recorded_final_response": recorded_final_response,
        "selected_workflow_response": selected_workflow_response,
        "completion_gate": completion_gate,
        "critic": critic,
        "evidence_consistency": _build_consistency(
            user_visible_response=user_visible_response,
            recorded_final_response=recorded_final_response,
            selected_workflow_response=selected_workflow_response,
            completion_gate=completion_gate,
            critic=critic,
        ),
        "policy_boundary": {
            "answer_correctness_classified": False,
            "prompt_promotion_recommended": False,
            "reason": (
                "Response-surface reconciliation only compares deterministic "
                "source fingerprints and telemetry surfaces. Replay and critic "
                "workflows own answer-quality judgement."
            ),
        },
    }


__all__ = [
    "TURN_RESPONSE_SURFACES_SCHEMA_VERSION",
    "build_turn_response_surface_reconciliation",
]
