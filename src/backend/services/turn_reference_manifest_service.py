"""Typed, bounded references for one user-visible conversation turn.

The manifest is a presentation contract, not another source of truth. Evidence
entries are projected from the persisted turn envelope, workflow instances from
structured effect facts, and assertion IDs are admitted only after an exact
actor-visible lookup. Regexes discover candidates in screen text; they never
decide what an identifier means.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .scoped_assertion_service import get_visible_scoped_assertion_by_id

SCHEMA_VERSION = "turn_reference_manifest.v1"
MAX_REFERENCES = 64

_ASSERTION_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(ska_[A-Za-z0-9_-]{4,256})(?![A-Za-z0-9_-])"
)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _outcome_facts(outcome_report: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(outcome_report, Mapping):
        return []
    facts = outcome_report.get("facts")
    if not isinstance(facts, list):
        return []
    return [item for item in facts if isinstance(item, Mapping)]


def _reference_entry(
    *,
    reference_id: str,
    reference_type: str,
    screen_text: str,
    source: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "reference_id": reference_id,
        "reference_type": reference_type,
        "label": {
            "scoped_assertion": "Assertion",
            "turn_evidence": "Turn evidence",
            "workflow_instance": "Workflow instance",
            "ontology_mutation_receipt": "Ontology mutation receipt",
        }.get(reference_type, "Reference"),
        "source": source,
        "occurrence_count": screen_text.count(reference_id),
        **extra,
    }


def _bounded_evidence_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "schema_version",
        "evidence_id",
        "tool_name",
        "call_id",
        "turn_id",
        "status",
        "trust_boundary",
        "sha256",
        "size_bytes",
        "char_count",
        "content_type",
        "value_kind",
        "shape",
        "preview",
        "preview_format",
        "preview_truncated",
        "provenance",
        "source_diagnostics",
        "available_selectors",
    )
    return {key: value.get(key) for key in allowed if key in value}


def build_turn_reference_manifest(
    *,
    response_text: Any,
    request_id: Any,
    evidence_index: Sequence[Mapping[str, Any]] = (),
    outcome_report: Mapping[str, Any] | None = None,
    tool_invocations: Sequence[Mapping[str, Any]] = (),
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    assertion_resolver: Callable[..., Mapping[str, Any] | None] = (
        get_visible_scoped_assertion_by_id
    ),
) -> dict[str, Any]:
    """Build one server-typed manifest for identifiers present on screen."""

    screen_text = str(response_text or "")
    references: dict[str, dict[str, Any]] = {}

    envelope_by_id: dict[str, Mapping[str, Any]] = {}
    for envelope in evidence_index:
        if not isinstance(envelope, Mapping):
            continue
        evidence_id = _clean(envelope.get("evidence_id"))
        if evidence_id:
            envelope_by_id[evidence_id] = envelope

    structured_evidence_ids: set[str] = set(envelope_by_id)
    workflow_instances: dict[str, dict[str, Any]] = {}
    mutation_receipt_ids: set[str] = set()
    for fact in _outcome_facts(outcome_report):
        evidence_id = _clean(fact.get("evidence_id"))
        if evidence_id:
            structured_evidence_ids.add(evidence_id)
        instance_id = _clean(fact.get("instance_id"))
        if instance_id:
            workflow_instances[instance_id] = {
                "workflow_id": _clean(fact.get("workflow_id")),
                "effect_status": _clean(fact.get("effect_status")),
                "canonical_readback_verified": (
                    fact.get("canonical_readback_verified") is True
                ),
            }

    for invocation in tool_invocations:
        if not isinstance(invocation, Mapping):
            continue
        evidence = invocation.get("evidence")
        if isinstance(evidence, Mapping):
            evidence_id = _clean(evidence.get("evidence_id"))
            if evidence_id:
                structured_evidence_ids.add(evidence_id)
        workflow_id = _clean(invocation.get("workflow_id"))
        execution_id = _clean(invocation.get("execution_id"))
        if workflow_id and execution_id:
            workflow_instances.setdefault(
                execution_id,
                {"workflow_id": workflow_id},
            )
        for key in ("receipt_id", "mutation_receipt_id"):
            receipt_id = _clean(invocation.get(key))
            if receipt_id:
                mutation_receipt_ids.add(receipt_id)

    for evidence_id in structured_evidence_ids:
        if evidence_id not in screen_text:
            continue
        envelope = envelope_by_id.get(evidence_id)
        references[evidence_id] = _reference_entry(
            reference_id=evidence_id,
            reference_type="turn_evidence",
            screen_text=screen_text,
            source=(
                "persisted_evidence_index" if envelope else "structured_effect_fact"
            ),
            evidence=(
                _bounded_evidence_envelope(envelope) if envelope is not None else None
            ),
            lifecycle={
                "handle_scope": "exact_actor_and_turn",
                "complete_result_lifetime": "ordinary_turn_only",
                "persisted_projection": "bounded_envelope",
                "durable_evidence_receipt": False,
            },
        )

    for instance_id, instance in workflow_instances.items():
        if instance_id not in screen_text:
            continue
        references[instance_id] = _reference_entry(
            reference_id=instance_id,
            reference_type="workflow_instance",
            screen_text=screen_text,
            source="structured_workflow_effect",
            workflow=instance,
        )

    for receipt_id in mutation_receipt_ids:
        if receipt_id not in screen_text:
            continue
        references[receipt_id] = _reference_entry(
            reference_id=receipt_id,
            reference_type="ontology_mutation_receipt",
            screen_text=screen_text,
            source="structured_mutation_receipt",
        )

    assertion_candidates: list[str] = []
    seen_candidates: set[str] = set()
    for match in _ASSERTION_CANDIDATE_RE.finditer(screen_text):
        assertion_id = match.group(1)
        if assertion_id in seen_candidates:
            continue
        seen_candidates.add(assertion_id)
        assertion_candidates.append(assertion_id)
        if len(assertion_candidates) >= MAX_REFERENCES:
            break
    for assertion_id in assertion_candidates:
        try:
            assertion = assertion_resolver(
                assertion_id,
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
            )
        except Exception:  # noqa: BLE001 - inspection must not break the turn
            # Reference enrichment is fail-soft; assertion content remains
            # available through its canonical store once that read recovers.
            assertion = None
        if not isinstance(assertion, Mapping):
            continue
        references[assertion_id] = _reference_entry(
            reference_id=assertion_id,
            reference_type="scoped_assertion",
            screen_text=screen_text,
            source="exact_actor_visible_assertion_read",
            assertion={
                key: assertion.get(key)
                for key in (
                    "assertion_form",
                    "assertion_revision",
                    "status",
                    "subject_concept_id",
                    "predicate",
                    "object_kind",
                    "updated_at",
                )
                if key in assertion
            },
        )

    ordered = sorted(
        references.values(),
        key=lambda item: screen_text.find(str(item.get("reference_id") or "")),
    )[:MAX_REFERENCES]
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": _clean(request_id),
        "references": ordered,
        "reference_count": len(ordered),
        "typing_rule": "structured_or_exact_actor_visible_read",
    }


def build_turn_reference_manifest_from_debug(
    *,
    response_text: Any,
    debug_info: Mapping[str, Any] | None,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    assertion_resolver: Callable[..., Mapping[str, Any] | None] = (
        get_visible_scoped_assertion_by_id
    ),
) -> dict[str, Any]:
    """Reconstruct a viewer-scoped manifest from persisted turn diagnostics.

    History entries created before the manifest contract still retain bounded
    evidence and effect projections. Rebuilding at read time makes those
    references inspectable without rewriting history, and re-runs assertion
    visibility for the current viewer rather than trusting the original actor's
    manifest.
    """

    debug = debug_info if isinstance(debug_info, Mapping) else {}
    tool_invocations = (
        debug.get("turn_execution_record_tool_invocations")
        if isinstance(debug.get("turn_execution_record_tool_invocations"), list)
        else (
            debug.get("tool_invocations")
            if isinstance(debug.get("tool_invocations"), list)
            else []
        )
    )
    auxiliary_calls = (
        debug.get("aux_llm_calls")
        if isinstance(debug.get("aux_llm_calls"), list)
        else []
    )

    evidence_index: list[dict[str, Any]] = []
    for entry in reversed(auxiliary_calls):
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") != "adaptive_turn_evidence_index":
            continue
        raw_evidence = entry.get("evidence")
        if isinstance(raw_evidence, list):
            evidence_index = [
                dict(item) for item in raw_evidence if isinstance(item, Mapping)
            ]
        break

    outcome_sources: list[Any] = [*auxiliary_calls]
    diagnostics = debug.get("turn_execution_diagnostics")
    if isinstance(diagnostics, Mapping):
        diagnostic_auxiliary_calls = diagnostics.get("aux_llm_calls")
        if isinstance(diagnostic_auxiliary_calls, list):
            outcome_sources.extend(diagnostic_auxiliary_calls)

    outcome_report: Mapping[str, Any] | None = None
    for entry in reversed(outcome_sources):
        if not isinstance(entry, Mapping):
            continue
        if (
            entry.get("type") == "adaptive_turn_effect_outcome_report"
            and entry.get("schema_version") == "adaptive_turn_effect_outcome_report.v1"
        ):
            outcome_report = entry
            break

    return build_turn_reference_manifest(
        response_text=response_text,
        request_id=debug.get("request_id"),
        evidence_index=evidence_index,
        outcome_report=outcome_report,
        tool_invocations=tool_invocations,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        assertion_resolver=assertion_resolver,
    )


__all__ = [
    "MAX_REFERENCES",
    "SCHEMA_VERSION",
    "build_turn_reference_manifest",
    "build_turn_reference_manifest_from_debug",
]
