"""Neutral validation and projection for LLM-authored terminal outcome receipts.

The semantic judgement in a receipt belongs to a represented workflow prompt.
This module deliberately does not infer outcomes, causes, or recovery actions
from tool names, error strings, domains, or workflow identifiers.  It only
validates the authored interface, redacts unsafe values, and applies the hard
invariant that a claimed verified success cannot contradict the deterministic
completion-safety gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


TERMINAL_OUTCOME_RECEIPT_SCHEMA_VERSION = "terminal_outcome_receipt.v1"
TERMINAL_OUTCOME_RECEIPT_VALIDATION_SCHEMA_VERSION = (
    "terminal_outcome_receipt_validation.v1"
)
TERMINAL_OUTCOME_RECEIPT_PROJECTION_SCHEMA_VERSION = (
    "terminal_outcome_receipt_projection.v1"
)
TERMINAL_OUTCOME_RECEIPT_PROFILE_CONCEPT_ID = "#V#terminal_outcome_receipt"

TERMINAL_OUTCOMES = frozenset(
    {
        "verified_success",
        "verified_partial",
        "input_required",
        "externally_blocked",
        "recoverable_failure",
        "terminal_failure",
        "cancelled",
        "inconclusive",
    }
)
CAUSAL_STAGES = frozenset(
    {
        "discovery",
        "selection",
        "planning",
        "retrieval",
        "invocation",
        "execution",
        "verification",
        "persistence",
        "answer_construction",
        "unknown",
        "not_applicable",
    }
)
RETRYABILITY_VALUES = frozenset(
    {
        "now",
        "after_input",
        "after_external_change",
        "after_represented_learning",
        "not_safely_recoverable",
        "not_applicable",
    }
)
RECOVERY_AFFORDANCE_ACTIONS = frozenset(
    {
        "inspect",
        "retry",
        "alternate_workflow",
        "alternate_tool",
        "alternate_model",
        "resume",
        "narrower_answer",
        "elicit_input",
        "escalate",
        "create_learning_candidate",
    }
)
REDACTION_STATUSES = frozenset(
    {"safe_projection", "redacted", "contains_no_sensitive_values"}
)

_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_MAX_DEPTH = 8
_MAX_SEQUENCE_ITEMS = 64
_MAX_MAPPING_ITEMS = 96
_MAX_STRING_CHARS = 2_000


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_sensitive_key(value: Any) -> bool:
    key = str(value or "").strip().lower()
    return any(marker in key for marker in _SENSITIVE_KEY_MARKERS)


def _redact_and_bound(value: Any, *, depth: int = 0) -> tuple[Any, int, bool]:
    """Return a JSON-safe bounded projection and redaction diagnostics."""

    if depth >= _MAX_DEPTH:
        return "[bounded]", 0, True
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        redacted_count = 0
        truncated = len(value) > _MAX_MAPPING_ITEMS
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= _MAX_MAPPING_ITEMS:
                break
            key = str(raw_key)
            if _is_sensitive_key(key):
                projected[key] = "[redacted]"
                redacted_count += 1
                continue
            nested, nested_redacted, nested_truncated = _redact_and_bound(
                raw_value,
                depth=depth + 1,
            )
            projected[key] = nested
            redacted_count += nested_redacted
            truncated = truncated or nested_truncated
        return projected, redacted_count, truncated
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_items: list[Any] = []
        redacted_count = 0
        truncated = len(value) > _MAX_SEQUENCE_ITEMS
        for item in list(value)[:_MAX_SEQUENCE_ITEMS]:
            nested, nested_redacted, nested_truncated = _redact_and_bound(
                item,
                depth=depth + 1,
            )
            projected_items.append(nested)
            redacted_count += nested_redacted
            truncated = truncated or nested_truncated
        return projected_items, redacted_count, truncated
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            return value[:_MAX_STRING_CHARS], 0, True
        return value, 0, False
    if value is None or isinstance(value, (bool, int, float)):
        return value, 0, False
    return str(value)[:_MAX_STRING_CHARS], 0, True


def _mapping_list(value: Any, *, field: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors.append(f"{field}_must_be_array")
        return []
    normalised: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"{field}_{index}_must_be_object")
            continue
        normalised.append({str(key): nested for key, nested in item.items()})
    return normalised


def validate_terminal_outcome_receipt(
    value: Any,
    *,
    completion_gate: Mapping[str, Any] | None = None,
    required: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate an LLM-authored receipt without deciding its semantics.

    ``completion_gate`` is consulted only for the hard safety invariant that a
    receipt cannot claim ``verified_success`` when deterministic required-effect
    checks say completion is unsafe.  Non-success outcome selection and recovery
    choice remain authored workflow/LLM decisions.
    """

    validation: dict[str, Any] = {
        "schema_version": TERMINAL_OUTCOME_RECEIPT_VALIDATION_SCHEMA_VERSION,
        "required": bool(required),
        "present": isinstance(value, Mapping),
        "valid": False,
        "errors": [],
        "redacted_field_count": 0,
        "bounded": False,
        "decision_authority": "represented_llm",
    }
    errors: list[str] = validation["errors"]
    if not isinstance(value, Mapping):
        if required:
            errors.append("terminal_outcome_receipt_missing")
        return None, validation

    projected, redacted_count, bounded = _redact_and_bound(value)
    if not isinstance(projected, Mapping):
        errors.append("terminal_outcome_receipt_not_object")
        return None, validation
    receipt = {str(key): item for key, item in projected.items()}
    validation["redacted_field_count"] = redacted_count
    validation["bounded"] = bounded

    schema_version = _safe_text(receipt.get("schema_version"))
    if schema_version != TERMINAL_OUTCOME_RECEIPT_SCHEMA_VERSION:
        errors.append("terminal_outcome_receipt_schema_version_invalid")

    profile_concept_id = _safe_text(receipt.get("profile_concept_id"))
    if profile_concept_id != TERMINAL_OUTCOME_RECEIPT_PROFILE_CONCEPT_ID:
        errors.append("terminal_outcome_receipt_profile_invalid")

    outcome = _safe_text(receipt.get("outcome"))
    if outcome not in TERMINAL_OUTCOMES:
        errors.append("terminal_outcome_receipt_outcome_invalid")

    causal_stage = _safe_text(receipt.get("causal_stage"))
    if causal_stage not in CAUSAL_STAGES:
        errors.append("terminal_outcome_receipt_causal_stage_invalid")

    if not _safe_text(receipt.get("summary")):
        errors.append("terminal_outcome_receipt_summary_missing")

    retryability = _safe_text(receipt.get("retryability"))
    if retryability not in RETRYABILITY_VALUES:
        errors.append("terminal_outcome_receipt_retryability_invalid")

    redaction_status = _safe_text(receipt.get("redaction_status"))
    if redacted_count:
        receipt["redaction_status"] = "redacted"
    elif redaction_status not in REDACTION_STATUSES:
        errors.append("terminal_outcome_receipt_redaction_status_invalid")

    evidence_refs = _mapping_list(
        receipt.get("evidence_refs"), field="evidence_refs", errors=errors
    )
    committed_effects = _mapping_list(
        receipt.get("committed_effects"), field="committed_effects", errors=errors
    )
    remaining_obligations = _mapping_list(
        receipt.get("remaining_obligations"),
        field="remaining_obligations",
        errors=errors,
    )
    recovery_affordances = _mapping_list(
        receipt.get("recovery_affordances"),
        field="recovery_affordances",
        errors=errors,
    )
    receipt["evidence_refs"] = evidence_refs
    receipt["committed_effects"] = committed_effects
    receipt["remaining_obligations"] = remaining_obligations
    receipt["recovery_affordances"] = recovery_affordances

    for index, affordance in enumerate(recovery_affordances):
        action_type = _safe_text(affordance.get("action_type"))
        if action_type not in RECOVERY_AFFORDANCE_ACTIONS:
            errors.append(
                f"terminal_outcome_receipt_recovery_affordance_{index}_invalid"
            )

    learning_candidate = receipt.get("learning_candidate")
    if learning_candidate is not None and not isinstance(learning_candidate, Mapping):
        errors.append("terminal_outcome_receipt_learning_candidate_invalid")

    provenance = receipt.get("provenance")
    if not isinstance(provenance, Mapping):
        errors.append("terminal_outcome_receipt_provenance_missing")
    else:
        decision_source = _safe_text(provenance.get("decision_source"))
        validation["decision_authority"] = decision_source or "unknown"
        if decision_source not in {
            "represented_llm",
            "represented_workflow_default",
        }:
            errors.append("terminal_outcome_receipt_decision_source_invalid")

    if outcome == "verified_success" and isinstance(completion_gate, Mapping):
        if completion_gate.get("safe_to_claim_completion") is False:
            errors.append(
                "terminal_outcome_receipt_verified_success_conflicts_with_gate"
            )

    if outcome not in {None, "verified_success"} and not _safe_text(
        receipt.get("cause_code")
    ):
        errors.append("terminal_outcome_receipt_cause_code_missing")

    validation["valid"] = not errors
    validation["outcome"] = outcome
    validation["profile_concept_id"] = profile_concept_id
    if errors:
        return None, validation
    return receipt, validation


