"""Vontology-first helpers for context bundles, dossiers, and workspaces.

This service provides the reusable support surface requested by
JVNAUTOSCI-254 / 1563 / 1564:

- canonical ontology bootstrap for context-bundle artefacts
- effective-context resolution for workflow and concept inheritance
- hybrid dossier/report-revision persistence that can reuse testing-theory state
- bounded reconstructed workspace assembly with inspectable telemetry
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id_exact
from .context_bundle_contracts import (
    CITES_EVIDENCE_RECEIPT_PREDICATE_ID,
    CONCEPT_CONTEXT_BUNDLE_TYPE_ID,
    CONTEXT_BUNDLE_EXTENDS_PREDICATE_ID,
    CONTEXT_BUNDLE_SCHEMA_VERSION,
    CONTEXT_BUNDLE_TYPE_ID,
    CONTEXT_DOSSIER_BRANCH_KIND_THEORY_LOCAL,
    CONTEXT_DOSSIER_BRANCH_KINDS,
    CONTEXT_DOSSIER_SCHEMA_VERSION,
    CONTEXT_DOSSIER_TYPE_ID,
    CONTEXT_FACET_SCHEMA_VERSION,
    CONTEXT_FACET_TYPE_ID,
    EVIDENCE_RECEIPT_SCHEMA_VERSION,
    EVIDENCE_RECEIPT_TYPE_ID,
    HAS_CONTEXT_BUNDLE_PREDICATE_ID,
    HAS_CONTEXT_DOSSIER_PREDICATE_ID,
    HAS_CONTEXT_FACET_PREDICATE_ID,
    HAS_REPORT_REVISION_PREDICATE_ID,
    RECONSTRUCTED_WORKSPACE_SCHEMA_VERSION,
    WORKFLOW_CONTEXT_BUNDLE_TYPE_ID,
    WORKFLOW_REPORT_REVISION_SCHEMA_VERSION,
    WORKFLOW_REPORT_REVISION_TYPE_ID,
    WORKSPACE_KEY_CONTEXT_DOSSIER,
    WORKSPACE_KEY_CONTEXT_DOSSIER_ID,
    WORKSPACE_KEY_EFFECTIVE_CONTEXT_BUNDLE_IDS,
    WORKSPACE_KEY_EFFECTIVE_CONTEXT_FACET_IDS,
    WORKSPACE_KEY_EVIDENCE_RECEIPTS,
    WORKSPACE_KEY_IMMEDIATE_CONTEXT,
    WORKSPACE_KEY_INTERACTION_BUDGET,
    WORKSPACE_KEY_OPEN_QUESTIONS,
    WORKSPACE_KEY_QUESTION,
    WORKSPACE_KEY_REPORT_REVISION,
    WORKSPACE_KEY_REPORT_REVISION_ID,
    WORKSPACE_KEY_SEARCH_HISTORY,
    WORKSPACE_KEY_TASK,
    WORKSPACE_KEY_TERMINATION_STATUS,
    WORKSPACE_KEY_WORKSPACE_TELEMETRY,
)
from .relationship_write_service import add_relationship
from .testing_theory_service import get_testing_theory_state
from .text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from .workflow_vontology_materialisation_helpers import stable_named_instance_concept_id

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_MAX_OPEN_QUESTIONS = 12
DEFAULT_WORKSPACE_MAX_EVIDENCE_RECEIPTS = 20
DEFAULT_WORKSPACE_MAX_SEARCH_HISTORY = 12
DEFAULT_WORKSPACE_MAX_BRANCHES = 8
DEFAULT_WORKSPACE_MAX_PROMOTION_CANDIDATES = 20
DEFAULT_WORKSPACE_MAX_LOCAL_ASSERTIONS = 40
DEFAULT_WORKSPACE_MAX_HYPOTHESES = 20
DEFAULT_WORKSPACE_MAX_IMMEDIATE_CONTEXT_CHARS = 12000
DEFAULT_REPORT_TEXT_MAX_CHARS = 32000

_CANONICAL_CONTEXT_ONTOLOGY_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "concept_id": CONTEXT_BUNDLE_TYPE_ID,
        "name": "Context Bundle",
        "description": (
            "A bounded, inheritable context artefact that attaches reusable graph-"
            "first working context to concepts or workflows."
        ),
        "parent_concept_ids": ["#V#thing"],
        "create_as_instance": False,
    },
    {
        "concept_id": WORKFLOW_CONTEXT_BUNDLE_TYPE_ID,
        "name": "Workflow Context Bundle",
        "description": (
            "Subtype of context bundle for workflow-scoped inherited context."
        ),
        "parent_concept_ids": [CONTEXT_BUNDLE_TYPE_ID],
        "create_as_instance": False,
    },
    {
        "concept_id": CONCEPT_CONTEXT_BUNDLE_TYPE_ID,
        "name": "Concept Context Bundle",
        "description": (
            "Subtype of context bundle for concept-scoped inherited context."
        ),
        "parent_concept_ids": [CONTEXT_BUNDLE_TYPE_ID],
        "create_as_instance": False,
    },
    {
        "concept_id": CONTEXT_FACET_TYPE_ID,
        "name": "Context Facet",
        "description": (
            "A bounded facet inside a context bundle or dossier, typically carrying "
            "one evidence-bearing slice of context."
        ),
        "parent_concept_ids": ["#V#thing"],
        "create_as_instance": False,
    },
    {
        "concept_id": CONTEXT_DOSSIER_TYPE_ID,
        "name": "Context Dossier",
        "description": (
            "A hybrid working artefact combining report text, evidence receipts, "
            "theory-local state, open questions, and promotion candidates."
        ),
        "parent_concept_ids": ["#V#thing"],
        "create_as_instance": False,
    },
    {
        "concept_id": WORKFLOW_REPORT_REVISION_TYPE_ID,
        "name": "Workflow Report Revision",
        "description": (
            "A bounded report revision linked to a dossier and backed by explicit "
            "evidence receipts."
        ),
        "parent_concept_ids": ["#V#thing"],
        "create_as_instance": False,
    },
    {
        "concept_id": EVIDENCE_RECEIPT_TYPE_ID,
        "name": "Evidence Receipt",
        "description": (
            "A hashed, provenance-bearing receipt describing the bounded evidence "
            "used to support a context facet, dossier, or report revision."
        ),
        "parent_concept_ids": ["#V#thing"],
        "create_as_instance": False,
    },
    {
        "concept_id": HAS_CONTEXT_BUNDLE_PREDICATE_ID,
        "name": "Has Context Bundle",
        "description": "Links a concept, workflow, or dossier to an attached context bundle.",
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    },
    {
        "concept_id": CONTEXT_BUNDLE_EXTENDS_PREDICATE_ID,
        "name": "Context Bundle Extends",
        "description": (
            "Links a context bundle to a more general bundle it inherits from."
        ),
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    },
    {
        "concept_id": HAS_CONTEXT_FACET_PREDICATE_ID,
        "name": "Has Context Facet",
        "description": "Links a bundle or dossier to one of its bounded context facets.",
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    },
    {
        "concept_id": HAS_CONTEXT_DOSSIER_PREDICATE_ID,
        "name": "Has Context Dossier",
        "description": "Links a subject concept or workflow to a reconstructed dossier.",
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    },
    {
        "concept_id": HAS_REPORT_REVISION_PREDICATE_ID,
        "name": "Has Report Revision",
        "description": "Links a dossier to one of its bounded report revisions.",
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    },
    {
        "concept_id": CITES_EVIDENCE_RECEIPT_PREDICATE_ID,
        "name": "Cites Evidence Receipt",
        "description": (
            "Links a bundle, facet, dossier, or report revision to the evidence "
            "receipts it depends on."
        ),
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    },
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _safe_str(value: Any, *, limit: int | None = None) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    if limit is not None:
        return text[:limit]
    return text


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalise_strings(values: Any, *, limit: int | None = None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if limit is not None and len(items) >= limit:
            break
    return items


def _clone_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _truncate_text(value: Any, *, maximum: int) -> tuple[str | None, bool]:
    text = _safe_str(value)
    if not text:
        return None, False
    if len(text) <= maximum:
        return text, False
    return text[:maximum], True


def _json_default(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _hash_payload(value: Any, *, length: int = 16) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _bounded_copy(value: Any, *, max_string_chars: int = 2000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_string_chars]
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_copy(item, max_string_chars=max_string_chars)
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_copy(item, max_string_chars=max_string_chars)
            for item in list(value)[:80]
        ]
    return str(value)[:max_string_chars]


def _structural_target_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _safe_str(value)
        return [text] if text else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _safe_str(item)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _list_relationship_targets(
    concept: Mapping[str, Any] | None,
    predicate: str,
) -> list[str]:
    if not isinstance(concept, Mapping):
        return []
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        return []
    return _normalise_strings(
        [
            *_structural_target_strings(relationships.get(predicate)),
            *_structural_target_strings(relationships.get(predicate.removeprefix("#V#"))),
        ]
    )


def _get_concept_or_none(concept_id: str) -> Mapping[str, Any] | None:
    resolved = _safe_str(concept_id)
    if not resolved:
        return None
    try:
        concept = get_concept_by_concept_id_exact(resolved)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _ensure_text_description(concept_id: str, description: str) -> None:
    if not description:
        return
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={"source": "context_bundle_service", "reason": "ontology_bootstrap"},
        garbage_collect=True,
    )


def _get_singleton_text_value(
    concept_id: str,
    *,
    predicate_preferences: Sequence[str] = ("hasContent", "#V#hasContent"),
    limit: int = 20,
) -> str | None:
    try:
        rows = list(get_texts_for_concept(concept_id, limit=limit) or [])
    except Exception as exc:
        logger.debug(
            "[context_bundle] could not load text rows for %s: %s",
            concept_id,
            exc,
        )
        return None

    preferred = {item.strip().lower() for item in predicate_preferences if item}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        predicate = _safe_str(row.get("predicate")).lower()
        text = _safe_str(row.get("text"))
        if predicate in preferred and text:
            return text
    return None


def canonical_context_bundle_concept_ids() -> tuple[str, ...]:
    return tuple(
        _safe_str(item.get("concept_id")) for item in _CANONICAL_CONTEXT_ONTOLOGY_BLUEPRINTS
    )


def context_bundle_ontology_blueprints() -> tuple[dict[str, Any], ...]:
    return tuple(dict(item) for item in _CANONICAL_CONTEXT_ONTOLOGY_BLUEPRINTS)


def bootstrap_canonical_context_bundle_ontology(
    *,
    concept_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    requested = _normalise_strings(concept_ids) or list(
        canonical_context_bundle_concept_ids()
    )
    persisted: list[str] = []
    missing: list[str] = []
    for concept_id in requested:
        blueprint = next(
            (
                item
                for item in _CANONICAL_CONTEXT_ONTOLOGY_BLUEPRINTS
                if item.get("concept_id") == concept_id
            ),
            None,
        )
        if not isinstance(blueprint, Mapping):
            missing.append(concept_id)
            continue
        concept = _get_concept_or_none(concept_id)
        if not isinstance(concept, Mapping):
            missing.append(concept_id)
            continue
        _ensure_text_description(concept_id, _safe_str(blueprint.get("description")))
        persisted.append(concept_id)
    return {
        "success": not missing,
        "requested_concept_ids": requested,
        "persisted_profile_concept_ids": persisted,
        "missing_concept_ids": missing,
        "counts": {
            "requested": len(requested),
            "persisted": len(persisted),
            "missing": len(missing),
        },
    }


def ensure_canonical_context_bundle_ontology(
    *,
    concept_ids: Sequence[str] | None = None,
    create_missing_concepts: bool = True,
) -> dict[str, Any]:
    requested = _normalise_strings(concept_ids) or list(
        canonical_context_bundle_concept_ids()
    )
    created_ids: list[str] = []
    persisted_ids: list[str] = []
    repaired_parent_link_ids: list[str] = []
    missing_ids: list[str] = []
    unknown_ids: list[str] = []
    errors: dict[str, str] = {}

    for concept_id in requested:
        blueprint = next(
            (
                item
                for item in _CANONICAL_CONTEXT_ONTOLOGY_BLUEPRINTS
                if item.get("concept_id") == concept_id
            ),
            None,
        )
        if not isinstance(blueprint, Mapping):
            unknown_ids.append(concept_id)
            continue

        concept = _get_concept_or_none(concept_id)
        if not isinstance(concept, Mapping):
            if not create_missing_concepts:
                missing_ids.append(concept_id)
                continue
            try:
                concept_service.create_concept(
                    name=_safe_str(blueprint.get("name")) or concept_id,
                    concept_id=concept_id,
                    description=_safe_str(blueprint.get("description")) or None,
                    parent_concept_ids=_normalise_strings(
                        blueprint.get("parent_concept_ids")
                    ),
                    create_as_instance=bool(blueprint.get("create_as_instance", False)),
                    visibility_scope_mode="global_general",
                )
                created_ids.append(concept_id)
            except Exception as exc:
                errors[concept_id] = f"create_failed:{exc}"
                continue

        relation_kind = (
            "is_an_instance_of"
            if bool(blueprint.get("create_as_instance", False))
            else "is_a_type_of"
        )
        expected_parent_ids = _normalise_strings(blueprint.get("parent_concept_ids"))
        current_parent_ids = set(_list_relationship_targets(concept, relation_kind))
        for parent_id in expected_parent_ids:
            if parent_id in current_parent_ids:
                continue
            try:
                add_relationship(
                    source_id=concept_id,
                    predicate=relation_kind,
                    target=parent_id,
                )
                repaired_parent_link_ids.append(
                    f"{concept_id}->{relation_kind}->{parent_id}"
                )
            except Exception as exc:
                errors[concept_id] = (
                    f"parent_link_failed:{relation_kind}:{parent_id}:{exc}"
                )
                break

        try:
            _ensure_text_description(
                concept_id,
                _safe_str(blueprint.get("description")),
            )
            persisted_ids.append(concept_id)
        except Exception as exc:
            errors[concept_id] = f"description_upsert_failed:{exc}"

    return {
        "success": not unknown_ids and not missing_ids and not errors,
        "requested_concept_ids": requested,
        "canonical_concept_ids": list(canonical_context_bundle_concept_ids()),
        "created_concept_ids": created_ids,
        "persisted_concept_ids": persisted_ids,
        "repaired_parent_links": repaired_parent_link_ids,
        "missing_concept_ids": missing_ids,
        "unknown_concept_ids": unknown_ids,
        "errors_by_concept_id": errors,
        "counts": {
            "requested": len(requested),
            "created": len(created_ids),
            "persisted": len(persisted_ids),
            "repaired_parent_links": len(repaired_parent_link_ids),
            "missing": len(missing_ids),
            "unknown": len(unknown_ids),
            "errors": len(errors),
        },
    }


def _persist_context_state(
    *,
    concept_id: str,
    attribute_prefix: str,
    state_key: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(state)
    concept_service.update_concept(
        concept_id,
        {
            f"concept_data.{state_key}": payload,
            f"attributes.{attribute_prefix}.updated_at_utc": payload.get("updated_at_utc"),
            f"attributes.{attribute_prefix}.schema_version": payload.get(
                "schema_version"
            ),
        },
    )
    return payload


def _ensure_context_instance(
    *,
    concept_id: str,
    name: str,
    description: str | None,
    type_id: str,
    namespace: str | None,
    user_id: str | None,
    org_id: str | None,
    visibility_scope_mode: str | None = None,
) -> None:
    if _get_concept_or_none(concept_id) is not None:
        return
    concept_service.create_concept(
        name=name,
        concept_id=concept_id,
        description=description,
        parent_concept_ids=[type_id],
        create_as_instance=True,
        created_by_concept_id=_safe_str(user_id) or None,
        organisation_concept_id=_safe_str(org_id) or None,
        event_namespace=_safe_str(namespace) or None,
        visibility_scope_mode=_safe_str(visibility_scope_mode) or None,
    )
    if description:
        _ensure_text_description(concept_id, description)


def load_context_bundle_state(bundle_id: str) -> dict[str, Any] | None:
    concept = _get_concept_or_none(bundle_id)
    if not isinstance(concept, Mapping):
        return None
    state = (concept.get("concept_data") or {}).get("context_bundle")
    return dict(state) if isinstance(state, Mapping) else None


def load_context_facet_state(facet_id: str) -> dict[str, Any] | None:
    concept = _get_concept_or_none(facet_id)
    if not isinstance(concept, Mapping):
        return None
    state = (concept.get("concept_data") or {}).get("context_facet")
    if not isinstance(state, Mapping):
        return None
    payload = dict(state)
    content = _get_singleton_text_value(facet_id)
    if content:
        payload["content"] = content
    return payload


def load_context_dossier_state(dossier_id: str) -> dict[str, Any] | None:
    concept = _get_concept_or_none(dossier_id)
    if not isinstance(concept, Mapping):
        return None
    state = (concept.get("concept_data") or {}).get("context_dossier")
    return dict(state) if isinstance(state, Mapping) else None


def list_attached_context_dossier_ids(subject_id: str) -> list[str]:
    resolved_subject_id = _safe_str(subject_id)
    if not resolved_subject_id:
        return []
    subject = _get_concept_or_none(resolved_subject_id)
    return _normalise_strings(
        _list_relationship_targets(subject, HAS_CONTEXT_DOSSIER_PREDICATE_ID)
    )


def load_workflow_report_revision_state(revision_id: str) -> dict[str, Any] | None:
    concept = _get_concept_or_none(revision_id)
    if not isinstance(concept, Mapping):
        return None
    state = (concept.get("concept_data") or {}).get("workflow_report_revision")
    if not isinstance(state, Mapping):
        return None
    payload = dict(state)
    report_text = _get_singleton_text_value(revision_id)
    if report_text:
        payload["report_text"] = report_text
    return payload


def load_evidence_receipt_state(receipt_id: str) -> dict[str, Any] | None:
    concept = _get_concept_or_none(receipt_id)
    if not isinstance(concept, Mapping):
        return None
    state = (concept.get("concept_data") or {}).get("evidence_receipt")
    return dict(state) if isinstance(state, Mapping) else None


def _normalise_evidence_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload = _clone_mapping(raw)
    payload["schema_version"] = (
        _safe_str(payload.get("schema_version")) or EVIDENCE_RECEIPT_SCHEMA_VERSION
    )
    payload["receipt_hash"] = _safe_str(payload.get("receipt_hash")) or _hash_payload(
        payload, length=24
    )
    payload["source_system"] = _safe_str(payload.get("source_system")) or "unknown"
    payload["locator"] = _clone_mapping(payload.get("locator"))
    return payload


def persist_evidence_receipt(
    *,
    receipt: Mapping[str, Any],
    name: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    normalised = _normalise_evidence_receipt(receipt)
    receipt_hash = _safe_str(normalised.get("receipt_hash"))
    receipt_id = f"#V#evidence_receipt_{receipt_hash}"
    label = _safe_str(name) or f"Evidence receipt {receipt_hash[:8]}"
    description = (
        f"Evidence receipt for {normalised.get('source_system')} "
        f"({receipt_hash[:12]})."
    )

    _ensure_context_instance(
        concept_id=receipt_id,
        name=label,
        description=description,
        type_id=EVIDENCE_RECEIPT_TYPE_ID,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        visibility_scope_mode=None,
    )
    state = {
        "schema_version": EVIDENCE_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "receipt_hash": receipt_hash,
        "source_system": _safe_str(normalised.get("source_system")),
        "locator": _clone_mapping(normalised.get("locator")),
        "payload": normalised,
        "updated_at_utc": _utcnow_iso(),
    }
    persisted = _persist_context_state(
        concept_id=receipt_id,
        attribute_prefix="evidence_receipt",
        state_key="evidence_receipt",
        state=state,
    )
    return {"success": True, "receipt_id": receipt_id, "evidence_receipt": persisted}


def create_or_update_context_bundle(
    *,
    name: str,
    bundle_id: str | None = None,
    description: str | None = None,
    bundle_type_id: str | None = None,
    subject_id: str | None = None,
    subject_kind: str | None = None,
    extends_bundle_ids: Sequence[str] = (),
    facets: Sequence[Mapping[str, Any]] = (),
    bundle_policy: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    ensure_canonical_context_bundle_ontology()

    resolved_name = _safe_str(name, limit=200) or "Context bundle"
    resolved_bundle_id = _safe_str(bundle_id) or stable_named_instance_concept_id(
        resolved_name,
        prefix="context_bundle",
    )
    resolved_type_id = _safe_str(bundle_type_id) or CONTEXT_BUNDLE_TYPE_ID
    resolved_description = _safe_str(description, limit=1000) or (
        f"Context bundle for {resolved_name}."
    )

    _ensure_context_instance(
        concept_id=resolved_bundle_id,
        name=resolved_name,
        description=resolved_description,
        type_id=resolved_type_id,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        visibility_scope_mode=None,
    )

    facet_ids: list[str] = []
    persisted_facets: list[dict[str, Any]] = []
    receipt_ids: list[str] = []
    for index, raw_facet in enumerate(list(facets or []), start=1):
        if not isinstance(raw_facet, Mapping):
            continue
        facet_name = _safe_str(raw_facet.get("name")) or f"{resolved_name} facet {index}"
        facet_id = _safe_str(raw_facet.get("facet_id")) or stable_named_instance_concept_id(
            f"{resolved_bundle_id}:{facet_name}:{index}",
            prefix="context_facet",
        )
        facet_description = _safe_str(raw_facet.get("description"), limit=1000) or (
            f"Context facet '{facet_name}' for {resolved_name}."
        )
        _ensure_context_instance(
            concept_id=facet_id,
            name=facet_name,
            description=facet_description,
            type_id=CONTEXT_FACET_TYPE_ID,
            namespace=namespace,
            user_id=user_id,
            org_id=org_id,
            visibility_scope_mode=None,
        )

        content, content_truncated = _truncate_text(
            raw_facet.get("content"),
            maximum=DEFAULT_WORKSPACE_MAX_IMMEDIATE_CONTEXT_CHARS,
        )
        facet_state = {
            "schema_version": CONTEXT_FACET_SCHEMA_VERSION,
            "facet_id": facet_id,
            "bundle_id": resolved_bundle_id,
            "facet_name": facet_name,
            "facet_kind": _safe_str(raw_facet.get("facet_kind")) or "generic",
            "source_kind": _safe_str(raw_facet.get("source_kind")) or "unknown",
            "source_locator": _clone_mapping(raw_facet.get("source_locator")),
            "metadata": _clone_mapping(raw_facet.get("metadata")),
            "content_truncated": content_truncated,
            "updated_at_utc": _utcnow_iso(),
        }
        _persist_context_state(
            concept_id=facet_id,
            attribute_prefix="context_facet",
            state_key="context_facet",
            state=facet_state,
        )
        if content:
            upsert_singleton_text_relation(
                subject_concept_id=facet_id,
                predicate="hasContent",
                text=content,
                lang="en-NZ",
                context={
                    "source": "context_bundle_service",
                    "reason": "context_facet_content",
                },
                garbage_collect=True,
            )

        facet_ids.append(facet_id)
        persisted_facets.append(facet_state)
        try:
            add_relationship(
                source_id=resolved_bundle_id,
                predicate=HAS_CONTEXT_FACET_PREDICATE_ID,
                target=facet_id,
            )
        except Exception as exc:
            logger.warning(
                "[context_bundle] could not link facet %s to bundle %s: %s",
                facet_id,
                resolved_bundle_id,
                exc,
            )

        raw_receipt = raw_facet.get("evidence_receipt")
        if isinstance(raw_receipt, Mapping):
            receipt_result = persist_evidence_receipt(
                receipt=raw_receipt,
                name=f"{facet_name} evidence receipt",
                namespace=namespace,
                user_id=user_id,
                org_id=org_id,
            )
            receipt_id = _safe_str(receipt_result.get("receipt_id"))
            if receipt_id:
                receipt_ids.append(receipt_id)
                try:
                    add_relationship(
                        source_id=facet_id,
                        predicate=CITES_EVIDENCE_RECEIPT_PREDICATE_ID,
                        target=receipt_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "[context_bundle] could not link receipt %s to facet %s: %s",
                        receipt_id,
                        facet_id,
                        exc,
                    )

    resolved_extends = _normalise_strings(extends_bundle_ids)
    for parent_bundle_id in resolved_extends:
        try:
            add_relationship(
                source_id=resolved_bundle_id,
                predicate=CONTEXT_BUNDLE_EXTENDS_PREDICATE_ID,
                target=parent_bundle_id,
            )
        except Exception as exc:
            logger.warning(
                "[context_bundle] could not link bundle inheritance %s -> %s: %s",
                resolved_bundle_id,
                parent_bundle_id,
                exc,
            )

    if _safe_str(subject_id):
        try:
            add_relationship(
                source_id=_safe_str(subject_id),
                predicate=HAS_CONTEXT_BUNDLE_PREDICATE_ID,
                target=resolved_bundle_id,
            )
        except Exception as exc:
            logger.warning(
                "[context_bundle] could not attach bundle %s to %s: %s",
                resolved_bundle_id,
                subject_id,
                exc,
            )

    state = {
        "schema_version": CONTEXT_BUNDLE_SCHEMA_VERSION,
        "bundle_id": resolved_bundle_id,
        "bundle_type_id": resolved_type_id,
        "bundle_name": resolved_name,
        "subject_id": _safe_str(subject_id) or None,
        "subject_kind": _safe_str(subject_kind) or None,
        "extends_bundle_ids": resolved_extends,
        "facet_ids": facet_ids,
        "bundle_policy": _clone_mapping(bundle_policy),
        "metadata": _clone_mapping(metadata),
        "updated_at_utc": _utcnow_iso(),
    }
    persisted = _persist_context_state(
        concept_id=resolved_bundle_id,
        attribute_prefix="context_bundle",
        state_key="context_bundle",
        state=state,
    )
    return {
        "success": True,
        "bundle_id": resolved_bundle_id,
        "context_bundle": persisted,
        "context_facets": persisted_facets,
        "receipt_ids": receipt_ids,
    }


def _resolve_bundle_inheritance(
    bundle_ids: Sequence[str],
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    ordered: list[str] = []
    inheritance_edges: list[dict[str, Any]] = []
    missing_bundle_ids: list[str] = []
    seen: set[str] = set()

    def _visit(bundle_id: str, *, source: str) -> None:
        resolved = _safe_str(bundle_id)
        if not resolved or resolved in seen:
            return
        seen.add(resolved)
        concept = _get_concept_or_none(resolved)
        state = load_context_bundle_state(resolved)
        if concept is None and state is None:
            missing_bundle_ids.append(resolved)
            return
        ordered.append(resolved)
        parent_ids = _normalise_strings(
            [
                *((state or {}).get("extends_bundle_ids") or []),
                *_list_relationship_targets(concept, CONTEXT_BUNDLE_EXTENDS_PREDICATE_ID),
            ]
        )
        for parent_id in parent_ids:
            inheritance_edges.append(
                {"bundle_id": resolved, "extends_bundle_id": parent_id, "source": source}
            )
            _visit(parent_id, source="bundle_inheritance")

    for requested in bundle_ids:
        _visit(requested, source="requested")
    return ordered, inheritance_edges, missing_bundle_ids


def _collect_type_ancestors(concept_id: str, *, max_depth: int = 6) -> list[dict[str, Any]]:
    concept = _get_concept_or_none(concept_id)
    if not isinstance(concept, Mapping):
        return []

    queue: deque[tuple[str, int]] = deque()
    direct_parents = _normalise_strings(
        [
            *_list_relationship_targets(concept, "is_an_instance_of"),
            *_list_relationship_targets(concept, "is_a_type_of"),
        ]
    )
    for parent_id in direct_parents:
        queue.append((parent_id, 1))

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    while queue:
        parent_id, depth = queue.popleft()
        if parent_id in seen or depth > max_depth:
            continue
        seen.add(parent_id)
        rows.append({"concept_id": parent_id, "depth": depth})
        parent_concept = _get_concept_or_none(parent_id)
        if not isinstance(parent_concept, Mapping):
            continue
        for grandparent_id in _list_relationship_targets(parent_concept, "is_a_type_of"):
            queue.append((grandparent_id, depth + 1))
    return rows


def _merge_bundle_policies(bundle_states: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for state in reversed(list(bundle_states)):
        policy = state.get("bundle_policy")
        if not isinstance(policy, Mapping):
            continue
        for key, value in policy.items():
            merged[str(key)] = copy.deepcopy(value)
    return merged


def resolve_effective_context(
    *,
    subject_kind: str,
    subject_id: str,
    explicit_bundle_ids: Sequence[str] = (),
    workflow_step_bundle_ids: Sequence[str] = (),
    local_default_bundle_ids: Sequence[str] = (),
    include_type_hierarchy: bool = True,
) -> dict[str, Any]:
    resolved_subject_kind = _safe_str(subject_kind).lower()
    resolved_subject_id = _safe_str(subject_id)
    if resolved_subject_kind not in {"concept", "workflow"} or not resolved_subject_id:
        return {
            "success": False,
            "error": "subject_kind_and_subject_id_required",
        }

    subject = _get_concept_or_none(resolved_subject_id)
    direct_bundle_sources: list[dict[str, Any]] = []
    requested_bundle_ids = _normalise_strings(explicit_bundle_ids)

    for bundle_id in _normalise_strings(workflow_step_bundle_ids):
        direct_bundle_sources.append(
            {"bundle_id": bundle_id, "source": "workflow_step", "depth": 0}
        )

    for bundle_id in requested_bundle_ids:
        direct_bundle_sources.append(
            {"bundle_id": bundle_id, "source": "explicit", "depth": 0}
        )

    attached_bundle_ids = _list_relationship_targets(subject, HAS_CONTEXT_BUNDLE_PREDICATE_ID)
    for bundle_id in attached_bundle_ids:
        direct_bundle_sources.append(
            {
                "bundle_id": bundle_id,
                "source": "attached_workflow" if resolved_subject_kind == "workflow" else "attached_concept",
                "depth": 0,
            }
        )

    if include_type_hierarchy and resolved_subject_kind == "concept":
        for ancestor in _collect_type_ancestors(resolved_subject_id):
            ancestor_concept_id = _safe_str(ancestor.get("concept_id"))
            if not ancestor_concept_id:
                continue
            for bundle_id in _list_relationship_targets(
                _get_concept_or_none(ancestor_concept_id),
                HAS_CONTEXT_BUNDLE_PREDICATE_ID,
            ):
                direct_bundle_sources.append(
                    {
                        "bundle_id": bundle_id,
                        "source": "type_ancestor",
                        "depth": _coerce_int(
                            ancestor.get("depth"),
                            default=1,
                            minimum=1,
                            maximum=12,
                        ),
                        "ancestor_concept_id": ancestor_concept_id,
                    }
                )

    for bundle_id in _normalise_strings(local_default_bundle_ids):
        direct_bundle_sources.append(
            {"bundle_id": bundle_id, "source": "local_default", "depth": 99}
        )

    ordered_requested_bundle_ids = _normalise_strings(
        [row.get("bundle_id") for row in direct_bundle_sources]
    )
    effective_bundle_ids, inheritance_edges, missing_bundle_ids = _resolve_bundle_inheritance(
        ordered_requested_bundle_ids
    )

    bundle_states: list[dict[str, Any]] = []
    facet_ids: list[str] = []
    facet_states: list[dict[str, Any]] = []
    missing_facet_ids: list[str] = []
    for bundle_id in effective_bundle_ids:
        state = load_context_bundle_state(bundle_id)
        if not isinstance(state, Mapping):
            bundle_states.append({"bundle_id": bundle_id})
            continue
        bundle_states.append(dict(state))
        for facet_id in _normalise_strings(state.get("facet_ids")):
            if facet_id in facet_ids:
                continue
            if _get_concept_or_none(facet_id) is None:
                missing_facet_ids.append(facet_id)
                continue
            facet_ids.append(facet_id)
            facet_state = load_context_facet_state(facet_id)
            if isinstance(facet_state, Mapping):
                facet_states.append(dict(facet_state))

    bundle_source_trace: list[dict[str, Any]] = []
    seen_trace_pairs: set[tuple[str, str]] = set()
    for row in direct_bundle_sources:
        bundle_id = _safe_str(row.get("bundle_id"))
        source = _safe_str(row.get("source")) or "unknown"
        if not bundle_id or (bundle_id, source) in seen_trace_pairs:
            continue
        seen_trace_pairs.add((bundle_id, source))
        bundle_source_trace.append(
            {
                "bundle_id": bundle_id,
                "source": source,
                "depth": _coerce_int(row.get("depth"), default=0, minimum=0, maximum=99),
                "ancestor_concept_id": _safe_str(row.get("ancestor_concept_id")) or None,
            }
        )

    diagnostics = {
        "subject_id": resolved_subject_id,
        "subject_kind": resolved_subject_kind,
        "bundle_source_trace": bundle_source_trace,
        "inheritance_edges": inheritance_edges,
        "missing_bundle_ids": missing_bundle_ids,
        "missing_facet_ids": _normalise_strings(missing_facet_ids),
        "effective_policy": _merge_bundle_policies(bundle_states),
        "counts": {
            "requested_bundle_count": len(ordered_requested_bundle_ids),
            "effective_bundle_count": len(effective_bundle_ids),
            "effective_facet_count": len(facet_ids),
            "missing_bundle_count": len(missing_bundle_ids),
            "missing_facet_count": len(_normalise_strings(missing_facet_ids)),
        },
        "guardrail_events": [
            {"event": "missing_context_bundle", "bundle_id": bundle_id}
            for bundle_id in missing_bundle_ids
        ],
    }
    return {
        "success": True,
        "subject_id": resolved_subject_id,
        "subject_kind": resolved_subject_kind,
        "effective_context_bundle_ids": effective_bundle_ids,
        "effective_context_facet_ids": facet_ids,
        "bundle_states": bundle_states,
        "facet_states": facet_states,
        "diagnostics": diagnostics,
    }


def update_context_report_revision(
    *,
    dossier_id: str,
    report_text: str,
    revision_id: str | None = None,
    title: str | None = None,
    summary: Mapping[str, Any] | None = None,
    open_questions: Sequence[str] = (),
    evidence_receipts: Sequence[Mapping[str, Any]] = (),
    branch_id: str | None = None,
    status: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    ensure_canonical_context_bundle_ontology()

    resolved_dossier_id = _safe_str(dossier_id)
    dossier_state = load_context_dossier_state(resolved_dossier_id)
    if not resolved_dossier_id or not isinstance(dossier_state, Mapping):
        return {"success": False, "error": "context_dossier_not_found"}

    report_body, report_truncated = _truncate_text(
        report_text,
        maximum=DEFAULT_REPORT_TEXT_MAX_CHARS,
    )
    if not report_body:
        return {"success": False, "error": "report_text_required"}

    revision_number = _coerce_int(
        len(_normalise_strings(dossier_state.get("report_revision_ids"))) + 1,
        default=1,
        minimum=1,
        maximum=10000,
    )
    resolved_revision_id = _safe_str(revision_id) or stable_named_instance_concept_id(
        f"{resolved_dossier_id}:report_revision:{revision_number}",
        prefix="workflow_report_revision",
    )
    revision_title = _safe_str(title) or f"Report revision {revision_number}"
    _ensure_context_instance(
        concept_id=resolved_revision_id,
        name=revision_title,
        description=f"Report revision {revision_number} for {resolved_dossier_id}.",
        type_id=WORKFLOW_REPORT_REVISION_TYPE_ID,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        visibility_scope_mode=None,
    )

    receipt_ids: list[str] = []
    for raw_receipt in evidence_receipts:
        if not isinstance(raw_receipt, Mapping):
            continue
        result = persist_evidence_receipt(
            receipt=raw_receipt,
            name=f"{revision_title} evidence receipt",
            namespace=namespace,
            user_id=user_id,
            org_id=org_id,
        )
        receipt_id = _safe_str(result.get("receipt_id"))
        if receipt_id:
            receipt_ids.append(receipt_id)
            try:
                add_relationship(
                    source_id=resolved_revision_id,
                    predicate=CITES_EVIDENCE_RECEIPT_PREDICATE_ID,
                    target=receipt_id,
                )
            except Exception as exc:
                logger.warning(
                    "[context_bundle] could not link receipt %s to report %s: %s",
                    receipt_id,
                    resolved_revision_id,
                    exc,
                )

    revision_state = {
        "schema_version": WORKFLOW_REPORT_REVISION_SCHEMA_VERSION,
        "revision_id": resolved_revision_id,
        "dossier_id": resolved_dossier_id,
        "revision_number": revision_number,
        "title": revision_title,
        "summary": _clone_mapping(summary),
        "report_text_char_count": len(report_body),
        "open_questions": _normalise_strings(
            open_questions,
            limit=DEFAULT_WORKSPACE_MAX_OPEN_QUESTIONS,
        ),
        "receipt_ids": receipt_ids,
        "branch_id": _safe_str(branch_id) or None,
        "status": _safe_str(status) or "active",
        "report_text_truncated": report_truncated,
        "updated_at_utc": _utcnow_iso(),
    }
    persisted = _persist_context_state(
        concept_id=resolved_revision_id,
        attribute_prefix="workflow_report_revision",
        state_key="workflow_report_revision",
        state=revision_state,
    )
    upsert_singleton_text_relation(
        subject_concept_id=resolved_revision_id,
        predicate="hasContent",
        text=report_body,
        lang="en-NZ",
        context={
            "source": "context_bundle_service",
            "reason": "workflow_report_revision",
        },
        garbage_collect=True,
    )

    report_revision_ids = _normalise_strings(
        [*dossier_state.get("report_revision_ids", []), resolved_revision_id]
    )
    dossier_state["report_revision_ids"] = report_revision_ids
    dossier_state["latest_report_revision_id"] = resolved_revision_id
    dossier_state["updated_at_utc"] = _utcnow_iso()
    _persist_context_state(
        concept_id=resolved_dossier_id,
        attribute_prefix="context_dossier",
        state_key="context_dossier",
        state=dossier_state,
    )

    try:
        add_relationship(
            source_id=resolved_dossier_id,
            predicate=HAS_REPORT_REVISION_PREDICATE_ID,
            target=resolved_revision_id,
        )
    except Exception as exc:
        logger.warning(
            "[context_bundle] could not link dossier %s to report revision %s: %s",
            resolved_dossier_id,
            resolved_revision_id,
            exc,
        )

    return {
        "success": True,
        "dossier_id": resolved_dossier_id,
        "report_revision_id": resolved_revision_id,
        "report_revision": persisted,
    }


def assemble_context_dossier(
    *,
    name: str,
    dossier_id: str | None = None,
    subject_kind: str,
    subject_id: str,
    dossier_kind: str | None = None,
    effective_context_bundle_ids: Sequence[str] = (),
    open_questions: Sequence[str] = (),
    evidence_receipts: Sequence[Mapping[str, Any]] = (),
    immediate_context: Mapping[str, Any] | None = None,
    search_history: Sequence[Mapping[str, Any]] = (),
    testing_theory_ids: Sequence[str] = (),
    local_assertions: Sequence[Mapping[str, Any]] = (),
    hypotheses: Sequence[Mapping[str, Any]] = (),
    promotion_candidates: Sequence[Mapping[str, Any]] = (),
    branch_specs: Sequence[Mapping[str, Any]] = (),
    report_text: str | None = None,
    report_title: str | None = None,
    report_summary: Mapping[str, Any] | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    ensure_canonical_context_bundle_ontology()

    resolved_name = _safe_str(name, limit=200) or "Context dossier"
    resolved_dossier_id = _safe_str(dossier_id) or stable_named_instance_concept_id(
        resolved_name,
        prefix="context_dossier",
    )
    resolved_subject_id = _safe_str(subject_id)
    resolved_subject_kind = _safe_str(subject_kind).lower()
    if resolved_subject_kind not in {"concept", "workflow"} or not resolved_subject_id:
        return {"success": False, "error": "subject_kind_and_subject_id_required"}

    resolution = resolve_effective_context(
        subject_kind=resolved_subject_kind,
        subject_id=resolved_subject_id,
        explicit_bundle_ids=effective_context_bundle_ids,
    )
    resolved_bundle_ids = _normalise_strings(
        resolution.get("effective_context_bundle_ids") or effective_context_bundle_ids
    )
    resolved_facet_ids = _normalise_strings(
        resolution.get("effective_context_facet_ids") or []
    )

    _ensure_context_instance(
        concept_id=resolved_dossier_id,
        name=resolved_name,
        description=f"Context dossier for {resolved_subject_id}.",
        type_id=CONTEXT_DOSSIER_TYPE_ID,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        visibility_scope_mode=None,
    )

    receipt_ids: list[str] = []
    for raw_receipt in evidence_receipts:
        if not isinstance(raw_receipt, Mapping):
            continue
        receipt_result = persist_evidence_receipt(
            receipt=raw_receipt,
            name=f"{resolved_name} evidence receipt",
            namespace=namespace,
            user_id=user_id,
            org_id=org_id,
        )
        receipt_id = _safe_str(receipt_result.get("receipt_id"))
        if not receipt_id:
            continue
        receipt_ids.append(receipt_id)
        try:
            add_relationship(
                source_id=resolved_dossier_id,
                predicate=CITES_EVIDENCE_RECEIPT_PREDICATE_ID,
                target=receipt_id,
            )
        except Exception as exc:
            logger.warning(
                "[context_bundle] could not link dossier %s to receipt %s: %s",
                resolved_dossier_id,
                receipt_id,
                exc,
            )

    theory_states: list[dict[str, Any]] = []
    for theory_id in _normalise_strings(testing_theory_ids):
        state = get_testing_theory_state(theory_id)
        if isinstance(state, Mapping):
            theory_states.append(dict(state))

    bounded_local_assertions = [
        _bounded_copy(item, max_string_chars=1200)
        for item in list(local_assertions or [])[:DEFAULT_WORKSPACE_MAX_LOCAL_ASSERTIONS]
        if isinstance(item, Mapping)
    ]
    bounded_hypotheses = [
        _bounded_copy(item, max_string_chars=1200)
        for item in list(hypotheses or [])[:DEFAULT_WORKSPACE_MAX_HYPOTHESES]
        if isinstance(item, Mapping)
    ]
    bounded_promotion_candidates = [
        _bounded_copy(item, max_string_chars=1200)
        for item in list(promotion_candidates or [])[
            :DEFAULT_WORKSPACE_MAX_PROMOTION_CANDIDATES
        ]
        if isinstance(item, Mapping)
    ]

    branches: list[dict[str, Any]] = []
    for index, raw_branch in enumerate(
        list(branch_specs or [])[:DEFAULT_WORKSPACE_MAX_BRANCHES],
        start=1,
    ):
        if not isinstance(raw_branch, Mapping):
            continue
        branch_kind = _safe_str(raw_branch.get("branch_kind")).lower()
        if branch_kind not in CONTEXT_DOSSIER_BRANCH_KINDS:
            branch_kind = CONTEXT_DOSSIER_BRANCH_KIND_THEORY_LOCAL
        branches.append(
            {
                "branch_id": _safe_str(raw_branch.get("branch_id"))
                or f"{resolved_dossier_id}:branch:{index}",
                "branch_kind": branch_kind,
                "title": _safe_str(raw_branch.get("title"), limit=200) or f"Branch {index}",
                "status": _safe_str(raw_branch.get("status")) or "active",
                "testing_theory_id": _safe_str(raw_branch.get("testing_theory_id")) or None,
                "basis": _safe_str(raw_branch.get("basis"), limit=1000) or None,
                "open_questions": _normalise_strings(
                    raw_branch.get("open_questions"),
                    limit=DEFAULT_WORKSPACE_MAX_OPEN_QUESTIONS,
                ),
                "promotion_candidate_ids": _normalise_strings(
                    raw_branch.get("promotion_candidate_ids"),
                    limit=DEFAULT_WORKSPACE_MAX_PROMOTION_CANDIDATES,
                ),
            }
        )

    state = {
        "schema_version": CONTEXT_DOSSIER_SCHEMA_VERSION,
        "dossier_id": resolved_dossier_id,
        "dossier_kind": _safe_str(dossier_kind) or resolved_subject_kind,
        "subject_kind": resolved_subject_kind,
        "subject_id": resolved_subject_id,
        "effective_context_bundle_ids": resolved_bundle_ids,
        "effective_context_facet_ids": resolved_facet_ids,
        "testing_theory_ids": _normalise_strings(testing_theory_ids),
        "theory_state_summaries": [
            {
                "theory_id": _safe_str(item.get("theory_id")),
                "included_canonical_concept_ids": _normalise_strings(
                    item.get("included_canonical_concept_ids")
                ),
                "local_assertion_count": len(item.get("local_assertions") or []),
                "lifecycle_state": _safe_str(item.get("lifecycle_state")) or None,
                "expires_at_utc": _safe_str(item.get("expires_at_utc")) or None,
            }
            for item in theory_states
        ],
        "local_assertions": bounded_local_assertions,
        "hypotheses": bounded_hypotheses,
        "promotion_candidates": bounded_promotion_candidates,
        "branches": branches,
        "open_questions": _normalise_strings(
            open_questions,
            limit=DEFAULT_WORKSPACE_MAX_OPEN_QUESTIONS,
        ),
        "receipt_ids": receipt_ids,
        "immediate_context": _bounded_copy(
            immediate_context or {},
            max_string_chars=DEFAULT_WORKSPACE_MAX_IMMEDIATE_CONTEXT_CHARS,
        ),
        "search_history": [
            _bounded_copy(item, max_string_chars=1200)
            for item in list(search_history or [])[:DEFAULT_WORKSPACE_MAX_SEARCH_HISTORY]
            if isinstance(item, Mapping)
        ],
        "report_revision_ids": [],
        "latest_report_revision_id": None,
        "updated_at_utc": _utcnow_iso(),
    }
    persisted = _persist_context_state(
        concept_id=resolved_dossier_id,
        attribute_prefix="context_dossier",
        state_key="context_dossier",
        state=state,
    )

    if resolved_subject_id:
        try:
            add_relationship(
                source_id=resolved_subject_id,
                predicate=HAS_CONTEXT_DOSSIER_PREDICATE_ID,
                target=resolved_dossier_id,
            )
        except Exception as exc:
            logger.warning(
                "[context_bundle] could not attach dossier %s to %s: %s",
                resolved_dossier_id,
                resolved_subject_id,
                exc,
            )

    for bundle_id in resolved_bundle_ids:
        try:
            add_relationship(
                source_id=resolved_dossier_id,
                predicate=HAS_CONTEXT_BUNDLE_PREDICATE_ID,
                target=bundle_id,
            )
        except Exception:
            pass

    report_result = None
    report_text_value = _safe_str(report_text)
    if report_text_value:
        report_result = update_context_report_revision(
            dossier_id=resolved_dossier_id,
            report_text=report_text_value,
            title=report_title,
            summary=report_summary,
            open_questions=open_questions,
            evidence_receipts=evidence_receipts,
            namespace=namespace,
            user_id=user_id,
            org_id=org_id,
        )
        if bool(report_result.get("success")):
            refreshed = load_context_dossier_state(resolved_dossier_id)
            if isinstance(refreshed, Mapping):
                persisted = dict(refreshed)

    return {
        "success": True,
        "dossier_id": resolved_dossier_id,
        "context_dossier": persisted,
        "report_revision": (report_result or {}).get("report_revision"),
        "report_revision_id": (report_result or {}).get("report_revision_id"),
    }


def build_reconstructed_workspace(
    *,
    subject_kind: str,
    subject_id: str,
    question: str | None = None,
    task: str | None = None,
    dossier_id: str | None = None,
    report_revision_id: str | None = None,
    effective_context_bundle_ids: Sequence[str] = (),
    immediate_context: Mapping[str, Any] | None = None,
    open_questions: Sequence[str] = (),
    evidence_receipts: Sequence[Mapping[str, Any] | Mapping[str, str]] = (),
    search_history: Sequence[Mapping[str, Any]] = (),
    interaction_budget: Mapping[str, Any] | None = None,
    termination_status: Mapping[str, Any] | None = None,
    reconstruction_round: int | None = None,
    compression_decisions: Sequence[Mapping[str, Any]] = (),
    guardrail_events: Sequence[Mapping[str, Any]] = (),
    promotion_attempts: Sequence[Mapping[str, Any]] = (),
    branch_transitions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    resolved_subject_kind = _safe_str(subject_kind).lower()
    resolved_subject_id = _safe_str(subject_id)
    if resolved_subject_kind not in {"concept", "workflow"} or not resolved_subject_id:
        return {"success": False, "error": "subject_kind_and_subject_id_required"}

    resolved_dossier_id = _safe_str(dossier_id)
    dossier_state = (
        load_context_dossier_state(resolved_dossier_id) if resolved_dossier_id else None
    )
    revision_state = (
        load_workflow_report_revision_state(_safe_str(report_revision_id))
        if _safe_str(report_revision_id)
        else None
    )
    if revision_state is None and isinstance(dossier_state, Mapping):
        latest_revision_id = _safe_str(dossier_state.get("latest_report_revision_id"))
        if latest_revision_id:
            revision_state = load_workflow_report_revision_state(latest_revision_id)

    resolution = resolve_effective_context(
        subject_kind=resolved_subject_kind,
        subject_id=resolved_subject_id,
        explicit_bundle_ids=effective_context_bundle_ids,
    )
    resolved_bundle_ids = _normalise_strings(
        resolution.get("effective_context_bundle_ids") or effective_context_bundle_ids
    )
    resolved_facet_ids = _normalise_strings(
        resolution.get("effective_context_facet_ids") or []
    )

    dropped_counts = {
        "open_questions": max(
            0,
            len(list(open_questions or [])) - DEFAULT_WORKSPACE_MAX_OPEN_QUESTIONS,
        ),
        "evidence_receipts": max(
            0,
            len(list(evidence_receipts or [])) - DEFAULT_WORKSPACE_MAX_EVIDENCE_RECEIPTS,
        ),
        "search_history": max(
            0,
            len(list(search_history or [])) - DEFAULT_WORKSPACE_MAX_SEARCH_HISTORY,
        ),
        "branches": max(
            0,
            len(list((dossier_state or {}).get("branches") or []))
            - DEFAULT_WORKSPACE_MAX_BRANCHES,
        ),
    }

    bounded_receipts: list[dict[str, Any]] = []
    if evidence_receipts:
        bounded_receipts = [
            _bounded_copy(item, max_string_chars=1200)
            for item in list(evidence_receipts or [])[
                :DEFAULT_WORKSPACE_MAX_EVIDENCE_RECEIPTS
            ]
            if isinstance(item, Mapping)
        ]
    else:
        receipt_ids = _normalise_strings(
            [
                *((dossier_state or {}).get("receipt_ids") or []),
                *((revision_state or {}).get("receipt_ids") or []),
            ],
            limit=DEFAULT_WORKSPACE_MAX_EVIDENCE_RECEIPTS,
        )
        for receipt_id in receipt_ids:
            receipt_state = load_evidence_receipt_state(receipt_id)
            if isinstance(receipt_state, Mapping):
                bounded_receipts.append(
                    _bounded_copy(receipt_state, max_string_chars=1200)
                )
    bounded_immediate_context = _bounded_copy(
        immediate_context
        if immediate_context is not None
        else (dossier_state or {}).get("immediate_context") or {},
        max_string_chars=DEFAULT_WORKSPACE_MAX_IMMEDIATE_CONTEXT_CHARS,
    )
    bounded_open_questions = _normalise_strings(
        open_questions or (dossier_state or {}).get("open_questions"),
        limit=DEFAULT_WORKSPACE_MAX_OPEN_QUESTIONS,
    )
    bounded_search_history = [
        _bounded_copy(item, max_string_chars=1000)
        for item in list(search_history or (dossier_state or {}).get("search_history") or [])[
            :DEFAULT_WORKSPACE_MAX_SEARCH_HISTORY
        ]
        if isinstance(item, Mapping)
    ]

    telemetry = {
        "schema_version": RECONSTRUCTED_WORKSPACE_SCHEMA_VERSION,
        "reconstruction_round": _coerce_int(
            reconstruction_round,
            default=1,
            minimum=1,
            maximum=10000,
        ),
        "compression_decisions": [
            _bounded_copy(item, max_string_chars=1000)
            for item in list(compression_decisions or [])[:40]
            if isinstance(item, Mapping)
        ],
        "guardrail_events": [
            _bounded_copy(item, max_string_chars=1000)
            for item in list(guardrail_events or [])[:40]
            if isinstance(item, Mapping)
        ]
        or list((resolution.get("diagnostics") or {}).get("guardrail_events") or []),
        "promotion_attempts": [
            _bounded_copy(item, max_string_chars=1000)
            for item in list(promotion_attempts or [])[:40]
            if isinstance(item, Mapping)
        ],
        "branch_transitions": [
            _bounded_copy(item, max_string_chars=1000)
            for item in list(branch_transitions or [])[:40]
            if isinstance(item, Mapping)
        ],
        "dropped_context_counts": dropped_counts,
        "bundle_resolution_diagnostics": _bounded_copy(
            resolution.get("diagnostics"),
            max_string_chars=1000,
        ),
        "updated_at_utc": _utcnow_iso(),
    }

    workspace = {
        "schema_version": RECONSTRUCTED_WORKSPACE_SCHEMA_VERSION,
        "subject_kind": resolved_subject_kind,
        "subject_id": resolved_subject_id,
        WORKSPACE_KEY_QUESTION: _safe_str(question, limit=2000) or None,
        WORKSPACE_KEY_TASK: _safe_str(task, limit=2000) or None,
        WORKSPACE_KEY_EFFECTIVE_CONTEXT_BUNDLE_IDS: resolved_bundle_ids,
        WORKSPACE_KEY_EFFECTIVE_CONTEXT_FACET_IDS: resolved_facet_ids,
        WORKSPACE_KEY_CONTEXT_DOSSIER_ID: resolved_dossier_id or None,
        WORKSPACE_KEY_CONTEXT_DOSSIER: _bounded_copy(
            dossier_state or {},
            max_string_chars=1000,
        ),
        WORKSPACE_KEY_REPORT_REVISION_ID: _safe_str(
            (revision_state or {}).get("revision_id") or report_revision_id
        )
        or None,
        WORKSPACE_KEY_REPORT_REVISION: _bounded_copy(
            revision_state or {},
            max_string_chars=1000,
        ),
        WORKSPACE_KEY_IMMEDIATE_CONTEXT: bounded_immediate_context,
        WORKSPACE_KEY_OPEN_QUESTIONS: bounded_open_questions,
        WORKSPACE_KEY_EVIDENCE_RECEIPTS: bounded_receipts,
        WORKSPACE_KEY_SEARCH_HISTORY: bounded_search_history,
        WORKSPACE_KEY_INTERACTION_BUDGET: _bounded_copy(
            interaction_budget or {},
            max_string_chars=1000,
        ),
        WORKSPACE_KEY_TERMINATION_STATUS: _bounded_copy(
            termination_status or {},
            max_string_chars=1000,
        ),
        WORKSPACE_KEY_WORKSPACE_TELEMETRY: telemetry,
    }
    workspace["workspace_fingerprint"] = _hash_payload(workspace)

    if isinstance(dossier_state, Mapping) and resolved_dossier_id:
        updated_dossier = dict(dossier_state)
        updated_dossier["workspace_state"] = {
            "schema_version": RECONSTRUCTED_WORKSPACE_SCHEMA_VERSION,
            "workspace_fingerprint": workspace["workspace_fingerprint"],
            "last_reconstruction_round": telemetry["reconstruction_round"],
            "telemetry": telemetry,
            "updated_at_utc": _utcnow_iso(),
        }
        updated_dossier["updated_at_utc"] = _utcnow_iso()
        _persist_context_state(
            concept_id=resolved_dossier_id,
            attribute_prefix="context_dossier",
            state_key="context_dossier",
            state=updated_dossier,
        )

    return {"success": True, "workspace": workspace}


__all__ = [
    "assemble_context_dossier",
    "bootstrap_canonical_context_bundle_ontology",
    "build_reconstructed_workspace",
    "canonical_context_bundle_concept_ids",
    "context_bundle_ontology_blueprints",
    "create_or_update_context_bundle",
    "ensure_canonical_context_bundle_ontology",
    "list_attached_context_dossier_ids",
    "load_context_bundle_state",
    "load_context_dossier_state",
    "load_workflow_report_revision_state",
    "persist_evidence_receipt",
    "resolve_effective_context",
    "update_context_report_revision",
]
