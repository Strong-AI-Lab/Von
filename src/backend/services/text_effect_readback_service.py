"""Canonical verification for text-relation effects emitted by a workflow.

The represented workflow supplies the predicates that matter.  This service
only correlates successful text-mutation receipts from the current execution
with actor-effective canonical text-relation reads.  It does not decide which
facts a meeting, paper, or other domain object ought to contain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .text_value_service import get_texts_for_concept
from .text_relation_predicate_validation_service import predicate_concept_id_for_storage

TEXT_EFFECT_READBACK_SCHEMA_VERSION = "workflow_text_effect_readback.v1"
MAX_TEXT_EFFECT_RECORDS = 80
MAX_REQUIRED_TEXT_PREDICATES = 20
_TEXT_MUTATION_TOOLS = frozenset(
    {"upsert_text_relation", "upsert_singleton_text_relation"}
)


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _records(value: Any) -> list[Mapping[str, Any]] | None:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    rows = [row for row in value if isinstance(row, Mapping)]
    return rows if len(rows) == len(value) else None


def _successful_text_effect(record: Mapping[str, Any]) -> dict[str, str] | None:
    tool_name = _clean_text(record.get("tool"))
    if tool_name not in _TEXT_MUTATION_TOOLS or record.get("status") != "ok":
        return None
    arguments = _mapping(record.get("effective_arguments"))
    payload = _mapping(record.get("effective_payload"))
    if arguments is None or payload is None or payload.get("success") is not True:
        return None

    concept_id = _clean_text(arguments.get("concept_id"))
    predicate = _clean_text(arguments.get("predicate"))
    text = _clean_text(arguments.get("text"))
    if not concept_id or not predicate or not text:
        return None

    payload_concept_id = _clean_text(payload.get("concept_id"))
    payload_predicate = _clean_text(
        payload.get("predicate_concept_id") or payload.get("predicate")
    )
    if payload_concept_id and payload_concept_id != concept_id:
        return None
    if payload_predicate and (
        predicate_concept_id_for_storage(payload_predicate) or payload_predicate
    ) != (predicate_concept_id_for_storage(predicate) or predicate):
        return None
    effect_status = _clean_text(payload.get("effect_status"))
    if effect_status and effect_status != "succeeded":
        return None
    return {
        "tool": tool_name,
        "concept_id": concept_id,
        "predicate": predicate,
        "text": text,
    }


def verify_text_effect_readback(
    *,
    required_predicates: Any,
    optional_predicates: Any = None,
    tool_invocations: Any = None,
    text_effect_receipts: Any = None,
    expected_concept_id: Any = None,
) -> dict[str, Any]:
    """Verify required text effects against actor-effective canonical state.

    Every required predicate must have a successful current-run mutation receipt
    for the same single concept, and the exact receipted text must be present in
    canonical read-back.  Extra receipted text predicates are also checked so a
    represented workflow can require a small core while retaining exact proof of
    richer source facts such as an end time.
    """

    expected_id = _clean_text(expected_concept_id)
    if not isinstance(required_predicates, Sequence) or isinstance(
        required_predicates, (str, bytes, bytearray)
    ):
        required: list[str] = []
    else:
        required = list(
            dict.fromkeys(
                predicate
                for predicate in (_clean_text(item) for item in required_predicates)
                if predicate
            )
        )

    if optional_predicates is None:
        optional: list[str] = []
        optional_predicates_invalid = False
    elif not isinstance(optional_predicates, Sequence) or isinstance(
        optional_predicates, (str, bytes, bytearray)
    ):
        optional = []
        optional_predicates_invalid = True
    else:
        optional = list(
            dict.fromkeys(
                predicate
                for predicate in (_clean_text(item) for item in optional_predicates)
                if predicate and predicate not in required
            )
        )
        optional_predicates_invalid = False

    invocations = _records(tool_invocations)
    receipts = _records(text_effect_receipts)
    failure_code: str | None = None
    if (
        not required
        or len(required) + len(optional) > MAX_REQUIRED_TEXT_PREDICATES
        or (optional_predicates is not None and optional_predicates_invalid)
        or invocations is None
        or receipts is None
        or len(invocations) + len(receipts) > MAX_TEXT_EFFECT_RECORDS
    ):
        failure_code = "text_effect_readback_inputs_invalid"

    effects: list[dict[str, str]] = []
    if failure_code is None:
        accepted_predicates = set(required) | set(optional)
        for record in [*(invocations or []), *(receipts or [])]:
            effect = _successful_text_effect(record)
            if effect is not None and effect["predicate"] in accepted_predicates:
                effects.append(effect)

    concept_ids = list(dict.fromkeys(effect["concept_id"] for effect in effects))
    if failure_code is None:
        if expected_id:
            effects = [
                effect for effect in effects if effect["concept_id"] == expected_id
            ]
            concept_id = expected_id
        elif len(concept_ids) == 1:
            concept_id = concept_ids[0]
        else:
            concept_id = ""
            failure_code = (
                "text_effect_concept_missing"
                if not concept_ids
                else "text_effect_concept_ambiguous"
            )
    else:
        concept_id = expected_id

    effects_by_predicate: dict[str, list[dict[str, str]]] = {}
    for effect in effects:
        effects_by_predicate.setdefault(effect["predicate"], []).append(effect)

    missing_receipts = [
        predicate for predicate in required if not effects_by_predicate.get(predicate)
    ]
    if failure_code is None and missing_receipts:
        failure_code = "text_effect_required_mutation_missing"

    verified_effects: list[dict[str, Any]] = []
    canonical_rows: dict[str, list[dict[str, Any]]] = {}
    if failure_code is None:
        for predicate, predicate_effects in effects_by_predicate.items():
            rows = get_texts_for_concept(
                subject_concept_id=concept_id,
                predicate=predicate,
                limit=20,
                context_view="actor_effective",
            )
            serialised_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
            canonical_rows[predicate] = serialised_rows
            canonical_texts = {
                text for text in (_clean_text(row.get("text")) for row in rows) if text
            }
            for effect in predicate_effects:
                if effect["text"] not in canonical_texts:
                    failure_code = "text_effect_exact_readback_missing"
                    break
                matching_row = next(
                    row
                    for row in serialised_rows
                    if _clean_text(row.get("text")) == effect["text"]
                )
                verified_effects.append(
                    {
                        "concept_id": concept_id,
                        "predicate": predicate,
                        "text": effect["text"],
                        "relation_id": matching_row.get("relation_id"),
                    }
                )
            if failure_code is not None:
                break

    verified = failure_code is None
    return {
        "schema_version": TEXT_EFFECT_READBACK_SCHEMA_VERSION,
        "success": verified,
        "text_effect_readback_verified": verified,
        "text_effect_readback_failure_code": failure_code,
        "represented_concept_id": concept_id or None,
        "required_predicates": required,
        "optional_predicates": optional,
        "missing_receipt_predicates": missing_receipts,
        "verified_text_effects": verified_effects,
        "canonical_readback": canonical_rows,
    }


__all__ = [
    "TEXT_EFFECT_READBACK_SCHEMA_VERSION",
    "verify_text_effect_readback",
]
