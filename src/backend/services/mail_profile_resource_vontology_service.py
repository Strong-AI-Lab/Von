"""Vontology KR materialisation for mail profile resources.

The profile resolver workflow needs represented user-to-mail-profile facts. This
module seeds the vocabulary and safe, non-secret resource instances for runtime
profile aliases; it does not change Gmail tool behaviour or provide hidden
runtime defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from . import concept_service
from .relationship_write_service import add_relationship
from .tool_evidence_contract_vontology_service import (
    BINARY_PREDICATE_TYPE_ID,
    PREDICATE_TYPE_ID,
    bootstrap_tool_evidence_contract_vocabulary,
)
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
    suspend_event_workflow_integration,
)

MAIL_PROFILE_RESOURCE_SCHEMA_VERSION = "mail_profile_resource.v2"
MAIL_PROFILE_RESOURCE_SOURCE_TAG = "JVNAUTOSCI-2295"
MAIL_PROFILE_RESOURCE_MANAGED_BY = "mail_profile_resource_vontology_service"

MAIL_PROFILE_RESOURCE_TYPE_ID = "#V#mail_profile_resource"
GMAIL_PROFILE_RESOURCE_TYPE_ID = "#V#gmail_profile_resource"
MAIL_PROFILE_RUNTIME_ALIAS_TYPE_ID = "#V#mail_profile_runtime_alias"
HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID = "#V#has_authorised_mail_profile"
HAS_DEFAULT_MAIL_PROFILE_PREDICATE_ID = "#V#has_default_mail_profile"
HAS_RUNTIME_PROFILE_ALIAS_PREDICATE_ID = "#V#has_runtime_profile_alias"
HAS_OAUTH_SCOPE_PREDICATE_ID = "#V#has_oauth_scope"
MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID = (
    "#V#mail_profile_represents_identity"
)


@dataclass(frozen=True)
class ConceptSpec:
    concept_id: str
    name: str
    description: str
    parent_concept_ids: tuple[str, ...]
    create_as_instance: bool
    category: str
    attributes: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RelationshipSpec:
    source_id: str
    predicate: str
    target_id: str


def _base_attributes(category: str) -> dict[str, Any]:
    return {
        "repo_seed_source_tag": MAIL_PROFILE_RESOURCE_SOURCE_TAG,
        "repo_seed_managed_by": MAIL_PROFILE_RESOURCE_MANAGED_BY,
        "schema_version": MAIL_PROFILE_RESOURCE_SCHEMA_VERSION,
        "mail_profile_resource_category": category,
    }


def _concept(
    *,
    concept_id: str,
    name: str,
    description: str,
    parent_concept_ids: tuple[str, ...],
    create_as_instance: bool,
    category: str,
    attributes: Mapping[str, Any] | None = None,
) -> ConceptSpec:
    merged_attributes = _base_attributes(category)
    if attributes:
        merged_attributes.update(dict(attributes))
    return ConceptSpec(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=parent_concept_ids,
        create_as_instance=create_as_instance,
        category=category,
        attributes=merged_attributes,
    )


_VOCABULARY_CONCEPT_SPECS: tuple[ConceptSpec, ...] = (
    _concept(
        concept_id=MAIL_PROFILE_RESOURCE_TYPE_ID,
        name="Mail profile resource",
        description=(
            "Type for represented mail profile resources that a user is "
            "authorised to use through a mail tool."
        ),
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="resource_type",
    ),
    _concept(
        concept_id=GMAIL_PROFILE_RESOURCE_TYPE_ID,
        name="Gmail profile resource",
        description=(
            "Type for represented Gmail profile resources linked to runtime "
            "Gmail profile aliases."
        ),
        parent_concept_ids=(MAIL_PROFILE_RESOURCE_TYPE_ID,),
        create_as_instance=False,
        category="resource_type",
    ),
    _concept(
        concept_id=MAIL_PROFILE_RUNTIME_ALIAS_TYPE_ID,
        name="Mail profile runtime alias",
        description=(
            "Type for non-secret runtime alias concepts whose names identify "
            "configured mail profile ids accepted by mail tools."
        ),
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="alias_type",
    ),
    _concept(
        concept_id=HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID,
        name="has authorised mail profile",
        description=(
            "Predicate linking a user concept to a represented mail profile "
            "resource that the user is authorised to use."
        ),
        parent_concept_ids=(PREDICATE_TYPE_ID, BINARY_PREDICATE_TYPE_ID),
        create_as_instance=True,
        category="predicate",
    ),
    _concept(
        concept_id=HAS_DEFAULT_MAIL_PROFILE_PREDICATE_ID,
        name="has default mail profile",
        description=(
            "Predicate linking a user concept to the represented mail profile "
            "resource that should be used when no explicit profile is named."
        ),
        parent_concept_ids=(PREDICATE_TYPE_ID, BINARY_PREDICATE_TYPE_ID),
        create_as_instance=True,
        category="predicate",
    ),
    _concept(
        concept_id=HAS_RUNTIME_PROFILE_ALIAS_PREDICATE_ID,
        name="has runtime profile alias",
        description=(
            "Predicate linking a represented mail profile resource to a "
            "non-secret runtime alias concept accepted by the corresponding "
            "mail tool profile argument."
        ),
        parent_concept_ids=(PREDICATE_TYPE_ID, BINARY_PREDICATE_TYPE_ID),
        create_as_instance=True,
        category="predicate",
    ),
    _concept(
        concept_id=HAS_OAUTH_SCOPE_PREDICATE_ID,
        name="has OAuth scope",
        description=(
            "Predicate representing the OAuth scope list associated with a "
            "Gmail profile resource concept. Scope values are stored as the "
            "oauth_scopes attribute on the profile concept for runtime lookup "
            "by the OAuth flow and Gmail MCP tools."
        ),
        parent_concept_ids=(PREDICATE_TYPE_ID, BINARY_PREDICATE_TYPE_ID),
        create_as_instance=True,
        category="predicate",
    ),
    _concept(
        concept_id=MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID,
        name="mail profile represents identity",
        description=(
            "Predicate linking a mail profile resource to the person, agent, "
            "organisation, team, or other represented identity whose mailbox "
            "the profile denotes. This differs from who may use the profile "
            "and from which profile is that actor's default."
        ),
        parent_concept_ids=(PREDICATE_TYPE_ID, BINARY_PREDICATE_TYPE_ID),
        create_as_instance=True,
        category="predicate",
    ),
)


def normalise_profile_id_for_concept_id(profile_id: str) -> str:
    """Return a stable concept-id-safe suffix for a runtime profile id."""

    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", profile_id.strip().lower()).strip("_")
    return cleaned or "unnamed"


def gmail_profile_resource_concept_id(profile_id: str) -> str:
    return f"#V#gmail_profile_{normalise_profile_id_for_concept_id(profile_id)}"


def gmail_profile_alias_concept_id(profile_id: str) -> str:
    return f"#V#gmail_runtime_profile_alias_{normalise_profile_id_for_concept_id(profile_id)}"


def mail_profile_resource_represents_identity(
    *,
    profile_resource_concept_id: str,
    identity_concept_id: str,
) -> bool:
    """Return whether one represented mail profile denotes ``identity_concept_id``.

    Runtime aliases and actor-to-profile authority do not establish mailbox
    identity.  Callers that depend on that meaning must read the dedicated
    relation from the canonical profile resource and fail closed when the
    resource or relation is unavailable.
    """

    resource_id = str(profile_resource_concept_id or "").strip()
    identity_id = str(identity_concept_id or "").strip()
    if not resource_id.startswith("#V#") or not identity_id.startswith("#V#"):
        return False

    resource_doc = load_concept(resource_id)
    if not isinstance(resource_doc, Mapping):
        return False
    relationships = resource_doc.get("relationships")
    relation_map = relationships if isinstance(relationships, Mapping) else {}
    represented_identity_ids = normalise_relationship_targets(
        relation_map.get(MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID)
    )
    return identity_id in represented_identity_ids


def list_authorised_gmail_profiles_for_user(
    *,
    user_concept_id: str,
) -> dict[str, Any]:
    """List represented Gmail profile choices within one actor's authority.

    The result deliberately contains only non-secret represented resource and
    runtime-alias identities. Runtime configuration and OAuth mailbox labels
    are intersected at the request boundary, where they can be kept scoped to
    the authenticated actor rather than materialised as global profile facts.
    """

    cleaned_user_id = str(user_concept_id or "").strip()
    user_doc = load_concept(cleaned_user_id)
    if not isinstance(user_doc, Mapping):
        return {
            "success": False,
            "reason_code": "mail_profile_actor_not_represented",
            "profiles": [],
        }

    relationships = user_doc.get("relationships")
    relation_map = relationships if isinstance(relationships, Mapping) else {}
    authorised_resource_ids = normalise_relationship_targets(
        relation_map.get(HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID)
    )
    default_resource_ids = set(
        normalise_relationship_targets(
            relation_map.get(HAS_DEFAULT_MAIL_PROFILE_PREDICATE_ID)
        )
    )

    profiles: list[dict[str, Any]] = []
    for resource_id in authorised_resource_ids:
        resource_doc = load_concept(resource_id)
        if not isinstance(resource_doc, Mapping):
            continue
        attributes = resource_doc.get("attributes")
        if not isinstance(attributes, Mapping):
            continue
        alias = attributes.get("runtime_profile_alias")
        if not isinstance(alias, str) or not alias.strip():
            continue
        profiles.append(
            {
                "profile_id": alias.strip(),
                "profile_resource_concept_id": resource_id,
                "is_default": resource_id in default_resource_ids,
                "represented_identity_concept_ids": normalise_relationship_targets(
                    (resource_doc.get("relationships") or {}).get(
                        MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID
                    )
                ),
            }
        )

    return {
        "success": True,
        "reason_code": "authorised_mail_profiles_resolved",
        "profiles": profiles,
    }


def resolve_authorised_gmail_profile_for_user(
    *,
    user_concept_id: str,
    requested_profile_id: str | None = None,
) -> dict[str, Any]:
    """Resolve one runtime Gmail alias from represented user authority.

    This is a read-only authority check. It neither enumerates globally
    configured profiles nor infers ownership from a runtime alias alone.
    """

    requested = str(requested_profile_id or "").strip() or None
    authority = list_authorised_gmail_profiles_for_user(
        user_concept_id=user_concept_id,
    )
    if not authority.get("success"):
        return {
            "success": False,
            "reason_code": authority.get("reason_code")
            or "mail_profile_actor_not_represented",
            "profile_id": None,
        }

    profiles = [
        dict(item)
        for item in authority.get("profiles") or []
        if isinstance(item, Mapping)
    ]

    selected_resource_id: str | None = None
    selected_profile_id: str | None = None
    if requested is not None:
        for profile in profiles:
            resource_id = str(profile.get("profile_resource_concept_id") or "")
            alias = str(profile.get("profile_id") or "")
            if requested in {resource_id, alias}:
                selected_resource_id = resource_id
                selected_profile_id = alias
                break
        if selected_resource_id is None:
            return {
                "success": False,
                "reason_code": "mail_profile_not_authorised_for_actor",
                "profile_id": None,
            }
    else:
        default_profiles = [
            profile for profile in profiles if profile.get("is_default") is True
        ]
        if len(default_profiles) > 1:
            return {
                "success": False,
                "reason_code": "multiple_default_mail_profiles_represented",
                "profile_id": None,
            }
        selected_profile = default_profiles[0] if default_profiles else None
        selection_source = "represented_default"
        if selected_profile is None and len(profiles) == 1:
            selected_profile = profiles[0]
            selection_source = "sole_authorised_profile"
        if selected_profile is not None:
            selected_resource_id = str(
                selected_profile.get("profile_resource_concept_id") or ""
            )
            selected_profile_id = str(selected_profile.get("profile_id") or "")
        if selected_resource_id is None:
            return {
                "success": False,
                "reason_code": "default_mail_profile_not_represented",
                "profile_id": None,
            }

    return {
        "success": True,
        "reason_code": "authorised_mail_profile_resolved",
        "profile_id": selected_profile_id,
        "profile_resource_concept_id": selected_resource_id,
        "selection_source": "request" if requested is not None else selection_source,
    }


def _ensure_structural_targets(
    *,
    concept_id: str,
    relationship_key: str,
    target_ids: Sequence[str],
) -> bool:
    concept_doc = load_concept(concept_id)
    if not isinstance(concept_doc, Mapping):
        return False

    existing_targets = normalise_relationship_targets(
        (concept_doc.get("relationships") or {}).get(relationship_key)
    )
    updated_targets = list(existing_targets)
    for target_id in target_ids:
        cleaned = str(target_id or "").strip()
        if cleaned and cleaned not in updated_targets:
            updated_targets.append(cleaned)

    if updated_targets == existing_targets:
        return False

    concept_service.update_concept(
        concept_id,
        {f"relationships.{relationship_key}": updated_targets},
    )
    return True


def _ensure_concept(spec: ConceptSpec) -> str:
    concept_doc = load_concept(spec.concept_id)
    if not isinstance(concept_doc, Mapping):
        concept_service.create_concept(
            name=spec.name,
            concept_id=spec.concept_id,
            description=spec.description,
            parent_concept_ids=list(spec.parent_concept_ids),
            create_as_instance=spec.create_as_instance,
            attributes=dict(spec.attributes or {}),
            system_tags=[
                "mail_profile_resource",
                MAIL_PROFILE_RESOURCE_SOURCE_TAG,
            ],
            visibility_scope_mode="global_general",
        )
        return "created"

    relationship_key = "is_an_instance_of" if spec.create_as_instance else "is_a_type_of"
    repaired = _ensure_structural_targets(
        concept_id=spec.concept_id,
        relationship_key=relationship_key,
        target_ids=spec.parent_concept_ids,
    )
    return "repaired" if repaired else "existing"


def _ensure_relationship(spec: RelationshipSpec) -> dict[str, Any]:
    result = add_relationship(
        source_id=spec.source_id,
        predicate=spec.predicate,
        target=spec.target_id,
    )
    return dict(result) if isinstance(result, Mapping) else {"success": False}


def _profile_concept_specs(
    profile_ids: Sequence[str],
    profile_scopes: Mapping[str, Sequence[str]] | None = None,
) -> tuple[ConceptSpec, ...]:
    specs: list[ConceptSpec] = []
    seen: set[str] = set()
    for raw_profile_id in profile_ids:
        profile_id = str(raw_profile_id or "").strip()
        if not profile_id or profile_id in seen:
            continue
        seen.add(profile_id)
        resource_concept_id = gmail_profile_resource_concept_id(profile_id)
        alias_concept_id = gmail_profile_alias_concept_id(profile_id)
        extra_attrs: dict[str, Any] = {"runtime_profile_alias": profile_id}
        if profile_scopes and profile_id in profile_scopes:
            extra_attrs["oauth_scopes"] = list(profile_scopes[profile_id])
        specs.append(
            _concept(
                concept_id=resource_concept_id,
                name=f"Gmail profile resource {profile_id}",
                description=(
                    "Represented Gmail profile resource for runtime profile alias "
                    f"{profile_id}. This concept carries no token path or secret."
                ),
                parent_concept_ids=(GMAIL_PROFILE_RESOURCE_TYPE_ID,),
                create_as_instance=True,
                category="gmail_profile_resource",
                attributes=extra_attrs,
            )
        )
        specs.append(
            _concept(
                concept_id=alias_concept_id,
                name=profile_id,
                description=(
                    "Non-secret runtime profile alias accepted by Gmail tool "
                    f"profile arguments: {profile_id}."
                ),
                parent_concept_ids=(MAIL_PROFILE_RUNTIME_ALIAS_TYPE_ID,),
                create_as_instance=True,
                category="runtime_alias",
                attributes={"runtime_profile_alias": profile_id},
            )
        )
    return tuple(specs)


def _profile_relationship_specs(
    *,
    user_concept_id: str,
    profile_ids: Sequence[str],
    default_profile_id: str | None,
    profile_identity_concept_ids: Mapping[str, str] | None = None,
) -> tuple[RelationshipSpec, ...]:
    specs: list[RelationshipSpec] = []
    seen: set[str] = set()
    default_id = str(default_profile_id or "").strip()
    for raw_profile_id in profile_ids:
        profile_id = str(raw_profile_id or "").strip()
        if not profile_id or profile_id in seen:
            continue
        seen.add(profile_id)
        resource_concept_id = gmail_profile_resource_concept_id(profile_id)
        alias_concept_id = gmail_profile_alias_concept_id(profile_id)
        specs.append(
            RelationshipSpec(
                source_id=user_concept_id,
                predicate=HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID,
                target_id=resource_concept_id,
            )
        )
        specs.append(
            RelationshipSpec(
                source_id=resource_concept_id,
                predicate=HAS_RUNTIME_PROFILE_ALIAS_PREDICATE_ID,
                target_id=alias_concept_id,
            )
        )
        if default_id and profile_id == default_id:
            specs.append(
                RelationshipSpec(
                    source_id=user_concept_id,
                    predicate=HAS_DEFAULT_MAIL_PROFILE_PREDICATE_ID,
                    target_id=resource_concept_id,
                )
            )
        identity_concept_id = str(
            (profile_identity_concept_ids or {}).get(profile_id) or ""
        ).strip()
        if identity_concept_id:
            specs.append(
                RelationshipSpec(
                    source_id=resource_concept_id,
                    predicate=MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID,
                    target_id=identity_concept_id,
                )
            )
    return tuple(specs)


def bootstrap_mail_profile_resource_vocabulary() -> dict[str, Any]:
    """Ensure generic mail-profile resource vocabulary exists in Vontology."""

    base_report = bootstrap_tool_evidence_contract_vocabulary()
    concept_status_by_id: dict[str, str] = {}
    errors: list[dict[str, Any]] = []

    with suspend_event_workflow_integration():
        for spec in _VOCABULARY_CONCEPT_SPECS:
            try:
                concept_status_by_id[spec.concept_id] = _ensure_concept(spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "concepts",
                        "concept_id": spec.concept_id,
                        "reason_code": str(exc),
                    }
                )

    return {
        "success": not errors and bool(base_report.get("success")),
        "schema_version": MAIL_PROFILE_RESOURCE_SCHEMA_VERSION,
        "source_tag": MAIL_PROFILE_RESOURCE_SOURCE_TAG,
        "base_vocabulary_report": base_report,
        "concept_status_by_id": concept_status_by_id,
        "counts": {"concept_specs": len(_VOCABULARY_CONCEPT_SPECS)},
        "errors": errors,
    }


def materialise_gmail_profile_resources_for_user(
    *,
    user_concept_id: str,
    profile_ids: Sequence[str],
    default_profile_id: str | None = None,
    profile_scopes: Mapping[str, Sequence[str]] | None = None,
    profile_identity_concept_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Materialise safe Gmail profile resource facts for a user.

    ``profile_ids`` are non-secret runtime aliases such as ``vonwitbrock-gmail``.
    Token paths, credentials paths, and email contents must not be passed here.
    """

    cleaned_user_concept_id = str(user_concept_id or "").strip()
    cleaned_profile_ids = [
        str(profile_id or "").strip()
        for profile_id in profile_ids
        if str(profile_id or "").strip()
    ]
    if not cleaned_user_concept_id.startswith("#V#"):
        return {
            "success": False,
            "reason_code": "invalid_user_concept_id",
            "errors": [
                {
                    "section": "inputs",
                    "reason_code": "invalid_user_concept_id",
                    "user_concept_id": cleaned_user_concept_id,
                }
            ],
        }

    vocabulary_report = bootstrap_mail_profile_resource_vocabulary()
    concept_status_by_id: dict[str, str] = {}
    relationship_ids: list[str] = []
    errors: list[dict[str, Any]] = []

    if not vocabulary_report.get("success"):
        errors.append(
            {
                "section": "vocabulary",
                "reason_code": "mail_profile_resource_vocabulary_invalid",
                "details": vocabulary_report,
            }
        )

    concept_specs = _profile_concept_specs(cleaned_profile_ids, profile_scopes)
    relationship_specs = _profile_relationship_specs(
        user_concept_id=cleaned_user_concept_id,
        profile_ids=cleaned_profile_ids,
        default_profile_id=default_profile_id,
        profile_identity_concept_ids=profile_identity_concept_ids,
    )

    with suspend_event_workflow_integration():
        for spec in concept_specs:
            try:
                concept_status_by_id[spec.concept_id] = _ensure_concept(spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "concepts",
                        "concept_id": spec.concept_id,
                        "reason_code": str(exc),
                    }
                )

        if profile_scopes:
            for profile_id in cleaned_profile_ids:
                scopes_for_profile = profile_scopes.get(profile_id)
                if not scopes_for_profile:
                    continue
                resource_concept_id = gmail_profile_resource_concept_id(profile_id)
                try:
                    concept_service.update_concept(
                        resource_concept_id,
                        {"attributes.oauth_scopes": list(scopes_for_profile)},
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "section": "scope_attributes",
                            "profile_id": profile_id,
                            "reason_code": str(exc),
                        }
                    )

        for relationship_spec in relationship_specs:
            try:
                result = _ensure_relationship(relationship_spec)
                if result.get("success"):
                    relationship_id = str(result.get("relationship_id") or "").strip()
                    if relationship_id:
                        relationship_ids.append(relationship_id)
                else:
                    errors.append(
                        {
                            "section": "relationships",
                            "relationship": relationship_spec,
                            "reason_code": "relationship_write_failed",
                            "details": result,
                        }
                    )
            except Exception as exc:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": relationship_spec,
                        "reason_code": str(exc),
                    }
                )

    return {
        "success": not errors,
        "schema_version": MAIL_PROFILE_RESOURCE_SCHEMA_VERSION,
        "source_tag": MAIL_PROFILE_RESOURCE_SOURCE_TAG,
        "user_concept_id": cleaned_user_concept_id,
        "profile_ids": cleaned_profile_ids,
        "default_profile_id": default_profile_id,
        "vocabulary_report": vocabulary_report,
        "concept_status_by_id": concept_status_by_id,
        "relationship_ids": relationship_ids,
        "counts": {
            "profile_ids": len(cleaned_profile_ids),
            "concept_specs": len(concept_specs),
            "relationship_specs": len(relationship_specs),
            "relationships_written": len(relationship_ids),
        },
        "errors": errors,
    }


