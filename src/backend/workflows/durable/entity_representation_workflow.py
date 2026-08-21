from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ...services import concept_search_service, concept_service
from ...services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)
from ...services.workflow_event_integration_service import resolve_event_actor_context
from ...services.workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    stable_named_instance_concept_id,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)

logger = logging.getLogger(__name__)

ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID = (
    "entity_representation.materialise_from_payload"
)

_ENTITY_DOMAIN_DEFAULT_TYPE_IDS: dict[str, str] = {
    "person": "#V#person",
    "company": "#V#organisation",
    "event": "#V#event",
    "place": "#V#place",
}


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_non_empty_text(*values: Any) -> str | None:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return None


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(text)
    return values


_REQUESTED_FACT_TEXT_FIELDS: tuple[str, ...] = (
    "claim_text",
    "relation_hint",
    "target_name",
    "identifier_scheme",
    "identifier_value",
    "evidence_text",
    "source_locator",
)


def _normalise_requested_facts(value: Any) -> tuple[list[dict[str, str]], bool]:
    """Preserve grounded non-core facts without interpreting their semantics."""

    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return [], False

    facts: list[dict[str, str]] = []
    valid = True
    for item in value:
        if not isinstance(item, Mapping):
            valid = False
            continue
        fact = {
            field_name: text
            for field_name in _REQUESTED_FACT_TEXT_FIELDS
            if (text := _clean_text(item.get(field_name)))
        }
        if not fact.get("claim_text"):
            valid = False
            continue
        facts.append(fact)
    return facts, valid


def _requested_fact_coverage_outputs(value: Any) -> dict[str, Any]:
    facts, payload_valid = _normalise_requested_facts(value)
    facts_complete = payload_valid and not facts
    return {
        "entity_representation_requested_facts": facts,
        "entity_representation_unresolved_requested_facts": facts,
        "entity_representation_requested_fact_count": len(facts),
        "entity_representation_requested_facts_payload_valid": payload_valid,
        "entity_representation_requested_facts_complete": facts_complete,
        "entity_representation_coverage": (
            "complete" if facts_complete else "core_only"
        ),
    }


def _normalise_entity_domain(value: Any) -> str:
    raw = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "organisation": "company",
        "organization": "company",
        "business": "company",
        "startup": "company",
        "meeting": "event",
        "calendar_event": "event",
        "location": "place",
    }
    resolved = aliases.get(raw, raw)
    if resolved in _ENTITY_DOMAIN_DEFAULT_TYPE_IDS:
        return resolved
    return ""


def _concept_has_exact_name(*, concept_id: str, concept_name: str) -> bool:
    expected = _clean_text(concept_name).casefold()
    if not expected:
        return False

    try:
        name_rows = get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            limit=200,
        )
    except Exception:
        name_rows = []
    for row in name_rows:
        if not isinstance(row, Mapping):
            continue
        value = _clean_text(row.get("text")).casefold()
        if value and value == expected:
            return True

    try:
        concept = concept_service.get_concept_by_concept_id_exact(concept_id)
    except Exception:
        concept = None
    fallback_name = _clean_text((concept or {}).get("name")).casefold()
    return bool(fallback_name and fallback_name == expected)


def _find_verified_named_instance_concept_ids(
    *,
    concept_name: str,
    instance_of_type_id: str,
) -> list[str]:
    try:
        search_result = concept_search_service.search_concepts(
            query=concept_name,
            instance_of=instance_of_type_id,
            match_type="exact",
            include_description=False,
            limit=25,
        )
    except Exception:
        return []

    rows = search_result.get("results") if isinstance(search_result, Mapping) else None
    if not isinstance(rows, list):
        return []

    verified_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        candidate_id = _clean_text(row.get("concept_id"))
        if not candidate_id:
            continue
        if _concept_has_exact_name(
            concept_id=candidate_id,
            concept_name=concept_name,
        ):
            verified_ids.append(candidate_id)
    return list(dict.fromkeys(verified_ids))