def apply_terminal_outcome_receipt_to_completion_gate(
    completion_gate: Mapping[str, Any] | None,
    *,
    receipt: Mapping[str, Any] | None,
    validation: Mapping[str, Any],
    authoritative_receipt_required: bool,
) -> dict[str, Any]:
    """Project the represented outcome into legacy gate fields.

    This is compatibility plumbing, not an outcome classifier.  The outcome is
    copied from the LLM-authored receipt; deterministic blockers may veto a
    success claim but never invent a more favourable one.
    """

    gate = dict(completion_gate or {})
    evidence_payload = (
        dict(gate.get("evidence_payload"))
        if isinstance(gate.get("evidence_payload"), Mapping)
        else {}
    )
    evidence_payload["terminal_outcome_receipt_validation"] = dict(validation)

    valid = bool(validation.get("valid")) and isinstance(receipt, Mapping)
    if valid:
        outcome = _safe_text(receipt.get("outcome"))
        evidence_payload["terminal_outcome_receipt"] = dict(receipt)
        gate["terminal_outcome_receipt_source"] = (
            _safe_text(validation.get("decision_authority")) or "represented_llm"
        )
        if outcome == "verified_success":
            gate["decision"] = "completed"
        else:
            gate["decision"] = outcome or gate.get("decision")
            gate["safe_to_claim_completion"] = False
            gate["requires_follow_up"] = True
    elif authoritative_receipt_required:
        failure_codes = [
            str(code)
            for code in validation.get("errors") or []
            if isinstance(code, str) and code.strip()
        ] or ["terminal_outcome_receipt_invalid"]
        existing_codes = [
            str(code)
            for code in gate.get("blocking_failure_codes") or []
            if isinstance(code, str) and code.strip()
        ]
        gate["blocking_failure_codes"] = sorted(set([*existing_codes, *failure_codes]))
        gate["safe_to_claim_completion"] = False
        gate["requires_follow_up"] = True
        gate["terminal_outcome_receipt_source"] = "missing_or_invalid"

    gate["evidence_payload"] = evidence_payload
    return gate