def validate_mail_profile_resource_vocabulary() -> dict[str, Any]:
    missing_concept_ids: list[str] = []
    for spec in _VOCABULARY_CONCEPT_SPECS:
        if not isinstance(load_concept(spec.concept_id), Mapping):
            missing_concept_ids.append(spec.concept_id)

    return {
        "success": not missing_concept_ids,
        "schema_version": MAIL_PROFILE_RESOURCE_SCHEMA_VERSION,
        "missing_concept_ids": missing_concept_ids,
    }


__all__ = [
    "GMAIL_PROFILE_RESOURCE_TYPE_ID",
    "HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID",
    "HAS_DEFAULT_MAIL_PROFILE_PREDICATE_ID",
    "HAS_OAUTH_SCOPE_PREDICATE_ID",
    "HAS_RUNTIME_PROFILE_ALIAS_PREDICATE_ID",
    "MAIL_PROFILE_RESOURCE_TYPE_ID",
    "MAIL_PROFILE_RUNTIME_ALIAS_TYPE_ID",
    "MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID",
    "bootstrap_mail_profile_resource_vocabulary",
    "gmail_profile_alias_concept_id",
    "gmail_profile_resource_concept_id",
    "list_authorised_gmail_profiles_for_user",
    "mail_profile_resource_represents_identity",
    "materialise_gmail_profile_resources_for_user",
    "normalise_profile_id_for_concept_id",
    "resolve_authorised_gmail_profile_for_user",
    "validate_mail_profile_resource_vocabulary",
]