def _resolve_actor_context(
    request: WorkflowActionRequest,
) -> tuple[str | None, str | None]:
    explicit_user = _first_non_empty_text(
        request.inputs.get("user_concept_id"),
        request.data.get("user_concept_id"),
    )
    explicit_org = _first_non_empty_text(
        request.inputs.get("org_concept_id"),
        request.data.get("org_concept_id"),
    )
    resolved_user, resolved_org = resolve_event_actor_context(
        user_id=explicit_user,
        org_id=explicit_org,
        namespace=_clean_text(getattr(request.environment, "user_namespace", None))
        or None,
    )
    return _clean_text(resolved_user) or None, _clean_text(resolved_org) or None


def _read_only_existing_entity_result(
    *,
    concept_id: str,
    entity_domain: str,
    entity_type_id: str,
    entity_name: str,
    requested_fact_coverage: Mapping[str, Any],
) -> WorkflowActionResult:
    """Return an idempotent handoff without changing an existing concept.

    The represented workflow owns reuse/readback policy.  This guard only
    closes the race between its read-only resolution step and this additive
    create primitive.
    """

    return WorkflowActionResult(
        status="success",
        outputs={
            "entity_representation_materialised": False,
            "entity_core_representation_verified": True,
            "entity_representation_verified": bool(
                requested_fact_coverage.get(
                    "entity_representation_requested_facts_complete"
                )
            ),
            "entity_representation_domain": entity_domain,
            "entity_representation_type_id": entity_type_id,
            "entity_representation_concept_id": concept_id,
            "entity_representation_reused_existing": True,
            "entity_representation_name": entity_name,
            **dict(requested_fact_coverage),
        },
    )