def build_terminal_outcome_receipt_projection(
    receipt: Mapping[str, Any] | None,
    *,
    validation: Mapping[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Build one neutral, bounded projection for read and evaluation surfaces.

    The projection copies an already-authored receipt.  It does not infer an
    outcome, causal stage, retry policy, or recovery action from surrounding
    runtime data.  Re-validating here ensures every projection receives the
    same redaction and bounding treatment even when its caller loaded an older
    persisted record.
    """

    clean_source = _safe_text(source) or "unspecified"
    if not isinstance(receipt, Mapping):
        return {
            "schema_version": TERMINAL_OUTCOME_RECEIPT_PROJECTION_SCHEMA_VERSION,
            "available": False,
            "source": clean_source,
            "receipt": None,
            "validation": (
                dict(validation) if isinstance(validation, Mapping) else None
            ),
        }

    projected_receipt, current_validation = validate_terminal_outcome_receipt(
        receipt,
        required=True,
    )
    if isinstance(validation, Mapping):
        supplied_validation = dict(validation)
        supplied_errors = supplied_validation.get("errors")
        current_errors = current_validation.get("errors")
        if isinstance(supplied_errors, list) and supplied_errors:
            current_validation["source_validation_errors"] = list(supplied_errors)
        if supplied_validation.get("valid") is False:
            current_validation["source_validation_valid"] = False
        if isinstance(current_errors, list) and current_errors:
            current_validation["projection_validation_errors"] = list(current_errors)

    if not isinstance(projected_receipt, Mapping):
        return {
            "schema_version": TERMINAL_OUTCOME_RECEIPT_PROJECTION_SCHEMA_VERSION,
            "available": False,
            "source": clean_source,
            "receipt": None,
            "validation": current_validation,
        }

    receipt_payload = dict(projected_receipt)
    return {
        "schema_version": TERMINAL_OUTCOME_RECEIPT_PROJECTION_SCHEMA_VERSION,
        "available": True,
        "source": clean_source,
        "receipt_schema_version": receipt_payload.get("schema_version"),
        "profile_concept_id": receipt_payload.get("profile_concept_id"),
        "outcome": receipt_payload.get("outcome"),
        "causal_stage": receipt_payload.get("causal_stage"),
        "cause_code": receipt_payload.get("cause_code"),
        "summary": receipt_payload.get("summary"),
        "evidence_refs": list(receipt_payload.get("evidence_refs") or []),
        "committed_effects": list(receipt_payload.get("committed_effects") or []),
        "remaining_obligations": list(
            receipt_payload.get("remaining_obligations") or []
        ),
        "retryability": receipt_payload.get("retryability"),
        "recovery_affordances": list(
            receipt_payload.get("recovery_affordances") or []
        ),
        "learning_candidate": receipt_payload.get("learning_candidate"),
        "provenance": dict(receipt_payload.get("provenance") or {}),
        "redaction_status": receipt_payload.get("redaction_status"),
        "receipt": receipt_payload,
        "validation": current_validation,
    }


def terminal_outcome_receipt_projection_from_record(
    record: Mapping[str, Any] | None,
    *,
    source: str = "turn_execution_record",
) -> dict[str, Any]:
    """Project the canonical receipt from a persisted turn-record shape."""

    if not isinstance(record, Mapping):
        return build_terminal_outcome_receipt_projection(None, source=source)
    receipt = record.get("terminal_outcome_receipt")
    validation = record.get("terminal_outcome_receipt_validation")
    if not isinstance(receipt, Mapping):
        completion_gate = record.get("completion_gate")
        if isinstance(completion_gate, Mapping):
            evidence_payload = completion_gate.get("evidence_payload")
            if isinstance(evidence_payload, Mapping):
                receipt = evidence_payload.get("terminal_outcome_receipt")
                validation = evidence_payload.get(
                    "terminal_outcome_receipt_validation"
                )
    return build_terminal_outcome_receipt_projection(
        receipt if isinstance(receipt, Mapping) else None,
        validation=validation if isinstance(validation, Mapping) else None,
        source=source,
    )
