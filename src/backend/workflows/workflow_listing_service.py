from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..security.access_control import (
    filter_accessible_concept_ids,
    should_enforce_access_control,
)

from .vontology_loader import (
    resolve_workflow_background_launch_policy,
    resolve_workflow_description,
    resolve_workflow_initial_step,
)
from .workflow_definition_identity_service import (
    WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION,
    WORKFLOW_DEFINITION_IDENTITY_VERSION,
    build_workflow_definition_identity,
)
from .workflow_description_quality_service import (
    assess_workflow_description_quality,
)
from .workflow_registry import WorkflowRegistry

_PENDING_DEFINITION_IDENTITY_REASON = "lazy_definition_not_loaded"
_PENDING_DEFINITION_IDENTITY_BUILD_STATE = "pending_lazy_definition"
WORKFLOW_LISTING_METADATA_RESOLUTION_SCHEMA_VERSION = (
    "workflow_listing_metadata_resolution.v1"
)

_RESTRICTED_WORKFLOW_PLACEHOLDER = "[restricted_workflow]"
_DROP_RESTRICTED_VALUE = object()


def _normalise_workflow_ids(workflow_ids: Iterable[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_workflow_id in workflow_ids:
        workflow_id = (
            raw_workflow_id.strip()
            if isinstance(raw_workflow_id, str)
            else ""
        )
        if not workflow_id or workflow_id in seen:
            continue
        seen.add(workflow_id)
        ordered.append(workflow_id)
    return ordered


def _is_workflow_id_field(field_name: Any) -> bool:
    token = str(field_name or "").strip().lower()
    return token == "workflow_id" or token.endswith("_workflow_id")


def _is_workflow_ids_field(field_name: Any) -> bool:
    token = str(field_name or "").strip().lower()
    return token == "workflow_ids" or token.endswith("_workflow_ids")


def _is_workflow_keyed_field(field_name: Any) -> bool:
    token = str(field_name or "").strip().lower()
    return token.endswith("_by_workflow_id") or token.endswith("_by_workflow_ids")


def _collect_parity_inventory_workflow_ids(
    value: Any,
    *,
    field_name: Any = None,
    collected: list[str],
) -> None:
    """Collect only schema-labelled workflow IDs from parity inventory data."""

    if _is_workflow_keyed_field(field_name) and isinstance(value, Mapping):
        collected.extend(
            key.strip()
            for key in value
            if isinstance(key, str) and key.strip().startswith("#")
        )
    elif _is_workflow_ids_field(field_name):
        if isinstance(value, (list, tuple, set, frozenset)):
            collected.extend(
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip().startswith("#")
            )
    elif _is_workflow_id_field(field_name):
        if isinstance(value, str) and value.strip().startswith("#"):
            collected.append(value.strip())

    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect_parity_inventory_workflow_ids(
                item,
                field_name=key,
                collected=collected,
            )
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_parity_inventory_workflow_ids(
                item,
                field_name=None,
                collected=collected,
            )


def collect_workflow_introspection_projection_ids(
    workflow_ids: Iterable[Any],
    *,
    parity_inventory: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return registry plus schema-labelled parity-inventory workflow IDs.

    Parity inventory can contain Vontology-only workflows that are absent from
    the current registry.  Those identifiers still require actor projection,
    while adjacent step, predicate, type, and tool concept IDs must not be
    mistaken for workflow visibility subjects.
    """

    collected = list(workflow_ids)
    if isinstance(parity_inventory, Mapping):
        _collect_parity_inventory_workflow_ids(
            parity_inventory,
            collected=collected,
        )
    return _normalise_workflow_ids(collected)


def filter_workflow_ids_for_current_actor(
    workflow_ids: Iterable[Any],
) -> list[str]:
    """Return workflow IDs visible to the ambient authenticated actor.

    Registry and capability-index contents are process-global acceleration
    surfaces, not access authority.  Introspection callers must therefore
    re-check represented workflow IDs against the current access-control
    context even when those global surfaces are already warm.  Actor identity
    is deliberately not accepted as an argument: it comes from the canonical
    request/contextvar authority used by ``filter_accessible_concept_ids``.
    """

    ordered_ids = _normalise_workflow_ids(workflow_ids)
    if not ordered_ids or not should_enforce_access_control():
        return ordered_ids

    # Generic concept access historically fails open when the concept store is
    # absent so non-authority UI surfaces can degrade. A warm process-global
    # workflow registry must never inherit that behaviour: without Vontology
    # visibility authority, actor-scoped workflow enumeration fails closed.
    concept_ids = [item for item in ordered_ids if item.startswith("#")]
    try:
        from ..db.mongo_client import get_concepts_collection

        if get_concepts_collection() is None:
            return []
        accessible_ids = filter_accessible_concept_ids(concept_ids)
    except Exception:
        return []

    # A represented workflow missing from the concept authority is hidden on
    # actor-scoped introspection paths.  This fail-closed behaviour also avoids
    # treating a warm registry entry as proof of current visibility.
    return [
        workflow_id
        for workflow_id in ordered_ids
        if workflow_id.startswith("#") and workflow_id in accessible_ids
    ]


def _redact_restricted_workflow_references(
    value: Any,
    *,
    restricted_ids: set[str],
) -> Any:
    if isinstance(value, Mapping):
        entity_id = value.get("workflow_id") or value.get("concept_id")
        if isinstance(entity_id, str) and entity_id in restricted_ids:
            return _DROP_RESTRICTED_VALUE
        projected: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in restricted_ids:
                continue
            projected_item = _redact_restricted_workflow_references(
                item,
                restricted_ids=restricted_ids,
            )
            if projected_item is _DROP_RESTRICTED_VALUE:
                continue
            projected[key] = projected_item
        return projected
    if isinstance(value, (list, tuple, set, frozenset)):
        projected_items: list[Any] = []
        for item in value:
            projected_item = _redact_restricted_workflow_references(
                item,
                restricted_ids=restricted_ids,
            )
            if projected_item is _DROP_RESTRICTED_VALUE:
                continue
            projected_items.append(projected_item)
        return projected_items
    if isinstance(value, str):
        if value in restricted_ids:
            return _DROP_RESTRICTED_VALUE
        projected_text = value
        for restricted_id in sorted(restricted_ids, key=len, reverse=True):
            if restricted_id in projected_text:
                projected_text = projected_text.replace(
                    restricted_id,
                    _RESTRICTED_WORKFLOW_PLACEHOLDER,
                )
        return projected_text
    return value


def project_workflow_introspection_payload_for_current_actor(
    payload: Mapping[str, Any],
    *,
    workflow_ids: Iterable[Any] = (),
    total_workflow_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Remove inaccessible represented metadata from an introspection payload.

    This second projection is intentionally safe to apply to cached payloads:
    it prevents an actor from receiving workflow IDs or nested inventory/index
    metadata prepared under a different actor or under unscoped process-global
    authority.
    """

    if not should_enforce_access_control():
        return dict(payload)

    parity_inventory_payload = payload.get("parity_inventory")
    explicit_workflow_ids = collect_workflow_introspection_projection_ids(
        workflow_ids,
        parity_inventory=(
            parity_inventory_payload
            if isinstance(parity_inventory_payload, Mapping)
            else None
        ),
    )
    if not explicit_workflow_ids:
        return dict(payload)
    total_reference_ids = _normalise_workflow_ids(
        workflow_ids if total_workflow_ids is None else total_workflow_ids
    )

    accessible_references = set(
        filter_workflow_ids_for_current_actor(explicit_workflow_ids)
    )
    restricted_ids = set(explicit_workflow_ids) - accessible_references
    projected = _redact_restricted_workflow_references(
        payload,
        restricted_ids=restricted_ids,
    )
    if not isinstance(projected, dict):
        projected = {}

    for collection_key in ("definitions", "items"):
        collection = projected.get(collection_key)
        if isinstance(collection, list):
            projected["count"] = len(collection)

    if total_reference_ids and "total" in projected:
        projected["total"] = len(
            [
                workflow_id
                for workflow_id in total_reference_ids
                if workflow_id in accessible_references
            ]
        )

    parity_inventory = projected.get("parity_inventory")
    if isinstance(parity_inventory, dict):
        parity_inventory.pop("summary_lines", None)
        parity_inventory.pop("summary_text", None)
        if restricted_ids:
            def _visible_id_count(container: Any, key: str) -> int:
                values = container.get(key) if isinstance(container, Mapping) else None
                if not isinstance(values, list):
                    return 0
                return len(
                    [item for item in values if isinstance(item, str) and item]
                )

            representation = parity_inventory.get("representation")
            parity_inventory["counts"] = {
                "registry": _visible_id_count(
                    parity_inventory,
                    "registry_workflow_ids",
                ),
                "vontology_discovered": _visible_id_count(
                    parity_inventory,
                    "vontology_discovered_workflow_ids",
                ),
                "overlap": _visible_id_count(
                    parity_inventory,
                    "overlap_workflow_ids",
                ),
                "registry_only": _visible_id_count(
                    parity_inventory,
                    "registry_only_workflow_ids",
                ),
                "vontology_only": _visible_id_count(
                    parity_inventory,
                    "vontology_only_workflow_ids",
                ),
                "graph_complete": _visible_id_count(
                    representation,
                    "graph_complete_workflow_ids",
                ),
                "identity_only": _visible_id_count(
                    representation,
                    "identity_only_workflow_ids",
                ),
            }

            registry_sources = parity_inventory.get("registry_sources")
            if isinstance(registry_sources, dict):
                source_by_workflow_id = registry_sources.get(
                    "source_by_workflow_id"
                )
                visible_source_counts: dict[str, int] = {}
                if isinstance(source_by_workflow_id, Mapping):
                    for source in source_by_workflow_id.values():
                        source_name = str(source or "unknown").strip() or "unknown"
                        visible_source_counts[source_name] = (
                            visible_source_counts.get(source_name, 0) + 1
                        )
                registry_sources["counts"] = visible_source_counts

            # These sections contain process-global aggregate counts and drift
            # signals that cannot be safely partitioned after a restricted ID
            # has been removed. Keep visible per-workflow parity facts, but do
            # not let aggregate differences enumerate hidden workflows.
            for aggregate_key in (
                "workflow_description_quality",
                "workflow_authority",
                "workflow_purity",
                "parity_policy",
            ):
                parity_inventory.pop(aggregate_key, None)

            visible_reason_codes = [
                reason_code
                for reason_code, count_key in (
                    ("registry_only", "registry_only"),
                    ("vontology_only", "vontology_only"),
                    ("identity_only", "identity_only"),
                )
                if parity_inventory["counts"].get(count_key, 0) > 0
            ]
            parity_inventory["diagnostics"] = {
                "drift_detected": bool(visible_reason_codes),
                "severity": "warning" if visible_reason_codes else "ok",
                "reason_codes": visible_reason_codes,
                "actor_scoped_projection": True,
            }
        parity_inventory["visibility_projection"] = {
            "actor_scoped": True,
            "visible_workflow_count": len(
                [
                    workflow_id
                    for workflow_id in explicit_workflow_ids
                    if workflow_id in accessible_references
                ]
            ),
        }

    return projected


def build_pending_workflow_definition_identity(
    *,
    workflow_id: str,
    source: str,
) -> dict[str, Any]:
    """Return a stable placeholder until a lazy workflow definition is loaded."""

    return {
        "schema_version": WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION,
        "version": WORKFLOW_DEFINITION_IDENTITY_VERSION,
        "workflow_id": str(workflow_id or "").strip(),
        "source": str(source or "unknown").strip() or "unknown",
        "definition_hash": None,
        "runtime_definition_hash": None,
        "authoritative_definition_hash": None,
        "hash_mismatch": False,
        "state_count": 0,
        "action_count": 0,
        "build_state": _PENDING_DEFINITION_IDENTITY_BUILD_STATE,
        "reason_code": _PENDING_DEFINITION_IDENTITY_REASON,
    }


def build_workflow_listing_entry(
    *,
    registry: WorkflowRegistry,
    workflow_id: str,
    resolve_vontology_metadata: bool = True,
    metadata_mode: str | None = None,
    metadata_reason_code: str | None = None,
) -> dict[str, Any]:
    """Build a workflow-listing summary without forcing lazy definition loads."""

    registration = registry.peek_registration(workflow_id)
    definition = getattr(registration, "definition", None)

    source = registry.get_registration_source(
        workflow_id,
        resolve_lazy=False,
    ) or str(getattr(registration, "source", "") or "").strip() or "unknown"

    registration_purpose = getattr(registration, "purpose", None)
    definition_purpose = getattr(definition, "purpose", "") if definition is not None else None
    description = ""
    description_source = "none"
    if resolve_vontology_metadata:
        description, description_source = resolve_workflow_description(
            workflow_id,
            workflow_source=source,
            registration_purpose=registration_purpose,
            definition_purpose=definition_purpose,
        )
    else:
        if isinstance(registration_purpose, str) and registration_purpose.strip():
            description = registration_purpose.strip()
            description_source = "registration.purpose"
        elif isinstance(definition_purpose, str) and definition_purpose.strip():
            description = definition_purpose.strip()
            description_source = "definition.purpose"
    description_quality = assess_workflow_description_quality(
        description=description,
        description_source=description_source,
    )

    initial_state = ""
    if definition is not None:
        initial_state = str(getattr(definition, "initial_state", "") or "").strip()
    if resolve_vontology_metadata and not initial_state:
        initial_state = str(resolve_workflow_initial_step(workflow_id) or "").strip()

    background_launch_policy = None
    background_launch_policy_source = "none"
    definition_metadata = getattr(definition, "metadata", None)
    if isinstance(definition_metadata, Mapping):
        policy_from_definition = definition_metadata.get("background_launch_policy")
        if isinstance(policy_from_definition, dict):
            background_launch_policy = dict(policy_from_definition)
            background_launch_policy_source = str(
                definition_metadata.get("background_launch_policy_source")
                or "definition.metadata"
            )
    if resolve_vontology_metadata and background_launch_policy is None:
        (
            background_launch_policy,
            background_launch_policy_source,
        ) = resolve_workflow_background_launch_policy(workflow_id)

    if definition is not None:
        definition_identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=source,
            definition=definition,
            authoritative_definition=definition if source.lower() == "vontology" else None,
        )
    else:
        definition_identity = build_pending_workflow_definition_identity(
            workflow_id=workflow_id,
            source=source,
        )

    effective_metadata_mode = (
        "authoritative" if bool(resolve_vontology_metadata) else "fast"
    )
    requested_metadata_mode = str(metadata_mode or effective_metadata_mode).strip()
    if not requested_metadata_mode:
        requested_metadata_mode = effective_metadata_mode
    reason_code = str(metadata_reason_code or "").strip()
    if not reason_code:
        reason_code = (
            "vontology_metadata_resolved"
            if bool(resolve_vontology_metadata)
            else "fast_registration_metadata_only"
        )

    return {
        "workflow_id": workflow_id,
        "description": description,
        "description_source": description_source,
        "description_quality": description_quality,
        "initial_state": initial_state,
        "source": source,
        "background_launch_policy": background_launch_policy,
        "background_launch_policy_source": background_launch_policy_source,
        "definition_identity": definition_identity,
        "definition_loaded": definition is not None,
        "metadata_resolution": {
            "schema_version": WORKFLOW_LISTING_METADATA_RESOLUTION_SCHEMA_VERSION,
            "requested_mode": requested_metadata_mode,
            "effective_mode": effective_metadata_mode,
            "resolved_vontology_metadata": bool(resolve_vontology_metadata),
            "definition_loaded": definition is not None,
            "description_source": description_source,
            "reason_code": reason_code,
        },
    }


__all__ = [
    "WORKFLOW_LISTING_METADATA_RESOLUTION_SCHEMA_VERSION",
    "build_pending_workflow_definition_identity",
    "build_workflow_listing_entry",
    "collect_workflow_introspection_projection_ids",
    "filter_workflow_ids_for_current_actor",
    "project_workflow_introspection_payload_for_current_actor",
]