def _handle_materialise_from_payload(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    entity_domain = _normalise_entity_domain(
        request.inputs.get("entity_domain") or request.data.get("entity_domain")
    )
    if not entity_domain:
        return WorkflowActionResult(
            status="failed",
            error="entity_representation_domain_missing_or_invalid",
            outputs={
                "response_text": (
                    "I couldn't determine whether this should be represented as a "
                    "person, company, event, or place."
                )
            },
        )

    entity_type_id = (
        _first_non_empty_text(
            request.inputs.get("entity_type_id"),
            request.data.get("entity_type_id"),
        )
        or _ENTITY_DOMAIN_DEFAULT_TYPE_IDS[entity_domain]
    )

    entity_name = _first_non_empty_text(
        request.inputs.get("entity_name"),
        request.data.get("entity_name"),
        request.inputs.get("name"),
        request.data.get("name"),
        request.inputs.get("title"),
        request.data.get("title"),
    )
    if not entity_name:
        return WorkflowActionResult(
            status="failed",
            error="entity_representation_entity_name_missing",
            outputs={
                "response_text": (
                    "I need the entity name before I can represent this in the "
                    "Vontology."
                )
            },
        )

    entity_description = _first_non_empty_text(
        request.inputs.get("entity_description"),
        request.data.get("entity_description"),
        request.inputs.get("description"),
        request.data.get("description"),
        request.inputs.get("summary"),
        request.data.get("summary"),
    )
    source_text = _first_non_empty_text(
        request.inputs.get("entity_source_text"),
        request.data.get("entity_source_text"),
        request.inputs.get("request_text"),
        request.data.get("request_text"),
        request.inputs.get("prompt"),
        request.data.get("prompt"),
    )
    aliases = [
        alias
        for alias in _coerce_string_list(
            request.inputs.get("entity_aliases")
            or request.data.get("entity_aliases")
            or request.inputs.get("aliases")
            or request.data.get("aliases")
        )
        if alias.casefold() != entity_name.casefold()
    ]
    raw_requested_facts = (
        request.inputs.get("requested_facts")
        if "requested_facts" in request.inputs
        else request.data.get("requested_facts")
    )
    requested_fact_coverage = _requested_fact_coverage_outputs(
        raw_requested_facts
    )

    verified_ids = _find_verified_named_instance_concept_ids(
        concept_name=entity_name,
        instance_of_type_id=entity_type_id,
    )
    if len(verified_ids) > 1:
        return WorkflowActionResult(
            status="success",
            outputs={
                "entity_representation_materialised": False,
                "entity_core_representation_verified": False,
                "entity_representation_verified": False,
                "entity_representation_domain": entity_domain,
                "entity_representation_type_id": entity_type_id,
                "entity_representation_concept_id": "",
                "entity_representation_reused_existing": False,
                "entity_representation_name": entity_name,
                **dict(requested_fact_coverage),
                "entity_representation_coverage": "not_started",
                "entity_resolution_status": "ambiguous",
                "entity_resolution_candidates": [
                    {"concept_id": concept_id} for concept_id in verified_ids
                ],
                "response_text": (
                    f"I found multiple {entity_domain} concepts named "
                    f"'{entity_name}'. Please clarify which one you mean."
                )
            },
        )

    if verified_ids:
        return _read_only_existing_entity_result(
            concept_id=verified_ids[0],
            entity_domain=entity_domain,
            entity_type_id=entity_type_id,
            entity_name=entity_name,
            requested_fact_coverage=requested_fact_coverage,
        )

    user_concept_id, org_concept_id = _resolve_actor_context(request)
    concept_id = stable_named_instance_concept_id(
        entity_name,
        prefix=entity_domain,
    )
    try:
        concept_service.create_concept(
            name=entity_name,
            concept_id=concept_id,
            description=entity_description,
            parent_concept_ids=[entity_type_id],
            create_as_instance=True,
            created_by_concept_id=user_concept_id,
            organisation_concept_id=org_concept_id,
            event_namespace=_clean_text(
                getattr(request.environment, "user_namespace", None)
            )
            or None,
        )
    except Exception as exc:
        logger.warning(
            "[entity_representation] create concept failed for %s (%s): %s",
            entity_name,
            entity_type_id,
            exc,
        )
        verified_ids = _find_verified_named_instance_concept_ids(
            concept_name=entity_name,
            instance_of_type_id=entity_type_id,
        )
        if len(verified_ids) == 1:
            return _read_only_existing_entity_result(
                concept_id=verified_ids[0],
                entity_domain=entity_domain,
                entity_type_id=entity_type_id,
                entity_name=entity_name,
                requested_fact_coverage=requested_fact_coverage,
            )
        return WorkflowActionResult(
            status="failed",
            error=f"entity_representation_concept_create_failed:{exc}",
            outputs={
                "response_text": (
                    f"I couldn't materialise the {entity_domain} "
                    f"'{entity_name}' just now."
                )
            },
        )

    ensure_instance_typing(concept_id=concept_id, type_ids=[entity_type_id])

    provenance = {
        "source": "entity_representation_workflow",
        "reason": "entity_representation.materialise_from_payload",
    }
    if entity_description:
        upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text=entity_description,
            lang="en-NZ",
            provenance=provenance,
            context={"entity_domain": entity_domain},
            garbage_collect=True,
        )
    if source_text:
        upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate="hasNote",
            text=source_text,
            lang="en-NZ",
            provenance=provenance,
            context={
                "entity_domain": entity_domain,
                "note_type": "entity_representation_source_text",
            },
            garbage_collect=True,
        )
    for alias in aliases:
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text=alias,
            lang="en-NZ",
            provenance=provenance,
            context={
                "entity_domain": entity_domain,
                "name_role": "alias",
            },
        )

    response_text = f"Represented {entity_domain} '{entity_name}' as {concept_id}."
    return WorkflowActionResult(
        status="success",
        outputs={
            "entity_representation_materialised": True,
            "entity_core_representation_verified": True,
            "entity_representation_verified": bool(
                requested_fact_coverage.get(
                    "entity_representation_requested_facts_complete"
                )
            ),
            "entity_representation_domain": entity_domain,
            "entity_representation_type_id": entity_type_id,
            "entity_representation_concept_id": concept_id,
            "entity_representation_reused_existing": False,
            "entity_representation_name": entity_name,
            **dict(requested_fact_coverage),
            "response_text": response_text,
        },
    )


def register_entity_representation_actions(registry: ActionRegistry) -> None:
    actions = [
        ActionSpec(
            action_id=ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID,
            handler=_handle_materialise_from_payload,
            description=(
                "Additively materialise and verify a typed entity core from "
                "structured payload fields while preserving requested non-core "
                "facts as explicit unresolved postconditions. If an exact "
                "existing instance is encountered, return a read-only "
                "idempotence handoff for represented workflow readback instead "
                "of mutating it."
            ),
            side_effects="write",
        ),
    ]
    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            pass


__all__ = [
    "ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID",
    "register_entity_representation_actions",
]
