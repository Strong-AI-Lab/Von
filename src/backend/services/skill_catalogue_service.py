"""Discover and synchronise external SKILL artefacts into Vontology.

This service deliberately builds on the deterministic SKILL interoperability
substrate in ``src.backend.workflows.skill_interop`` rather than introducing a
parallel execution model. Its job is to make the discovered skill inventory
inspectable and reusable from Vontology while keeping execution in VWL/runtime
semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .text_value_service import (
    delete_text_relation,
    get_text_relations_summary,
    upsert_singleton_text_relation,
)
from .workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    load_concept,
    normalise_relationship_targets,
)
from ..workflows.skill_interop import (
    SKILL_INTEROP_SCHEMA_VERSION,
    SKILL_VONTOLOGY_PREDICATE_IDS,
    SkillArtefact,
    build_skill_interop_metadata,
    discover_skill_catalogue,
    load_skill_artefact,
    transpile_skill_to_workflow_definition,
)

AGENT_SKILL_TYPE_ID = "#V#agent_skill"
SKILL_WORKFLOW_ID_PREDICATE_ID = "#V#has_skill_workflow_id"
SKILL_INTEROP_METADATA_JSON_PREDICATE_ID = "#V#has_skill_interop_metadata_json"

_PREDICATE_PARENT_IDS: tuple[str, ...] = ("#V#predicate", "#V#binary_predicate")
_SKILL_SCOPE_ORDER: tuple[str, ...] = ("project", "personal", "extension", "shared")
_DEFAULT_ROOT_ENV_VARS: Mapping[str, str] = {
    "project": "VON_PROJECT_SKILL_ROOTS",
    "personal": "VON_PERSONAL_SKILL_ROOTS",
    "extension": "VON_EXTENSION_SKILL_ROOTS",
    "shared": "VON_SHARED_SKILL_ROOTS",
}


def _safe_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _slugify(value: str) -> str:
    cleaned = [
        char.lower() if char.isalnum() else "_"
        for char in _safe_str(value)
    ]
    slug = "".join(cleaned).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "skill"


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = _safe_str(raw_value)
        if not value:
            continue
        fingerprint = value.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ordered.append(value)
    return ordered


def _resolve_existing_paths(values: Sequence[str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = _safe_str(raw_value)
        if not value:
            continue
        candidate = Path(value).expanduser().resolve()
        candidate_text = str(candidate)
        if candidate_text.casefold() in seen:
            continue
        seen.add(candidate_text.casefold())
        if candidate.exists():
            resolved.append(candidate_text)
    return resolved


def _split_env_paths(variable_name: str) -> list[str]:
    raw_value = os.environ.get(variable_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        return []
    return [item for item in raw_value.split(os.pathsep) if _safe_str(item)]


def _default_roots_for_scope(scope: str) -> list[str]:
    repo_root = Path(__file__).resolve().parents[3]
    home = Path.home()

    default_candidates: dict[str, list[str]] = {
        "project": [
            str(repo_root / ".agents" / "skills"),
            str(repo_root / ".codex" / "skills"),
            str(repo_root / ".github" / "skills"),
        ],
        "personal": [
            str(home / ".agents" / "skills"),
        ],
        "extension": [
            str(home / ".codex" / "skills"),
        ],
        "shared": [],
    }
    env_paths = _split_env_paths(_DEFAULT_ROOT_ENV_VARS[scope])
    return _resolve_existing_paths(default_candidates.get(scope, []) + env_paths)


def resolve_skill_roots_by_scope(
    *,
    include_default_roots: bool = True,
    project_roots: Sequence[str] | None = None,
    personal_roots: Sequence[str] | None = None,
    extension_roots: Sequence[str] | None = None,
    shared_roots: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    """Resolve concrete, existing skill roots for each supported scope."""

    explicit_roots_by_scope: dict[str, Sequence[str]] = {
        "project": project_roots or (),
        "personal": personal_roots or (),
        "extension": extension_roots or (),
        "shared": shared_roots or (),
    }
    resolved: dict[str, list[str]] = {}
    for scope in _SKILL_SCOPE_ORDER:
        candidates: list[str] = []
        if include_default_roots:
            candidates.extend(_default_roots_for_scope(scope))
        candidates.extend(list(explicit_roots_by_scope[scope]))
        resolved[scope] = _resolve_existing_paths(candidates)
    return resolved


def _skill_concept_id(skill: SkillArtefact) -> str:
    skill_dir_name = Path(skill.skill_directory).resolve().name or skill.name
    digest = hashlib.sha256(
        str(Path(skill.skill_file).resolve()).casefold().encode("utf-8")
    ).hexdigest()[:8]
    return f"#V#agent_skill_{_slugify(skill_dir_name)}_{digest}"


def _skill_display_name(skill: SkillArtefact) -> str:
    return f"{skill.name} skill"


def _body_preview(text: str, *, max_chars: int = 240) -> str:
    content = _safe_str(text)
    if len(content) <= max_chars:
        return content
    return f"{content[: max_chars - 3].rstrip()}..."


def _build_interop_metadata(skill: SkillArtefact, *, workflow_id: str) -> dict[str, Any]:
    metadata = dict(build_skill_interop_metadata(skill))
    metadata["workflow_id"] = workflow_id
    metadata["skill_concept_id"] = _skill_concept_id(skill)
    metadata["skill_type_id"] = AGENT_SKILL_TYPE_ID
    return metadata


def _build_skill_entry(
    skill: SkillArtefact,
    *,
    include_body: bool,
) -> dict[str, Any]:
    workflow_definition = transpile_skill_to_workflow_definition(skill)
    workflow_id = workflow_definition.workflow_id
    metadata = _build_interop_metadata(skill, workflow_id=workflow_id)
    payload = skill.to_discovery_record().to_dict()
    payload.update(
        {
            "skill_concept_id": _skill_concept_id(skill),
            "workflow_id": workflow_id,
            "interop_schema_version": SKILL_INTEROP_SCHEMA_VERSION,
            "body_preview": _body_preview(skill.body),
            "referenced_resources": list(skill.referenced_resources),
            "vontology_projection": metadata.get("vontology_projection") or {},
            "interop_metadata": metadata,
        }
    )
    if include_body:
        payload["body"] = skill.body
    return payload


def _load_catalogue_artefacts(
    *,
    roots_by_scope: Mapping[str, Sequence[str] | str],
) -> list[SkillArtefact]:
    discovered = discover_skill_catalogue(roots_by_scope)
    artefacts: list[SkillArtefact] = []
    for record in discovered:
        result = load_skill_artefact(
            record.skill_file,
            source_scope=record.source_scope,
            discovery_root=record.discovery_root,
        )
        if result.artefact is None:
            continue
        artefacts.append(result.artefact)
    return artefacts


def list_skill_catalogue(
    *,
    include_default_roots: bool = True,
    project_roots: Sequence[str] | None = None,
    personal_roots: Sequence[str] | None = None,
    extension_roots: Sequence[str] | None = None,
    shared_roots: Sequence[str] | None = None,
    include_body: bool = False,
) -> dict[str, Any]:
    """Return a Vontology-facing catalogue of discovered skills."""

    roots_by_scope = resolve_skill_roots_by_scope(
        include_default_roots=include_default_roots,
        project_roots=project_roots,
        personal_roots=personal_roots,
        extension_roots=extension_roots,
        shared_roots=shared_roots,
    )
    artefacts = _load_catalogue_artefacts(roots_by_scope=roots_by_scope)
    skills = [
        _build_skill_entry(skill, include_body=include_body)
        for skill in artefacts
    ]
    return {
        "success": True,
        "roots_by_scope": roots_by_scope,
        "skills": skills,
        "count": len(skills),
    }


def _ensure_type_concept(*, concept_id: str, name: str, description: str, parent_id: str) -> None:
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=[parent_id],
            create_as_instance=False,
        )
        return

    relationships = dict(concept_doc.get("relationships") or {})
    existing_parent_ids = normalise_relationship_targets(
        relationships.get("is_a_type_of")
    )
    if parent_id in existing_parent_ids:
        return
    existing_parent_ids.append(parent_id)
    relationships["is_a_type_of"] = existing_parent_ids
    concept_service.update_concept(concept_id, {"relationships": relationships})


def _ensure_predicate_concept(*, concept_id: str, name: str, description: str) -> None:
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=["#V#predicate"],
            create_as_instance=True,
        )
        return
    ensure_instance_typing(
        concept_id=concept_id,
        type_ids=("#V#predicate",),
        remove_type_parent_ids=_PREDICATE_PARENT_IDS,
    )


def ensure_skill_catalogue_primitives(*, dry_run: bool = False) -> dict[str, Any]:
    """Ensure the Vontology primitives required for skill catalogue sync exist."""

    ensured_ids: list[str] = [
        AGENT_SKILL_TYPE_ID,
        SKILL_WORKFLOW_ID_PREDICATE_ID,
        SKILL_INTEROP_METADATA_JSON_PREDICATE_ID,
        *SKILL_VONTOLOGY_PREDICATE_IDS,
    ]
    if dry_run:
        return {"success": True, "ensured_concept_ids": ensured_ids, "dry_run": True}

    _ensure_type_concept(
        concept_id=AGENT_SKILL_TYPE_ID,
        name="Agent skill",
        description=(
            "A reusable external SKILL artefact that Von can discover, inspect, "
            "and execute through the workflow interoperability layer."
        ),
        parent_id="#V#document",
    )

    for concept_id, name, description in (
        (
            SKILL_WORKFLOW_ID_PREDICATE_ID,
            "Has skill workflow ID",
            "Stores the deterministic workflow ID exposed by skill-to-workflow transpilation.",
        ),
        (
            SKILL_INTEROP_METADATA_JSON_PREDICATE_ID,
            "Has skill interop metadata JSON",
            "Stores the canonical JSON snapshot of skill interoperability metadata and provenance.",
        ),
    ):
        _ensure_predicate_concept(
            concept_id=concept_id,
            name=name,
            description=description,
        )

    for predicate_id in SKILL_VONTOLOGY_PREDICATE_IDS:
        _ensure_predicate_concept(
            concept_id=predicate_id,
            name=predicate_id.replace("#V#", "").replace("_", " ").strip() or predicate_id,
            description=(
                "Predicate used by the SKILL interoperability catalogue projection."
            ),
        )

    return {"success": True, "ensured_concept_ids": ensured_ids}


def _clear_singleton_text_group(*, concept_id: str, predicate: str, language: str = "en-NZ") -> list[str]:
    summary = get_text_relations_summary(
        concept_id,
        predicates=[predicate],
        languages=[language],
        max_relation_ids_per_group=200,
    )
    removed_relation_ids: list[str] = []
    for group in summary.get("groups") or []:
        if not isinstance(group, Mapping):
            continue
        if group.get("predicate") != predicate or group.get("language") != language:
            continue
        for relation_id in group.get("relation_ids") or []:
            if not isinstance(relation_id, str) or not relation_id.strip():
                continue
            delete_text_relation(
                concept_id,
                relation_id.strip(),
                garbage_collect=True,
            )
            removed_relation_ids.append(relation_id.strip())
    return removed_relation_ids


def _upsert_catalogue_text(
    *,
    concept_id: str,
    predicate: str,
    text: str,
    provenance: Mapping[str, Any],
    optional: bool = False,
) -> dict[str, Any]:
    value = _safe_str(text)
    if not value and optional:
        return {
            "success": True,
            "predicate": predicate,
            "cleared_relation_ids": _clear_singleton_text_group(
                concept_id=concept_id,
                predicate=predicate,
            ),
        }
    if not value:
        return {"success": False, "predicate": predicate, "skipped": True}
    return upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=predicate,
        text=value,
        lang="en-NZ",
        provenance=dict(provenance),
        garbage_collect=True,
    )


def _ensure_skill_concept(skill: SkillArtefact) -> tuple[str, str]:
    concept_id = _skill_concept_id(skill)
    display_name = _skill_display_name(skill)
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=display_name,
            concept_id=concept_id,
            description=skill.description,
            parent_concept_ids=[AGENT_SKILL_TYPE_ID],
            create_as_instance=True,
        )
    else:
        ensure_instance_typing(
            concept_id=concept_id,
            type_ids=(AGENT_SKILL_TYPE_ID,),
        )
    return concept_id, display_name


def sync_skill_catalogue_to_vontology(
    *,
    include_default_roots: bool = True,
    project_roots: Sequence[str] | None = None,
    personal_roots: Sequence[str] | None = None,
    extension_roots: Sequence[str] | None = None,
    shared_roots: Sequence[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Synchronise discovered skills into first-class Vontology concepts."""

    roots_by_scope = resolve_skill_roots_by_scope(
        include_default_roots=include_default_roots,
        project_roots=project_roots,
        personal_roots=personal_roots,
        extension_roots=extension_roots,
        shared_roots=shared_roots,
    )
    artefacts = _load_catalogue_artefacts(roots_by_scope=roots_by_scope)
    primitive_report = ensure_skill_catalogue_primitives(dry_run=dry_run)
    synced_skills: list[dict[str, Any]] = []

    for skill in artefacts:
        skill_entry = _build_skill_entry(skill, include_body=False)
        concept_id = skill_entry["skill_concept_id"]
        workflow_id = skill_entry["workflow_id"]
        metadata_json = json.dumps(
            skill_entry["interop_metadata"],
            sort_keys=True,
            ensure_ascii=True,
            indent=2,
        )
        provenance = {
            "source": "skill_catalogue_sync",
            "skill_file": skill.skill_file,
            "discovery_root": skill.discovery_root,
            "source_scope": skill.source_scope,
        }

        if not dry_run:
            _ensure_skill_concept(skill)
            _upsert_catalogue_text(
                concept_id=concept_id,
                predicate="hasDescription",
                text=skill.description,
                provenance=provenance,
            )
            _upsert_catalogue_text(
                concept_id=concept_id,
                predicate="hasContent",
                text=skill.body,
                provenance=provenance,
            )
            _upsert_catalogue_text(
                concept_id=concept_id,
                predicate=SKILL_WORKFLOW_ID_PREDICATE_ID,
                text=workflow_id,
                provenance=provenance,
            )
            _upsert_catalogue_text(
                concept_id=concept_id,
                predicate=SKILL_INTEROP_METADATA_JSON_PREDICATE_ID,
                text=metadata_json,
                provenance=provenance,
            )
            for predicate_id, value in (
                (key, value)
                for key, value in (skill_entry.get("vontology_projection") or {}).items()
                if isinstance(key, str)
            ):
                _upsert_catalogue_text(
                    concept_id=concept_id,
                    predicate=predicate_id,
                    text=str(value),
                    provenance=provenance,
                    optional=predicate_id == "#V#has_skill_argument_hint",
                )
            if "#V#has_skill_argument_hint" not in (skill_entry.get("vontology_projection") or {}):
                _upsert_catalogue_text(
                    concept_id=concept_id,
                    predicate="#V#has_skill_argument_hint",
                    text="",
                    provenance=provenance,
                    optional=True,
                )

        synced_skills.append(
            {
                **skill_entry,
                "dry_run": dry_run,
                "synced": not dry_run,
                "skill_concept_id": concept_id,
                "workflow_id": workflow_id,
            }
        )

    return {
        "success": True,
        "dry_run": dry_run,
        "roots_by_scope": roots_by_scope,
        "ensured_concept_ids": primitive_report.get("ensured_concept_ids") or [],
        "synced_skills": synced_skills,
        "count": len(synced_skills),
    }


__all__ = [
    "AGENT_SKILL_TYPE_ID",
    "SKILL_INTEROP_METADATA_JSON_PREDICATE_ID",
    "SKILL_WORKFLOW_ID_PREDICATE_ID",
    "ensure_skill_catalogue_primitives",
    "list_skill_catalogue",
    "resolve_skill_roots_by_scope",
    "sync_skill_catalogue_to_vontology",
]
