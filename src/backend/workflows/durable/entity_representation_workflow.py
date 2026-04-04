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


def _resolve_actor_context(request: WorkflowActionRequest) -> tuple[str | None, str | None]:
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

    entity_type_id = _first_non_empty_text(
        request.inputs.get("entity_type_id"),
        request.data.get("entity_type_id"),
    ) or _ENTITY_DOMAIN_DEFAULT_TYPE_IDS[entity_domain]

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

    verified_ids = _find_verified_named_instance_concept_ids(
        concept_name=entity_name,
        instance_of_type_id=entity_type_id,
    )
    if len(verified_ids) > 1:
        return WorkflowActionResult(
            status="failed",
            error="entity_representation_entity_name_ambiguous",
            outputs={
                "response_text": (
                    f"I found multiple {entity_domain} concepts named "
                    f"'{entity_name}'. Please clarify which one you mean."
                )
            },
        )

    user_concept_id, org_concept_id = _resolve_actor_context(request)
    concept_id: str
    reused_existing = False
    if verified_ids:
        concept_id = verified_ids[0]
        reused_existing = True
    else:
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
                concept_id = verified_ids[0]
                reused_existing = True
            else:
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

    verb = "Updated" if reused_existing else "Represented"
    response_text = f"{verb} {entity_domain} '{entity_name}' as {concept_id}."
    return WorkflowActionResult(
        status="success",
        outputs={
            "entity_representation_materialised": True,
            "entity_representation_verified": True,
            "entity_representation_domain": entity_domain,
            "entity_representation_type_id": entity_type_id,
            "entity_representation_concept_id": concept_id,
            "entity_representation_reused_existing": reused_existing,
            "entity_representation_name": entity_name,
            "response_text": response_text,
        },
    )


def register_entity_representation_actions(registry: ActionRegistry) -> None:
    actions = [
        ActionSpec(
            action_id=ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID,
            handler=_handle_materialise_from_payload,
            description=(
                "Materialise or reuse a typed entity concept from structured "
                "entity-representation payload fields."
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
