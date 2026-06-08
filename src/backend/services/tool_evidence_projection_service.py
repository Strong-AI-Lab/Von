"""Runtime projection of tool payloads from Vontology evidence contracts.

The projection rules are read from represented tool, field, payload-path, and
evidence-view concepts. This module is deliberately generic: tool-specific
behaviour belongs in Vontology materialisation services, not in the runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
)

VIEW_PURPOSE_PRIORITY: tuple[str, ...] = (
    "#V#tool_evidence_view_purpose_final_answer",
    "#V#tool_evidence_view_purpose_user_display",
    "#V#tool_evidence_view_purpose_follow_up_selection",
    "#V#tool_evidence_view_purpose_telemetry",
)

PROJECTION_SCHEMA_VERSION = "tool_evidence_projection.v1"
SURFACEABLE_CONCEPT_EVIDENCE_SCHEMA_VERSION = "surfaceable_concept_evidence.v1"
NESTED_WORKFLOW_PROGRESS_EVIDENCE_SCHEMA_VERSION = (
    "nested_workflow_progress_evidence.v1"
)
WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION = "workflow_progress_projection.v1"

_CONCEPT_ID_RE = re.compile(r"#V#[A-Za-z0-9._-]+")
_CONDITIONALLY_SURFACEABLE_SINGLE_KEYS = {
    "concept_id",
    "canonical_concept_id",
    "existing_concept_id",
}
_SURFACEABLE_SINGLE_KEYS = {
    "created_concept_id",
    "materialised_concept_id",
    "materialized_concept_id",
    "represented_artefact_concept_id",
    "paper_concept_id",
    "file_copy_concept_id",
    "computer_file_copy_concept_id",
    "source_file_copy_concept_id",
}
_SURFACEABLE_PATH_MARKERS = {
    "created",
    "created_concepts",
    "created_results",
    "creation_results",
    "materialised",
    "materialized",
    "materialisation",
    "materialization",
    "new_concepts",
    "surfaceable",
}
_SURFACEABLE_LIST_KEYS = {
    "created_concept_ids",
    "materialised_concept_ids",
    "materialized_concept_ids",
    "represented_artefact_concept_ids",
    "paper_concept_ids",
    "file_copy_concept_ids",
    "computer_file_copy_concept_ids",
    "source_file_copy_concept_ids",
}
_CONDITIONALLY_SURFACEABLE_LIST_KEYS = {
    "artefact_ids",
    "concept_ids",
}
_NON_SURFACEABLE_CONCEPT_KEYS = {
    "actor_concept_id",
    "assignee_concept_id",
    "author_concept_id",
    "object_concept_id",
    "org_concept_id",
    "predicate_concept_id",
    "source_concept_id",
    "subject_concept_id",
    "target_concept_id",
    "type_concept_id",
    "user_concept_id",
}
_MUTATION_KIND_KEYS = {
    "mutation_kind",
    "operation",
    "status",
    "action",
}
_CREATED_MUTATION_KINDS = {
    "created",
    "materialised",
    "materialized",
    "upserted",
    "linked",
    "bound",
}


@dataclass(frozen=True)
class ToolFieldContract:
    concept_id: str
    output_key: str
    wire_aliases: tuple[str, ...]
    payload_paths: tuple[str, ...]
    required: bool
    included: bool
    redacted: bool


@dataclass(frozen=True)
class ToolProjectionContract:
    tool_concept_id: str
    evidence_view_concept_ids: tuple[str, ...]
    fields: tuple[ToolFieldContract, ...]
    output_field_ids: tuple[str, ...]
    collection_field_ids: tuple[str, ...]


def project_surfaceable_concept_evidence(
    payload: Any,
    *,
    max_items: int = 40,
    max_depth: int = 8,
) -> list[dict[str, Any]]:
    """Extract durable concept handles that are safe to show or reuse.

    This is a generic evidence projection, not a response policy. It preserves
    concept IDs that tool/workflow results identify as created, materialised,
    linked, represented artefacts, or canonical concept outputs so later LLM
    stages and follow-up turns do not have to recover them from truncated JSON.
    """

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _clean_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    def _looks_like_concept_id(value: Any) -> bool:
        return isinstance(value, str) and bool(_CONCEPT_ID_RE.fullmatch(value.strip()))

    def _normalise_mutation_kind(value: Any) -> str | None:
        text = _clean_text(value)
        if not text:
            return None
        if text == "materialized":
            return "materialised"
        return text.lower()

    def _artefact_type_from_key(key: str) -> str | None:
        lowered = key.lower().strip()
        if lowered in {"paper_concept_id", "paper_concept_ids"}:
            return "paper_concept"
        if lowered in {
            "file_copy_concept_id",
            "file_copy_concept_ids",
            "computer_file_copy_concept_id",
            "computer_file_copy_concept_ids",
            "source_file_copy_concept_id",
            "source_file_copy_concept_ids",
        }:
            return "file_copy"
        if lowered in {"created_concept_id", "created_concept_ids"}:
            return "concept"
        if lowered in {
            "represented_artefact_concept_id",
            "represented_artefact_concept_ids",
        }:
            return "represented_artefact"
        return None

    def _mutation_kind_from_key_or_path(
        key: str,
        path: tuple[str, ...],
    ) -> str | None:
        lowered_key = key.lower().strip()
        if lowered_key in {"existing_concept_id", "existing_concept_ids"}:
            return "existing"
        if lowered_key in {"created_concept_id", "created_concept_ids"}:
            return "created"
        if lowered_key in {"materialised_concept_id", "materialised_concept_ids"}:
            return "materialised"
        if lowered_key in {"materialized_concept_id", "materialized_concept_ids"}:
            return "materialised"

        lowered_path = {part.lower().strip() for part in path}
        if lowered_path.intersection({"created", "created_concepts", "new_concepts"}):
            return "created"
        if lowered_path.intersection(
            {"materialised", "materialized", "materialisation", "materialization"}
        ):
            return "materialised"
        if lowered_path.intersection({"linked", "links"}):
            return "linked"
        return None

    def _truthy_creation_flag(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "created"}
        return False

    def _container_marks_surfaceable(
        container: Mapping[str, Any], path: tuple[str, ...]
    ) -> bool:
        mutation_kind = _normalise_mutation_kind(container.get("mutation_kind"))
        if mutation_kind in _CREATED_MUTATION_KINDS:
            return True
        if _truthy_creation_flag(container.get("created")) or _truthy_creation_flag(
            container.get("was_created")
        ):
            return True
        lowered_path = {part.lower() for part in path}
        return bool(lowered_path.intersection(_SURFACEABLE_PATH_MARKERS))

    def _add(
        concept_id: str,
        *,
        source_key: str,
        source_path: tuple[str, ...],
        container: Mapping[str, Any] | None = None,
        mutation_kind: str | None = None,
        artefact_type: str | None = None,
    ) -> None:
        if len(entries) >= max_items:
            return
        clean_id = concept_id.strip()
        if not _looks_like_concept_id(clean_id):
            return
        lowered = clean_id.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        container_map = container if isinstance(container, Mapping) else {}
        resolved_mutation = mutation_kind
        if resolved_mutation is None:
            for mutation_key in _MUTATION_KIND_KEYS:
                resolved_mutation = _normalise_mutation_kind(
                    container_map.get(mutation_key)
                )
                if resolved_mutation:
                    break
        resolved_type = artefact_type or _artefact_type_from_key(source_key)
        if not resolved_type:
            resolved_type = _clean_text(container_map.get("artefact_type"))
        if not resolved_type and "paper" in clean_id.lower():
            resolved_type = "paper_concept"
        elif not resolved_type and "file_copy" in clean_id.lower():
            resolved_type = "file_copy"

        entry: dict[str, Any] = {
            "concept_id": clean_id,
            "source_key": source_key,
            "source_path": ".".join(source_path),
        }
        if resolved_mutation:
            entry["mutation_kind"] = resolved_mutation
        if resolved_type:
            entry["artefact_type"] = resolved_type
        arxiv_id = _clean_text(container_map.get("arxiv_id"))
        if arxiv_id:
            entry["arxiv_id"] = arxiv_id
        entries.append(entry)

    def _scan_string_preview(text: str, path: tuple[str, ...]) -> None:
        stripped = text.strip()
        if not stripped:
            return
        parsed: Any = None
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
        if isinstance(parsed, (Mapping, list, tuple)):
            _walk(parsed, path=path, depth=0)
            return
        source_key = path[-1] if path else "text"
        if source_key.lower() not in {"_preview", "preview", "content", "text"}:
            return
        lowered_path = {part.lower() for part in path}
        if not lowered_path.intersection(_SURFACEABLE_PATH_MARKERS):
            return
        for match in _CONCEPT_ID_RE.finditer(stripped):
            _add(
                match.group(0),
                source_key=source_key,
                source_path=path,
                mutation_kind=_mutation_kind_from_key_or_path(source_key, path),
            )

    def _walk(value: Any, *, path: tuple[str, ...], depth: int) -> None:
        if len(entries) >= max_items or depth > max_depth:
            return
        if isinstance(value, Mapping):
            container = value
            mutation_kind = _normalise_mutation_kind(value.get("mutation_kind"))
            artefact_type = _clean_text(value.get("artefact_type"))
            for raw_key, nested in value.items():
                if len(entries) >= max_items:
                    break
                key = str(raw_key or "").strip()
                if not key:
                    continue
                lowered_key = key.lower()
                next_path = (*path, key)
                if lowered_key in _NON_SURFACEABLE_CONCEPT_KEYS:
                    continue
                if lowered_key in _SURFACEABLE_SINGLE_KEYS and isinstance(nested, str):
                    _add(
                        nested,
                        source_key=key,
                        source_path=next_path,
                        container=container,
                        mutation_kind=mutation_kind
                        or _mutation_kind_from_key_or_path(key, next_path),
                        artefact_type=artefact_type,
                    )
                    continue
                if (
                    lowered_key in _CONDITIONALLY_SURFACEABLE_SINGLE_KEYS
                    and isinstance(nested, str)
                    and _container_marks_surfaceable(container, next_path)
                ):
                    _add(
                        nested,
                        source_key=key,
                        source_path=next_path,
                        container=container,
                        mutation_kind=mutation_kind
                        or _mutation_kind_from_key_or_path(key, next_path),
                        artefact_type=artefact_type,
                    )
                    continue
                if (
                    lowered_key in _SURFACEABLE_LIST_KEYS
                    and isinstance(nested, Sequence)
                    and not isinstance(nested, (str, bytes, bytearray))
                ):
                    for index, item in enumerate(nested):
                        if not isinstance(item, str):
                            continue
                        _add(
                            item,
                            source_key=key,
                            source_path=(*next_path, str(index)),
                            container=container,
                            mutation_kind=mutation_kind
                            or _mutation_kind_from_key_or_path(key, next_path),
                            artefact_type=artefact_type,
                        )
                    continue
                if (
                    lowered_key in _CONDITIONALLY_SURFACEABLE_LIST_KEYS
                    and _container_marks_surfaceable(container, next_path)
                    and isinstance(nested, Sequence)
                    and not isinstance(nested, (str, bytes, bytearray))
                ):
                    for index, item in enumerate(nested):
                        if not isinstance(item, str):
                            continue
                        _add(
                            item,
                            source_key=key,
                            source_path=(*next_path, str(index)),
                            container=container,
                            mutation_kind=mutation_kind
                            or _mutation_kind_from_key_or_path(key, next_path),
                            artefact_type=artefact_type,
                        )
                    continue
                if isinstance(nested, str):
                    _scan_string_preview(nested, next_path)
                elif isinstance(nested, (Mapping, list, tuple)):
                    _walk(nested, path=next_path, depth=depth + 1)
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                if len(entries) >= max_items:
                    break
                _walk(item, path=(*path, str(index)), depth=depth + 1)

    _walk(payload, path=(), depth=0)
    return entries


def surfaceable_concept_ids_from_evidence(
    evidence: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    concept_ids: list[str] = []
    seen: set[str] = set()
    for entry in evidence or ():
        if not isinstance(entry, Mapping):
            continue
        concept_id = entry.get("concept_id")
        if not isinstance(concept_id, str) or not _CONCEPT_ID_RE.fullmatch(
            concept_id.strip()
        ):
            continue
        clean_id = concept_id.strip()
        lowered = clean_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        concept_ids.append(clean_id)
    return concept_ids


def project_nested_workflow_progress_evidence(
    payload: Any,
    *,
    max_facts: int = 16,
    max_depth: int = 8,
    max_nodes: int = 320,
) -> dict[str, Any] | None:
    """Project represented nested workflow progress facts from an aggregate payload.

    The only user-facing labels this helper preserves are labels already carried
    on ``workflow_progress_projection.v1`` facts. Source-specific evidence
    policy belongs in the represented projection metadata that produced those
    facts, not in this traversal primitive.
    """

    facts: list[dict[str, Any]] = []
    fact_seen: set[tuple[str, str, str]] = set()
    contract_ids: list[str] = []
    contract_seen: set[str] = set()
    source_paths: list[str] = []
    source_seen: set[str] = set()
    workflow_evidence_seen = False
    visited = 0

    def _clean_text(value: Any, *, limit: int = 240) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            return None
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: max(0, limit - 3)].rstrip()}..."

    def _normalise_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    def _normalise_value(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            return _clean_text(value, limit=320)
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            items: list[Any] = []
            for item in list(value)[:5]:
                normalised = _normalise_value(item)
                if normalised is not None:
                    items.append(normalised)
            return items or None
        return None

    def _append_source_path(path: str) -> None:
        if not path or path in source_seen:
            return
        source_seen.add(path)
        source_paths.append(path)

    def _append_contract_id(contract_id: str | None) -> None:
        if not contract_id or contract_id in contract_seen:
            return
        contract_seen.add(contract_id)
        contract_ids.append(contract_id)

    def _append_fact(raw_fact: Mapping[str, Any], source_path: str) -> None:
        if len(facts) >= max_facts:
            return
        schema_version = _clean_text(raw_fact.get("schema_version"), limit=80)
        if schema_version and schema_version != WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION:
            return
        fact_id = _clean_text(raw_fact.get("fact_id") or raw_fact.get("id"), limit=120)
        label = _clean_text(raw_fact.get("label"), limit=120)
        if not fact_id or not label:
            return
        status = _clean_text(raw_fact.get("status"), limit=80) or (
            "available" if "value" in raw_fact else "missing"
        )
        contract_id = _clean_text(
            raw_fact.get("contract_id")
            or raw_fact.get("projection_contract_id")
            or raw_fact.get("concept_id"),
            limit=180,
        )
        source_key = _clean_text(raw_fact.get("source_path"), limit=240) or source_path
        key = (fact_id, label, source_key)
        if key in fact_seen:
            return
        fact_seen.add(key)
        _append_source_path(source_path)
        _append_contract_id(contract_id)

        fact: dict[str, Any] = {
            "schema_version": WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION,
            "fact_id": fact_id,
            "label": label,
            "status": status,
            "present": _normalise_bool(raw_fact.get("present")),
            "redacted": _normalise_bool(raw_fact.get("redacted")),
            "truncated": _normalise_bool(raw_fact.get("truncated")),
            "source_path": source_key,
            "payload_source_path": source_path,
        }
        for field in (
            "value_kind",
            "visibility",
            "resolved_path",
            "reason_code",
            "workflow_id",
            "state_id",
            "action_id",
        ):
            text = _clean_text(raw_fact.get(field), limit=180)
            if text:
                fact[field] = text
        if contract_id:
            fact["contract_id"] = contract_id
        if "value" in raw_fact and not fact["redacted"]:
            normalised_value = _normalise_value(raw_fact.get("value"))
            if normalised_value is not None:
                fact["value"] = normalised_value
        facts.append(fact)

    def _append_raw_facts(raw_facts: Any, source_path: str) -> None:
        if not isinstance(raw_facts, Sequence) or isinstance(
            raw_facts, (str, bytes, bytearray)
        ):
            return
        for raw_fact in raw_facts:
            if isinstance(raw_fact, Mapping):
                _append_fact(raw_fact, source_path)
                if len(facts) >= max_facts:
                    return

    def _walk(value: Any, *, path: tuple[str, ...], depth: int) -> None:
        nonlocal visited, workflow_evidence_seen
        if len(facts) >= max_facts or depth > max_depth or visited >= max_nodes:
            return
        if isinstance(value, Mapping):
            visited += 1
            if any(
                key in value
                for key in (
                    "iteration_results",
                    "subworkflow_invocation",
                    "subworkflow_result_envelope",
                    "workflow_terminal",
                    "workflow_terminal_state",
                    "last_action_id",
                    "last_action_outputs",
                    "progress_facts",
                    "workflow_progress_facts",
                    "last_workflow_progress_facts",
                )
            ):
                workflow_evidence_seen = True
            for raw_key, nested in value.items():
                key = str(raw_key or "").strip()
                if not key:
                    continue
                next_path = (*path, key)
                source_path = ".".join(next_path)
                lowered_key = key.lower()
                if lowered_key in {
                    "progress_facts",
                    "workflow_progress_facts",
                    "last_workflow_progress_facts",
                }:
                    _append_raw_facts(nested, source_path)
                    continue
                if lowered_key == "_preview" and isinstance(nested, str):
                    stripped = nested.strip()
                    if stripped.startswith(("{", "[")):
                        try:
                            _walk(
                                json.loads(stripped),
                                path=next_path,
                                depth=depth + 1,
                            )
                        except Exception:
                            pass
                    continue
                if isinstance(nested, (Mapping, list, tuple)):
                    _walk(nested, path=next_path, depth=depth + 1)
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                if len(facts) >= max_facts or visited >= max_nodes:
                    return
                _walk(item, path=(*path, str(index)), depth=depth + 1)

    _walk(payload, path=(), depth=0)
    if not workflow_evidence_seen and not facts:
        return None
    return {
        "schema_version": NESTED_WORKFLOW_PROGRESS_EVIDENCE_SCHEMA_VERSION,
        "projection_source_schema_version": WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION,
        "facts": facts,
        "contract_ids": contract_ids,
        "source_paths": source_paths,
        "workflow_evidence_seen": workflow_evidence_seen,
        "telemetry": {
            "schema_version": NESTED_WORKFLOW_PROGRESS_EVIDENCE_SCHEMA_VERSION,
            "projection_source_schema_version": WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION,
            "fact_count": len(facts),
            "contract_ids": contract_ids,
            "source_paths": source_paths,
            "scanned_node_count": visited,
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        },
    }


def render_surfaceable_concept_lines(
    evidence: Sequence[Mapping[str, Any]] | None,
    *,
    existing_text: str | None = None,
    max_lines: int = 12,
) -> list[str]:
    lines: list[str] = []
    existing = existing_text or ""
    for entry in evidence or ():
        if len(lines) >= max_lines or not isinstance(entry, Mapping):
            break
        concept_id = entry.get("concept_id")
        if not isinstance(concept_id, str) or not _CONCEPT_ID_RE.fullmatch(
            concept_id.strip()
        ):
            continue
        clean_id = concept_id.strip()
        if clean_id in existing:
            continue
        artefact_type = _clean_str(entry.get("artefact_type")) or "concept"
        mutation_kind = (_clean_str(entry.get("mutation_kind")) or "").lower()
        if artefact_type in {"paper_concept", "paper"}:
            label = (
                "Created paper concept"
                if mutation_kind == "created"
                else "Paper concept"
            )
        elif artefact_type in {"file_copy", "computer_file_copy", "source_file_copy"}:
            label = (
                "Linked file copy" if mutation_kind == "linked" else "File copy concept"
            )
        elif mutation_kind in _CREATED_MUTATION_KINDS:
            label = f"{mutation_kind.replace('_', ' ').capitalize()} concept"
        elif mutation_kind == "existing":
            label = "Existing concept"
        else:
            label = "Concept"
        line = f"{label}: {clean_id}."
        if line not in lines:
            lines.append(line)
    return lines


def project_tool_payload_for_llm(
    tool_name: str,
    payload: Mapping[str, Any],
    *,
    max_collection_items: int = 40,
) -> dict[str, Any] | None:
    """Return a compact Vontology-backed LLM payload, or None if no contract exists."""

    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    if not isinstance(payload, Mapping):
        return None

    contract = resolve_tool_projection_contract(tool_name)
    if contract is None or not contract.fields:
        return None

    projected: dict[str, Any] = {
        "_llm_view": PROJECTION_SCHEMA_VERSION,
    }
    telemetry: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "tool_concept_id": contract.tool_concept_id,
        "evidence_view_concept_ids": list(contract.evidence_view_concept_ids),
        "preserved_fields": [],
        "missing_required_fields": [],
        "omitted_fields": [],
        "redacted_fields": [],
    }

    collection_projection = _project_collection_fields(
        contract,
        payload,
        max_collection_items=max_collection_items,
    )
    collection_field_ids = set(contract.collection_field_ids)
    row_projected_field_ids = set(collection_projection.get("field_ids") or [])
    if collection_projection.get("collection_key"):
        collection_key = str(collection_projection["collection_key"])
        projected[collection_key] = collection_projection["items"]
        projected[f"{collection_key}_count"] = collection_projection["total_count"]
        if collection_projection.get("omitted_count"):
            projected[f"{collection_key}_omitted_count"] = collection_projection[
                "omitted_count"
            ]
        telemetry["preserved_fields"].append(
            {
                "field_concept_id": collection_projection["collection_field_id"],
                "output_key": collection_key,
                "item_count": collection_projection["total_count"],
            }
        )
        for field_id in sorted(row_projected_field_ids):
            field = _field_by_id(contract, field_id)
            telemetry["preserved_fields"].append(
                {
                    "field_concept_id": field_id,
                    "output_key": field.output_key if field else field_id,
                    "location": collection_key,
                }
            )

    for field in contract.fields:
        if (
            field.concept_id in collection_field_ids
            or field.concept_id in row_projected_field_ids
        ):
            continue
        if field.redacted:
            telemetry["redacted_fields"].append(
                {"field_concept_id": field.concept_id, "output_key": field.output_key}
            )
            continue
        found, value = _extract_field_value(payload, field)
        if not found:
            if field.required:
                telemetry["missing_required_fields"].append(
                    {
                        "field_concept_id": field.concept_id,
                        "output_key": field.output_key,
                    }
                )
            else:
                telemetry["omitted_fields"].append(
                    {
                        "field_concept_id": field.concept_id,
                        "output_key": field.output_key,
                    }
                )
            continue
        compact_value = _compact_value(value)
        if compact_value is None:
            telemetry["omitted_fields"].append(
                {
                    "field_concept_id": field.concept_id,
                    "output_key": field.output_key,
                    "reason": "empty_or_unserialisable_value",
                }
            )
            continue
        projected[field.output_key] = compact_value
        telemetry["preserved_fields"].append(
            {"field_concept_id": field.concept_id, "output_key": field.output_key}
        )

    omitted_output_fields = set(contract.output_field_ids).difference(
        {entry["field_concept_id"] for entry in telemetry["preserved_fields"]}
    )
    omitted_output_fields.difference_update(
        {entry["field_concept_id"] for entry in telemetry["redacted_fields"]}
    )
    omitted_output_fields.difference_update(
        {entry["field_concept_id"] for entry in telemetry["missing_required_fields"]}
    )
    already_omitted = {
        entry["field_concept_id"] for entry in telemetry["omitted_fields"]
    }
    for field_id in sorted(omitted_output_fields.difference(already_omitted)):
        field = _field_by_id(contract, field_id)
        telemetry["omitted_fields"].append(
            {
                "field_concept_id": field_id,
                "output_key": field.output_key if field else field_id,
                "reason": "not_in_selected_evidence_view_or_value_absent",
            }
        )

    projected["_tool_evidence_projection"] = telemetry
    if len(projected) <= 2 and not (
        telemetry["missing_required_fields"] or telemetry["redacted_fields"]
    ):
        return None
    return projected


def resolve_tool_projection_contract(tool_name: str) -> ToolProjectionContract | None:
    """Resolve the represented projection contract for one internal MCP tool name."""

    tool_doc = _find_tool_concept(tool_name)
    if not isinstance(tool_doc, Mapping):
        return None
    tool_concept_id = _clean_str(tool_doc.get("concept_id"))
    if not tool_concept_id:
        return None

    relationships = _relationships(tool_doc)
    output_field_ids = tuple(
        _normalise_unique(relationships.get("#V#tool_has_output_field"))
    )
    preserve_field_ids = tuple(
        _normalise_unique(relationships.get("#V#tool_result_preserves_field"))
    )
    evidence_views = _select_evidence_views(tool_concept_id)
    if not evidence_views and not preserve_field_ids:
        return None

    required_field_ids: set[str] = set()
    included_field_ids: set[str] = set(preserve_field_ids)
    redacted_field_ids: set[str] = set()
    view_ids: list[str] = []
    for view_doc in evidence_views:
        view_id = _clean_str(view_doc.get("concept_id"))
        if view_id:
            view_ids.append(view_id)
        view_relationships = _relationships(view_doc)
        required_field_ids.update(
            _normalise_unique(view_relationships.get("#V#evidence_view_requires_field"))
        )
        included_field_ids.update(
            _normalise_unique(view_relationships.get("#V#evidence_view_includes_field"))
        )
        redacted_field_ids.update(
            _normalise_unique(view_relationships.get("#V#evidence_view_redacts_field"))
        )

    selected_field_ids = _ordered_unique(
        [
            *required_field_ids,
            *included_field_ids,
            *preserve_field_ids,
            *redacted_field_ids,
        ]
    )
    if output_field_ids:
        output_set = set(output_field_ids)
        selected_field_ids = [
            field_id for field_id in selected_field_ids if field_id in output_set
        ]
        if not selected_field_ids and preserve_field_ids:
            selected_field_ids = [
                field_id for field_id in preserve_field_ids if field_id in output_set
            ]

    fields = tuple(
        field
        for field_id in selected_field_ids
        if (
            field := _load_field_contract(
                field_id,
                required=field_id in required_field_ids,
                included=field_id in included_field_ids
                or field_id in preserve_field_ids,
                redacted=field_id in redacted_field_ids,
            )
        )
        is not None
    )
    collection_field_ids = tuple(
        field.concept_id
        for field in fields
        if "#V#tool_field_role_collection_membership"
        in _relationship_targets(
            load_concept(field.concept_id),
            "#V#field_has_role",
        )
    )

    return ToolProjectionContract(
        tool_concept_id=tool_concept_id,
        evidence_view_concept_ids=tuple(view_ids),
        fields=fields,
        output_field_ids=output_field_ids,
        collection_field_ids=collection_field_ids,
    )


def _find_tool_concept(tool_name: str) -> Mapping[str, Any] | None:
    cleaned = tool_name.strip()
    for query in (
        {"attributes.mcp_tool_name": cleaned},
        {"attributes.mcp_tool_name": cleaned.lower()},
    ):
        doc = ConceptsRepository.find_one(query)
        if isinstance(doc, Mapping):
            return doc
    return None


def _select_evidence_views(tool_concept_id: str) -> tuple[Mapping[str, Any], ...]:
    candidates = list(
        ConceptsRepository.find(
            {"relationships.#V#evidence_view_applies_to_tool": tool_concept_id},
        )
    )
    if not candidates:
        return ()

    by_priority: dict[int, list[Mapping[str, Any]]] = {}
    fallback: list[Mapping[str, Any]] = []
    for doc in candidates:
        purposes = _relationship_targets(doc, "#V#evidence_view_has_purpose")
        matched = False
        for index, purpose_id in enumerate(VIEW_PURPOSE_PRIORITY):
            if purpose_id in purposes:
                by_priority.setdefault(index, []).append(doc)
                matched = True
        if not matched:
            fallback.append(doc)
    if by_priority:
        return tuple(by_priority[min(by_priority)])
    return tuple(fallback)


def _load_field_contract(
    field_id: str,
    *,
    required: bool,
    included: bool,
    redacted: bool,
) -> ToolFieldContract | None:
    doc = load_concept(field_id)
    if not isinstance(doc, Mapping):
        return None
    raw_attributes = doc.get("attributes")
    attributes: Mapping[str, Any] = (
        raw_attributes if isinstance(raw_attributes, Mapping) else {}
    )
    output_key = _clean_str(attributes.get("field_key")) or _fallback_field_key(
        field_id
    )
    aliases = tuple(
        alias
        for alias_id in _relationship_targets(doc, "#V#field_has_wire_alias")
        if (alias := _wire_key_value(alias_id))
    )
    paths = tuple(
        path
        for path_id in _relationship_targets(doc, "#V#field_extracts_from_payload_path")
        if (path := _payload_path_value(path_id))
    )
    if output_key not in aliases:
        aliases = (output_key, *aliases)
    return ToolFieldContract(
        concept_id=field_id,
        output_key=output_key,
        wire_aliases=_dedupe(aliases),
        payload_paths=_dedupe(paths),
        required=required,
        included=included,
        redacted=redacted,
    )


def _project_collection_fields(
    contract: ToolProjectionContract,
    payload: Mapping[str, Any],
    *,
    max_collection_items: int,
) -> dict[str, Any]:
    collection_fields = [
        field
        for field in contract.fields
        if field.concept_id in contract.collection_field_ids
    ]
    for collection_field in collection_fields:
        found, value = _extract_field_value(payload, collection_field)
        if not found or not isinstance(value, list):
            continue
        row_fields = [
            field
            for field in contract.fields
            if field.concept_id != collection_field.concept_id
        ]
        rows: list[dict[str, Any]] = []
        projected_field_ids: set[str] = set()
        for item in value[:max_collection_items]:
            if not isinstance(item, Mapping):
                continue
            row: dict[str, Any] = {}
            for field in row_fields:
                found_row, row_value = _extract_field_value(
                    item,
                    _field_for_collection_item(field, collection_field),
                )
                if not found_row:
                    continue
                compact_value = _compact_value(row_value)
                if compact_value is None:
                    continue
                row[field.output_key] = compact_value
                projected_field_ids.add(field.concept_id)
            if row:
                rows.append(row)
        return {
            "collection_field_id": collection_field.concept_id,
            "collection_key": collection_field.output_key,
            "items": rows,
            "total_count": len(value),
            "omitted_count": max(0, len(value) - len(rows)),
            "field_ids": sorted(projected_field_ids),
        }
    return {}


def _field_for_collection_item(
    field: ToolFieldContract,
    collection_field: ToolFieldContract,
) -> ToolFieldContract:
    prefixes = [f"{alias}[]." for alias in collection_field.wire_aliases] + [
        f"{path}[]." for path in collection_field.payload_paths
    ]
    stripped_paths: list[str] = []
    for path in field.payload_paths:
        stripped = path
        for prefix in prefixes:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :]
                break
        stripped_paths.append(stripped)
    return ToolFieldContract(
        concept_id=field.concept_id,
        output_key=field.output_key,
        wire_aliases=field.wire_aliases,
        payload_paths=_dedupe(stripped_paths),
        required=field.required,
        included=field.included,
        redacted=field.redacted,
    )


def _extract_field_value(
    payload: Mapping[str, Any], field: ToolFieldContract
) -> tuple[bool, Any]:
    for alias in field.wire_aliases:
        if alias in payload:
            return True, payload.get(alias)
    for path in field.payload_paths:
        values = _extract_path_values(payload, path)
        values = [value for value in values if value is not None]
        if not values:
            continue
        if len(values) == 1:
            return True, values[0]
        return True, values
    return False, None


def _extract_path_values(value: Any, path: str) -> list[Any]:
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        return [value]
    return _extract_path_parts(value, parts)


def _extract_path_parts(value: Any, parts: Sequence[str]) -> list[Any]:
    if not parts:
        return [value]
    part = parts[0]
    rest = parts[1:]
    if part.endswith("[]"):
        key = part[:-2]
        sequence = _mapping_value(value, key)
        if not isinstance(sequence, list):
            return []
        results: list[Any] = []
        for item in sequence:
            results.extend(_extract_path_parts(item, rest))
        return results
    filter_key = None
    filter_value = None
    if "[" in part and part.endswith("]"):
        key, raw_filter = part[:-1].split("[", 1)
        if "=" in raw_filter:
            filter_key, filter_value = raw_filter.split("=", 1)
            part = key
    next_value = _mapping_value(value, part)
    if filter_key and isinstance(next_value, list):
        filtered = [
            item
            for item in next_value
            if isinstance(item, Mapping) and str(item.get(filter_key)) == filter_value
        ]
        results: list[Any] = []
        for item in filtered:
            if rest:
                results.extend(_extract_path_parts(item, rest))
            else:
                results.append(item.get("value") if "value" in item else item)
        return results
    return _extract_path_parts(next_value, rest) if next_value is not None else []


def _mapping_value(value: Any, key: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    if key in value:
        return value.get(key)
    lower_key = key.lower()
    for current_key, current_value in value.items():
        if isinstance(current_key, str) and current_key.lower() == lower_key:
            return current_value
    return None


def _compact_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        compact_items = [_compact_value(item) for item in value]
        compact_items = [item for item in compact_items if item is not None]
        return compact_items if compact_items else None
    if isinstance(value, Mapping):
        compact_map: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key.startswith("_"):
                continue
            compact_item = _compact_value(item)
            if compact_item is not None:
                compact_map[key] = compact_item
        return compact_map or None
    return str(value)


def _field_by_id(
    contract: ToolProjectionContract,
    field_id: str,
) -> ToolFieldContract | None:
    for field in contract.fields:
        if field.concept_id == field_id:
            return field
    return None


def _wire_key_value(concept_id: str) -> str | None:
    doc = load_concept(concept_id)
    attributes = doc.get("attributes") if isinstance(doc, Mapping) else None
    if isinstance(attributes, Mapping):
        return _clean_str(attributes.get("wire_key"))
    return None


def _payload_path_value(concept_id: str) -> str | None:
    doc = load_concept(concept_id)
    attributes = doc.get("attributes") if isinstance(doc, Mapping) else None
    if isinstance(attributes, Mapping):
        return _clean_str(attributes.get("payload_path"))
    return None


def _fallback_field_key(field_id: str) -> str:
    cleaned = field_id.removeprefix("#V#")
    for suffix in ("_field", "_argument_field"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned


def _relationships(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    relationships = doc.get("relationships")
    return relationships if isinstance(relationships, Mapping) else {}


def _relationship_targets(
    doc: Mapping[str, Any] | None, predicate: str
) -> tuple[str, ...]:
    if not isinstance(doc, Mapping):
        return ()
    return tuple(_normalise_unique(_relationships(doc).get(predicate)))


def _normalise_unique(value: Any) -> list[str]:
    return _ordered_unique(normalise_relationship_targets(value))


def _ordered_unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(_ordered_unique(list(values)))


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


__all__ = [
    "NESTED_WORKFLOW_PROGRESS_EVIDENCE_SCHEMA_VERSION",
    "PROJECTION_SCHEMA_VERSION",
    "WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION",
    "ToolFieldContract",
    "ToolProjectionContract",
    "project_nested_workflow_progress_evidence",
    "project_tool_payload_for_llm",
    "resolve_tool_projection_contract",
]
