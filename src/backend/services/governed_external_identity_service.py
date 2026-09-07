"""Exact external identity binding for one governed concept creation effect."""

from .concept_external_identity_service import (
    ExternalIdentityInputError,
    normalise_create_external_identifiers,
    canonical_concept_id_for_external_identifiers,
    resolve_external_identity_candidates,
    external_identity_marker,
)
from ..security.access_control import (
    get_effective_user_concept_id,
    get_effective_organisation_concept_id,
)


def bind_external_create_identity(spec, *, parent_id, scope_mode):
    """Resolve a single opaque identity without name-based merging or writes."""
    from .ontology_mutation_command_service import OntologyMutationCommandError

    try:
        identifiers = normalise_create_external_identifiers(
            external_identifiers=spec.get("external_identifiers"),
            concept_name=spec.get("name"),
            kind=spec.get("kind"),
        )
    except ExternalIdentityInputError as exc:
        raise OntologyMutationCommandError(exc.error_code, str(exc)) from exc
    if not get_effective_user_concept_id():
        raise OntologyMutationCommandError(
            "authenticated_actor_context_required",
            "Trusted actor context is required for external concept adoption",
        )
    if len(identifiers) != 1:
        raise OntologyMutationCommandError(
            "single_external_identity_required",
            "A governed create supports exactly one external identity",
        )
    canonical = canonical_concept_id_for_external_identifiers(
        identifiers,
        kind=spec.get("kind"),
        parent_id=parent_id,
        scope_mode=scope_mode or "user_only_default",
        actor_user_id=get_effective_user_concept_id(),
        actor_org_id=get_effective_organisation_concept_id(),
    )
    resolution = resolve_external_identity_candidates(
        identifiers[0], canonical_concept_id_candidates=[canonical]
    )
    if resolution.status == "resolved":
        canonical = resolution.candidate_concept_ids[0]
    elif resolution.status != "not_found":
        raise OntologyMutationCommandError(
            "external_identity_requires_review",
            "External identity is ambiguous or unverified; inspect existing evidence before adoption",
        )
    return {
        **spec,
        "concept_id": canonical,
        "external_identifiers": [
            {"scheme": identifiers[0].scheme, "value": identifiers[0].value}
        ],
    }


def external_identity_texts_present(spec, rows):
    identifiers = normalise_create_external_identifiers(
        external_identifiers=spec.get("external_identifiers"),
        concept_name=spec.get("name"),
        kind=spec.get("kind"),
    )
    return bool(identifiers) and all(
        any(
            row.get("predicate") == "hasName"
            and row.get("text") == external_identity_marker(identifier)
            and row.get("context", {}).get("identity_marker") is True
            for row in rows
        )
        for identifier in identifiers
    )
