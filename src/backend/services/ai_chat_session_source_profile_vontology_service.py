"""Vontology-backed source profiles for AI coding-session ingestion.

The ingestion pipeline scans files, computes hashes, and writes document/file
representations. The durable source-family catalogue belongs in Vontology:
profile identities, document/file-copy type concepts, relationship vocabulary,
adapter bindings, and local root hints are represented data. Repo seed fixtures
are import aids for materialising canonical profiles, not runtime authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .relationship_write_service import add_relationship
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation

SOURCE_PROFILE_CATALOGUE_SCHEMA_VERSION = "ai_chat_session_source_profile_catalogue.v1"
SOURCE_PROFILE_DEFINITION_SCHEMA_VERSION = "ai_chat_session_source_profile.v1"
SOURCE_PROFILE_SEED_SCHEMA_VERSION = "ai_chat_session_source_profiles_seed_bundle.v1"

SOURCE_PROFILE_CATALOGUE_CONCEPT_ID = (
    "#V#ai_assisted_programming_chat_session_source_profile_catalogue"
)
SOURCE_PROFILE_TYPE_ID = "#V#ai_assisted_programming_chat_session_source_profile"
HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON = (
    "#V#has_ai_chat_session_source_profile_catalogue_definition_json"
)
HAS_SOURCE_PROFILE_DEFINITION_JSON = (
    "#V#has_ai_chat_session_source_profile_definition_json"
)
HAS_SOURCE_PROFILE = "#V#has_ai_chat_session_source_profile"
HAS_SOURCE_PROFILE_DOCUMENT_TYPE = "#V#has_ai_chat_session_document_type"
HAS_SOURCE_PROFILE_FILE_COPY_TYPE = "#V#has_ai_chat_session_file_copy_type"

_CATALOGUE_TEXT_PREDICATES: tuple[str, ...] = (
    HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON,
    "has_ai_chat_session_source_profile_catalogue_definition_json",
    "hasContent",
)
_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    HAS_SOURCE_PROFILE_DEFINITION_JSON,
    "has_ai_chat_session_source_profile_definition_json",
    "hasContent",
)
_SEED_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "workflows"
    / "repo_seed_bundles"
    / "ai_chat_session_source_profiles_seed_bundle.json"
)


@dataclass(frozen=True)
class ConceptSpec:
    concept_id: str
    name: str
    parent_concept_id: str
    description: str
    notes: str | None = None


@dataclass(frozen=True)
class AIChatSessionSourceProfile:
    profile_concept_id: str
    environment: str
    display_name: str
    source_system: str
    adapter_kind: str
    document_type: ConceptSpec
    file_copy_type: ConceptSpec
    default_root_templates: tuple[str, ...] = ()
    file_patterns: tuple[str, ...] = ()
    name_tokens: tuple[str, ...] = ()
    required_roots: bool = False


@dataclass(frozen=True)
class AIChatSessionSourceProfileAuthority:
    catalogue_concept_id: str
    source_profile_type_id: str
    base_document_type: ConceptSpec
    base_file_copy_type: ConceptSpec
    document_has_file_predicate: ConceptSpec
    legacy_document_has_file_copy_predicate: ConceptSpec
    file_for_document_predicate: ConceptSpec
    instance_of_predicate_id: str
    profiles: tuple[AIChatSessionSourceProfile, ...]

    def profile_for_environment(self, environment: str) -> AIChatSessionSourceProfile:
        requested = _safe_str(environment).lower()
        for profile in self.profiles:
            if profile.environment == requested:
                return profile
        raise KeyError(requested)

    @property
    def profiles_by_environment(self) -> dict[str, AIChatSessionSourceProfile]:
        return {profile.environment: profile for profile in self.profiles}


class AIChatSessionSourceProfileAuthorityMissingError(RuntimeError):
    """Raised when chat-session ingestion lacks represented profile authority."""

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any]):
        super().__init__(message)
        self.diagnostics = dict(diagnostics)


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


def _hash_payload(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalise_strings(values: Any, *, lower: bool = False) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        stored = text.lower() if lower else text
        key = stored.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(stored)
    return tuple(output)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _normalise_concept_spec(raw: Any, *, field_name: str) -> ConceptSpec:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{field_name}_missing")
    concept_id = _safe_str(raw.get("concept_id"))
    name = _safe_str(raw.get("name")) or concept_id
    parent_concept_id = _safe_str(raw.get("parent_concept_id"))
    description = _safe_str(raw.get("description"), limit=4000)
    notes = _safe_str(raw.get("notes"), limit=4000) or None
    if not concept_id:
        raise ValueError(f"{field_name}_concept_id_missing")
    if not parent_concept_id:
        raise ValueError(f"{field_name}_parent_concept_id_missing")
    return ConceptSpec(
        concept_id=concept_id,
        name=name,
        parent_concept_id=parent_concept_id,
        description=description,
        notes=notes,
    )


def _concept_spec_to_dict(spec: ConceptSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "concept_id": spec.concept_id,
        "name": spec.name,
        "parent_concept_id": spec.parent_concept_id,
        "description": spec.description,
    }
    if spec.notes:
        payload["notes"] = spec.notes
    return payload


def _normalise_profile_definition(
    raw: Mapping[str, Any],
    *,
    profile_concept_id: str,
    source: str,
) -> tuple[AIChatSessionSourceProfile, dict[str, Any]]:
    environment = _safe_str(raw.get("environment")).lower()
    if not environment:
        raise ValueError("profile_environment_missing")
    source_system = _safe_str(raw.get("source_system"))
    if not source_system:
        raise ValueError("profile_source_system_missing")
    adapter_kind = _safe_str(raw.get("adapter_kind"))
    if not adapter_kind:
        raise ValueError("profile_adapter_kind_missing")

    document_type = _normalise_concept_spec(
        raw.get("document_type"),
        field_name="profile_document_type",
    )
    file_copy_type = _normalise_concept_spec(
        raw.get("file_copy_type"),
        field_name="profile_file_copy_type",
    )
    display_name = (
        _safe_str(raw.get("display_name"))
        or _safe_str(raw.get("profile_name"))
        or environment
    )
    profile = AIChatSessionSourceProfile(
        profile_concept_id=profile_concept_id,
        environment=environment,
        display_name=display_name,
        source_system=source_system,
        adapter_kind=adapter_kind,
        document_type=document_type,
        file_copy_type=file_copy_type,
        default_root_templates=_normalise_strings(raw.get("default_root_templates")),
        file_patterns=_normalise_strings(raw.get("file_patterns")),
        name_tokens=_normalise_strings(raw.get("name_tokens"), lower=True),
        required_roots=bool(raw.get("required_roots")),
    )
    definition = {
        "definition_schema_version": _safe_str(raw.get("definition_schema_version"))
        or SOURCE_PROFILE_DEFINITION_SCHEMA_VERSION,
        "profile_concept_id": profile.profile_concept_id,
        "environment": profile.environment,
        "display_name": profile.display_name,
        "source_system": profile.source_system,
        "adapter_kind": profile.adapter_kind,
        "document_type": _concept_spec_to_dict(profile.document_type),
        "file_copy_type": _concept_spec_to_dict(profile.file_copy_type),
        "default_root_templates": list(profile.default_root_templates),
        "file_patterns": list(profile.file_patterns),
        "name_tokens": list(profile.name_tokens),
        "required_roots": profile.required_roots,
        "source": source,
    }
    return profile, definition


def _normalise_catalogue_definition(
    raw: Mapping[str, Any],
    *,
    catalogue_concept_id: str,
    source: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base_document_type = _normalise_concept_spec(
        raw.get("base_document_type"),
        field_name="base_document_type",
    )
    base_file_copy_type = _normalise_concept_spec(
        raw.get("base_file_copy_type"),
        field_name="base_file_copy_type",
    )
    predicates = raw.get("predicates")
    if not isinstance(predicates, Mapping):
        raise ValueError("profile_catalogue_predicates_missing")
    document_has_file = _normalise_concept_spec(
        predicates.get("document_has_file"),
        field_name="document_has_file_predicate",
    )
    legacy_document_has_file_copy = _normalise_concept_spec(
        predicates.get("legacy_document_has_file_copy"),
        field_name="legacy_document_has_file_copy_predicate",
    )
    file_for_document = _normalise_concept_spec(
        predicates.get("file_for_document"),
        field_name="file_for_document_predicate",
    )
    instance_of_predicate_id = _safe_str(predicates.get("instance_of_predicate_id"))
    if not instance_of_predicate_id:
        raise ValueError("instance_of_predicate_id_missing")

    profile_concept_ids = _normalise_strings(raw.get("profile_concept_ids"))
    if not profile_concept_ids:
        raise ValueError("profile_concept_ids_missing")

    definition = {
        "definition_schema_version": _safe_str(raw.get("definition_schema_version"))
        or SOURCE_PROFILE_CATALOGUE_SCHEMA_VERSION,
        "catalogue_concept_id": catalogue_concept_id,
        "catalogue_title": _safe_str(raw.get("catalogue_title"))
        or "AI coding-session source profile catalogue",
        "catalogue_description": _safe_str(
            raw.get("catalogue_description"),
            limit=4000,
        ),
        "source_profile_type_id": _safe_str(raw.get("source_profile_type_id"))
        or SOURCE_PROFILE_TYPE_ID,
        "base_document_type": _concept_spec_to_dict(base_document_type),
        "base_file_copy_type": _concept_spec_to_dict(base_file_copy_type),
        "predicates": {
            "document_has_file": _concept_spec_to_dict(document_has_file),
            "legacy_document_has_file_copy": _concept_spec_to_dict(
                legacy_document_has_file_copy
            ),
            "file_for_document": _concept_spec_to_dict(file_for_document),
            "instance_of_predicate_id": instance_of_predicate_id,
            "has_source_profile": HAS_SOURCE_PROFILE,
            "has_source_profile_document_type": HAS_SOURCE_PROFILE_DOCUMENT_TYPE,
            "has_source_profile_file_copy_type": HAS_SOURCE_PROFILE_FILE_COPY_TYPE,
        },
        "profile_concept_ids": list(profile_concept_ids),
        "source": source,
    }
    components = {
        "base_document_type": base_document_type,
        "base_file_copy_type": base_file_copy_type,
        "document_has_file_predicate": document_has_file,
        "legacy_document_has_file_copy_predicate": legacy_document_has_file_copy,
        "file_for_document_predicate": file_for_document,
        "instance_of_predicate_id": instance_of_predicate_id,
        "source_profile_type_id": definition["source_profile_type_id"],
        "profile_concept_ids": profile_concept_ids,
    }
    return definition, components


def _definition_from_text_relation(
    concept_id: str,
    *,
    predicates: Sequence[str],
    diagnostics: dict[str, Any],
    malformed_key: str,
) -> tuple[dict[str, Any], str] | None:
    for predicate in predicates:
        texts = get_texts_for_concept(concept_id, predicate=predicate, limit=10)
        for row in texts:
            text = _safe_str((row or {}).get("text"))
            if not text:
                continue
            try:
                raw = json.loads(text)
                if not isinstance(raw, Mapping):
                    raise ValueError("definition_not_mapping")
                return dict(raw), predicate
            except Exception:
                diagnostics.setdefault(malformed_key, []).append(concept_id)
    return None


def load_ai_chat_session_source_profile_authority(
    *,
    catalogue_concept_id: str = SOURCE_PROFILE_CATALOGUE_CONCEPT_ID,
) -> tuple[AIChatSessionSourceProfileAuthority, dict[str, Any]]:
    requested_catalogue_id = _safe_str(catalogue_concept_id)
    diagnostics: dict[str, Any] = {
        "requested_catalogue_concept_id": requested_catalogue_id,
        "loaded_catalogue_concept_id": None,
        "catalogue_source_predicate": None,
        "profile_source_predicates": {},
        "missing_concept_ids": [],
        "malformed_catalogue_concept_ids": [],
        "malformed_profile_concept_ids": [],
    }
    if not requested_catalogue_id:
        diagnostics["missing_concept_ids"] = [None]
        raise AIChatSessionSourceProfileAuthorityMissingError(
            "source_profile_catalogue_concept_id_missing",
            diagnostics=diagnostics,
        )

    if not isinstance(_safe_get_concept(requested_catalogue_id), Mapping):
        diagnostics["missing_concept_ids"] = [requested_catalogue_id]
        raise AIChatSessionSourceProfileAuthorityMissingError(
            "source_profile_catalogue_concept_missing",
            diagnostics=diagnostics,
        )

    loaded = _definition_from_text_relation(
        requested_catalogue_id,
        predicates=_CATALOGUE_TEXT_PREDICATES,
        diagnostics=diagnostics,
        malformed_key="malformed_catalogue_concept_ids",
    )
    if loaded is None:
        diagnostics["missing_concept_ids"] = [requested_catalogue_id]
        raise AIChatSessionSourceProfileAuthorityMissingError(
            "source_profile_catalogue_definition_missing",
            diagnostics=diagnostics,
        )
    raw_catalogue, catalogue_predicate = loaded
    try:
        catalogue_definition, components = _normalise_catalogue_definition(
            raw_catalogue,
            catalogue_concept_id=requested_catalogue_id,
            source="vontology",
        )
    except Exception as exc:
        diagnostics["malformed_catalogue_concept_ids"] = [requested_catalogue_id]
        raise AIChatSessionSourceProfileAuthorityMissingError(
            f"source_profile_catalogue_definition_invalid:{exc}",
            diagnostics=diagnostics,
        ) from exc

    profiles: list[AIChatSessionSourceProfile] = []
    for profile_concept_id in components["profile_concept_ids"]:
        if not isinstance(_safe_get_concept(profile_concept_id), Mapping):
            diagnostics["missing_concept_ids"].append(profile_concept_id)
            continue
        loaded_profile = _definition_from_text_relation(
            profile_concept_id,
            predicates=_PROFILE_TEXT_PREDICATES,
            diagnostics=diagnostics,
            malformed_key="malformed_profile_concept_ids",
        )
        if loaded_profile is None:
            diagnostics["missing_concept_ids"].append(profile_concept_id)
            continue
        raw_profile, profile_predicate = loaded_profile
        try:
            profile, _definition = _normalise_profile_definition(
                raw_profile,
                profile_concept_id=profile_concept_id,
                source="vontology",
            )
        except Exception:
            diagnostics["malformed_profile_concept_ids"].append(profile_concept_id)
            continue
        profiles.append(profile)
        diagnostics["profile_source_predicates"][profile_concept_id] = profile_predicate

    if (
        diagnostics["missing_concept_ids"]
        or diagnostics["malformed_profile_concept_ids"]
    ):
        raise AIChatSessionSourceProfileAuthorityMissingError(
            "source_profile_authority_incomplete",
            diagnostics=diagnostics,
        )
    if not profiles:
        raise AIChatSessionSourceProfileAuthorityMissingError(
            "source_profile_authority_empty",
            diagnostics=diagnostics,
        )

    diagnostics["loaded_catalogue_concept_id"] = requested_catalogue_id
    diagnostics["catalogue_source_predicate"] = catalogue_predicate
    diagnostics["catalogue_definition_sha256"] = _hash_payload(catalogue_definition)
    diagnostics["loaded_profile_concept_ids"] = [
        profile.profile_concept_id for profile in profiles
    ]
    diagnostics["profile_environments"] = [profile.environment for profile in profiles]

    authority = AIChatSessionSourceProfileAuthority(
        catalogue_concept_id=requested_catalogue_id,
        source_profile_type_id=str(components["source_profile_type_id"]),
        base_document_type=components["base_document_type"],
        base_file_copy_type=components["base_file_copy_type"],
        document_has_file_predicate=components["document_has_file_predicate"],
        legacy_document_has_file_copy_predicate=components[
            "legacy_document_has_file_copy_predicate"
        ],
        file_for_document_predicate=components["file_for_document_predicate"],
        instance_of_predicate_id=str(components["instance_of_predicate_id"]),
        profiles=tuple(sorted(profiles, key=lambda profile: profile.environment)),
    )
    return authority, diagnostics


def load_ai_chat_session_source_profiles_from_seed_fixture(
    fixture_path: Path | str = _SEED_FIXTURE_PATH,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    resolved_path = Path(fixture_path)
    text = resolved_path.read_text(encoding="utf-8")
    raw = json.loads(text)
    if not isinstance(raw, Mapping):
        raise ValueError("source_profile_seed_fixture_invalid")
    if _safe_str(raw.get("seed_schema_version")) != SOURCE_PROFILE_SEED_SCHEMA_VERSION:
        raise ValueError("source_profile_seed_schema_version_invalid")

    raw_catalogue = raw.get("catalogue")
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_catalogue, Mapping):
        raise ValueError("source_profile_seed_catalogue_missing")
    if not isinstance(raw_profiles, Sequence) or isinstance(
        raw_profiles,
        (str, bytes, bytearray),
    ):
        raise ValueError("source_profile_seed_profiles_missing")

    catalogue_id = _safe_str(raw_catalogue.get("catalogue_concept_id")) or (
        SOURCE_PROFILE_CATALOGUE_CONCEPT_ID
    )
    catalogue_definition, _components = _normalise_catalogue_definition(
        raw_catalogue,
        catalogue_concept_id=catalogue_id,
        source="seed_bundle_import_fixture",
    )
    profile_definitions: list[dict[str, Any]] = []
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, Mapping):
            continue
        profile_id = _safe_str(raw_profile.get("profile_concept_id"))
        if not profile_id:
            raise ValueError("source_profile_seed_profile_concept_id_missing")
        _profile, definition = _normalise_profile_definition(
            raw_profile,
            profile_concept_id=profile_id,
            source="seed_bundle_import_fixture",
        )
        profile_definitions.append(definition)

    metadata = {
        "fixture_path": str(resolved_path),
        "fixture_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "seed_schema_version": SOURCE_PROFILE_SEED_SCHEMA_VERSION,
    }
    return catalogue_definition, profile_definitions, metadata


def _existing_definition(concept_id: str, predicates: Sequence[str]) -> bool:
    for predicate in predicates:
        if get_texts_for_concept(concept_id, predicate=predicate, limit=1):
            return True
    return False


def _create_concept_if_missing(
    *,
    concept_id: str,
    name: str,
    parent_concept_ids: Sequence[str],
    create_as_instance: bool,
    description: str = "",
    notes: str | None = None,
    system_tags: Sequence[str] = (),
) -> bool:
    if isinstance(_safe_get_concept(concept_id), Mapping):
        return False
    concept_service.create_concept(
        name=name,
        concept_id=concept_id,
        description=description,
        notes=notes,
        parent_concept_ids=list(parent_concept_ids),
        create_as_instance=create_as_instance,
        system_tags=list(system_tags),
        visibility_scope_mode="global_general",
    )
    return True


def _create_type_from_spec(spec: ConceptSpec) -> bool:
    return _create_concept_if_missing(
        concept_id=spec.concept_id,
        name=spec.name,
        parent_concept_ids=[spec.parent_concept_id],
        create_as_instance=False,
        description=spec.description,
        notes=spec.notes,
        system_tags=("ontology", "ai_assisted_programming", "chat_session"),
    )


def _create_predicate_from_spec(spec: ConceptSpec) -> bool:
    return _create_concept_if_missing(
        concept_id=spec.concept_id,
        name=spec.name,
        parent_concept_ids=["#V#predicate"],
        create_as_instance=True,
        description=spec.description,
        notes=spec.notes,
        system_tags=("ontology", "predicate", "ai_chat_session_source_profile"),
    )


def _definition_json(definition: Mapping[str, Any]) -> str:
    return json.dumps(
        definition, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def ensure_canonical_ai_chat_session_source_profiles_from_seed_fixture(
    *,
    fixture_path: Path | str = _SEED_FIXTURE_PATH,
    definition_language: str = "en-NZ",
    text_relation_policy: str = "replace_others",
    create_missing_concepts: bool = True,
    overwrite_existing: bool = False,
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    catalogue_definition, profile_definitions, fixture_metadata = (
        load_ai_chat_session_source_profiles_from_seed_fixture(fixture_path)
    )
    provenance_payload = dict(provenance) if isinstance(provenance, Mapping) else None
    context_payload = (
        dict(context)
        if isinstance(context, Mapping)
        else {
            "source": "ai_chat_session_source_profile_vontology_service",
            "seed_schema_version": fixture_metadata["seed_schema_version"],
        }
    )

    created_concept_ids: list[str] = []
    persisted_definition_concept_ids: list[str] = []
    skipped_existing_definition_concept_ids: list[str] = []
    relationship_targets: list[tuple[str, str, str]] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    try:
        _catalogue_definition, components = _normalise_catalogue_definition(
            catalogue_definition,
            catalogue_concept_id=str(catalogue_definition["catalogue_concept_id"]),
            source="seed_bundle_import_fixture",
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"catalogue_definition_invalid:{exc}",
            "fixture": fixture_metadata,
        }

    concept_specs: list[ConceptSpec] = [
        components["base_document_type"],
        components["base_file_copy_type"],
    ]
    predicate_specs: list[ConceptSpec] = [
        components["document_has_file_predicate"],
        components["legacy_document_has_file_copy_predicate"],
        components["file_for_document_predicate"],
        ConceptSpec(
            concept_id=HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON,
            name="has_ai_chat_session_source_profile_catalogue_definition_json",
            parent_concept_id="#V#predicate",
            description=(
                "Text relation predicate storing a represented AI coding-session "
                "source-profile catalogue definition as JSON."
            ),
        ),
        ConceptSpec(
            concept_id=HAS_SOURCE_PROFILE_DEFINITION_JSON,
            name="has_ai_chat_session_source_profile_definition_json",
            parent_concept_id="#V#predicate",
            description=(
                "Text relation predicate storing one represented AI coding-session "
                "source profile definition as JSON."
            ),
        ),
        ConceptSpec(
            concept_id=HAS_SOURCE_PROFILE,
            name="has_ai_chat_session_source_profile",
            parent_concept_id="#V#predicate",
            description=(
                "Relates an AI coding-session source-profile catalogue to a source "
                "profile concept."
            ),
        ),
        ConceptSpec(
            concept_id=HAS_SOURCE_PROFILE_DOCUMENT_TYPE,
            name="has_ai_chat_session_document_type",
            parent_concept_id="#V#predicate",
            description=(
                "Relates an AI coding-session source profile to the document type "
                "used for imported abstract session documents."
            ),
        ),
        ConceptSpec(
            concept_id=HAS_SOURCE_PROFILE_FILE_COPY_TYPE,
            name="has_ai_chat_session_file_copy_type",
            parent_concept_id="#V#predicate",
            description=(
                "Relates an AI coding-session source profile to the file-copy type "
                "used for imported session bytes."
            ),
        ),
    ]

    profile_by_concept_id: dict[str, AIChatSessionSourceProfile] = {}
    for definition in profile_definitions:
        try:
            profile, _normalised = _normalise_profile_definition(
                definition,
                profile_concept_id=str(definition["profile_concept_id"]),
                source="seed_bundle_import_fixture",
            )
        except Exception as exc:
            errors_by_concept_id[
                _safe_str(definition.get("profile_concept_id")) or "unknown_profile"
            ] = f"profile_definition_invalid:{exc}"
            continue
        profile_by_concept_id[profile.profile_concept_id] = profile
        concept_specs.append(profile.document_type)
        concept_specs.append(profile.file_copy_type)

    if create_missing_concepts:
        try:
            if _create_concept_if_missing(
                concept_id=SOURCE_PROFILE_TYPE_ID,
                name="AI-assisted programming chat-session source profile",
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
                description=(
                    "Type for represented source profiles that govern ingestion of "
                    "AI-assisted programming chat-session transcripts."
                ),
                system_tags=("ontology", "ai_assisted_programming", "chat_session"),
            ):
                created_concept_ids.append(SOURCE_PROFILE_TYPE_ID)
        except Exception as exc:
            errors_by_concept_id[SOURCE_PROFILE_TYPE_ID] = f"type_create_failed:{exc}"

        for spec in concept_specs:
            try:
                if _create_type_from_spec(spec):
                    created_concept_ids.append(spec.concept_id)
            except Exception as exc:
                errors_by_concept_id[spec.concept_id] = f"type_create_failed:{exc}"

        for spec in predicate_specs:
            try:
                if _create_predicate_from_spec(spec):
                    created_concept_ids.append(spec.concept_id)
            except Exception as exc:
                errors_by_concept_id[spec.concept_id] = f"predicate_create_failed:{exc}"

        catalogue_id = str(catalogue_definition["catalogue_concept_id"])
        try:
            if _create_concept_if_missing(
                concept_id=catalogue_id,
                name=str(catalogue_definition["catalogue_title"]),
                parent_concept_ids=["#V#thing"],
                create_as_instance=True,
                description=str(
                    catalogue_definition.get("catalogue_description") or ""
                ),
                system_tags=("ontology", "ai_assisted_programming", "chat_session"),
            ):
                created_concept_ids.append(catalogue_id)
        except Exception as exc:
            errors_by_concept_id[catalogue_id] = f"catalogue_create_failed:{exc}"

        for profile in profile_by_concept_id.values():
            try:
                if _create_concept_if_missing(
                    concept_id=profile.profile_concept_id,
                    name=profile.display_name,
                    parent_concept_ids=[SOURCE_PROFILE_TYPE_ID],
                    create_as_instance=True,
                    description=(
                        "Represented source profile for AI-assisted programming "
                        f"chat sessions from {profile.environment}."
                    ),
                    system_tags=(
                        "ontology",
                        "ai_assisted_programming",
                        "chat_session",
                        "source_profile",
                    ),
                ):
                    created_concept_ids.append(profile.profile_concept_id)
            except Exception as exc:
                errors_by_concept_id[profile.profile_concept_id] = (
                    f"profile_create_failed:{exc}"
                )

    for concept_id in [
        str(catalogue_definition["catalogue_concept_id"]),
        *(profile.profile_concept_id for profile in profile_by_concept_id.values()),
    ]:
        if not isinstance(_safe_get_concept(concept_id), Mapping):
            missing_concept_ids.append(concept_id)

    if not missing_concept_ids:
        catalogue_id = str(catalogue_definition["catalogue_concept_id"])
        if overwrite_existing or not _existing_definition(
            catalogue_id,
            _CATALOGUE_TEXT_PREDICATES,
        ):
            try:
                upsert_singleton_text_relation(
                    subject_concept_id=catalogue_id,
                    predicate=HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON,
                    lang=definition_language,
                    text=_definition_json(catalogue_definition),
                    policy=text_relation_policy,
                    provenance=provenance_payload,
                    context=context_payload,
                    garbage_collect=True,
                )
                persisted_definition_concept_ids.append(catalogue_id)
            except Exception as exc:
                errors_by_concept_id[catalogue_id] = f"catalogue_upsert_failed:{exc}"
        else:
            skipped_existing_definition_concept_ids.append(catalogue_id)

        for definition in profile_definitions:
            profile_id = str(definition["profile_concept_id"])
            if overwrite_existing or not _existing_definition(
                profile_id,
                _PROFILE_TEXT_PREDICATES,
            ):
                try:
                    upsert_singleton_text_relation(
                        subject_concept_id=profile_id,
                        predicate=HAS_SOURCE_PROFILE_DEFINITION_JSON,
                        lang=definition_language,
                        text=_definition_json(definition),
                        policy=text_relation_policy,
                        provenance=provenance_payload,
                        context=context_payload,
                        garbage_collect=True,
                    )
                    persisted_definition_concept_ids.append(profile_id)
                except Exception as exc:
                    errors_by_concept_id[profile_id] = f"profile_upsert_failed:{exc}"
            else:
                skipped_existing_definition_concept_ids.append(profile_id)

    if not missing_concept_ids:
        catalogue_id = str(catalogue_definition["catalogue_concept_id"])
        for profile in profile_by_concept_id.values():
            for source_id, predicate, target_id in (
                (catalogue_id, HAS_SOURCE_PROFILE, profile.profile_concept_id),
                (
                    profile.profile_concept_id,
                    HAS_SOURCE_PROFILE_DOCUMENT_TYPE,
                    profile.document_type.concept_id,
                ),
                (
                    profile.profile_concept_id,
                    HAS_SOURCE_PROFILE_FILE_COPY_TYPE,
                    profile.file_copy_type.concept_id,
                ),
            ):
                try:
                    result = add_relationship(source_id, predicate, target_id)
                    if result.get("success"):
                        relationship_targets.append((source_id, predicate, target_id))
                except Exception as exc:
                    errors_by_concept_id[f"{source_id}:{predicate}:{target_id}"] = (
                        f"relationship_add_failed:{exc}"
                    )

    return {
        "success": not (missing_concept_ids or errors_by_concept_id),
        "catalogue_concept_id": catalogue_definition["catalogue_concept_id"],
        "source_profile_type_id": SOURCE_PROFILE_TYPE_ID,
        "profile_concept_ids": [
            profile.profile_concept_id for profile in profile_by_concept_id.values()
        ],
        "created_concept_ids": created_concept_ids,
        "persisted_definition_concept_ids": persisted_definition_concept_ids,
        "skipped_existing_definition_concept_ids": (
            skipped_existing_definition_concept_ids
        ),
        "relationship_targets": relationship_targets,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
        "fixture": fixture_metadata,
        "counts": {
            "profiles": len(profile_by_concept_id),
            "created_concepts": len(created_concept_ids),
            "persisted_definitions": len(persisted_definition_concept_ids),
            "skipped_existing_definitions": len(
                skipped_existing_definition_concept_ids
            ),
            "relationships_written": len(relationship_targets),
            "missing_concepts": len(missing_concept_ids),
            "errors": len(errors_by_concept_id),
        },
    }


__all__ = [
    "AIChatSessionSourceProfile",
    "AIChatSessionSourceProfileAuthority",
    "AIChatSessionSourceProfileAuthorityMissingError",
    "ConceptSpec",
    "HAS_SOURCE_PROFILE",
    "HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON",
    "HAS_SOURCE_PROFILE_DEFINITION_JSON",
    "HAS_SOURCE_PROFILE_DOCUMENT_TYPE",
    "HAS_SOURCE_PROFILE_FILE_COPY_TYPE",
    "SOURCE_PROFILE_CATALOGUE_CONCEPT_ID",
    "SOURCE_PROFILE_CATALOGUE_SCHEMA_VERSION",
    "SOURCE_PROFILE_DEFINITION_SCHEMA_VERSION",
    "SOURCE_PROFILE_SEED_SCHEMA_VERSION",
    "SOURCE_PROFILE_TYPE_ID",
    "ensure_canonical_ai_chat_session_source_profiles_from_seed_fixture",
    "load_ai_chat_session_source_profile_authority",
    "load_ai_chat_session_source_profiles_from_seed_fixture",
]
