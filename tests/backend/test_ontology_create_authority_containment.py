from __future__ import annotations

from typing import Any

import pytest
from flask import Flask


def _parent_resolution(parent_id: str):
    from src.backend.services.create_concepts_parent_resolution_service import (
        ParentResolutionResult,
    )

    return ParentResolutionResult(
        requested_parent_id=parent_id,
        canonical_parent_id=parent_id,
        resolved_parent_id=parent_id,
        fallback_used=False,
        fallback_candidates_checked=(),
        fallback_selected_parent_id=None,
        resolved_parent_kind="type",
    )


def test_ordinary_create_exposes_scope_choice_but_binds_scope_identity() -> None:
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue

    definition = build_default_catalogue().get("create_concepts")

    assert "scope_mode" not in definition.ordinary_turn_fixed_arguments
    assert definition.ordinary_turn_fixed_arguments[
        "organisation_concept_id"
    ] is None
    assert "organisation_concept_id" not in (
        definition.ordinary_turn_trusted_argument_bindings
    )
    assert definition.input_schema.enum_values["scope_mode"] == [
        "user_only_default",
        "organisation_general",
        "global_general",
        None,
    ]


def test_governed_create_preserves_default_type_and_one_exact_parent(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    resolved = command.resolve_governed_ontology_arguments(
        "create_concepts",
        {
            "concepts": [{"name": "Default type"}],
            "parent_concept_ids": ["#V#parent"],
        },
    )
    assert resolved["concepts"][0]["kind"] == "type"
    assert "scope_mode" not in resolved
    assert "scope_mode" not in command.resolve_governed_ontology_arguments(
        "create_concepts",
        resolved,
    )
    assert resolved["parent_id"] == "#V#parent"
    assert resolved["parent_concept_ids"] == ["#V#parent"]

    with pytest.raises(
        command.OntologyMutationCommandError,
        match="one consistent exact parent",
    ):
        command.resolve_governed_ontology_arguments(
            "create_concepts",
            {
                "concepts": [{"name": "Conflicting parent"}],
                "parent_id": "#V#parent_a",
                "parent_concept_ids": ["#V#parent_b"],
            },
        )


def test_governed_create_uses_trusted_org_and_keeps_omission_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "_concept_exists_unfiltered", lambda _concept_id: False)
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)

    supplied = {
        "parent_id": "#V#research_object",
        "concepts": [
            {
                "concept_id": "#V#lab_record",
                "name": "Lab record",
                "kind": "type",
            }
        ],
        "created_by_concept_id": "#V#spoofed_user",
        "organisation_concept_id": "#V#spoofed_org",
        "org_id": "#V#spoofed_org_alias",
        "namespace": "#V#spoofed_user@spoofed_org",
    }
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        normalised = command.normalise_governed_ontology_arguments(
            "create_concepts",
            supplied,
        )
        resolved = command.resolve_governed_ontology_arguments(
            "create_concepts",
            normalised,
        )
        intent = command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments=resolved,
        )

    assert normalised["created_by_concept_id"] == "#V#member"
    assert normalised["organisation_concept_id"] == "#V#organisation_a"
    assert "org_id" not in normalised
    assert "namespace" not in normalised
    assert normalised["maintain_relationship_inverses"] is False
    assert normalised["resolve_visibility_from_event_namespace"] is False
    assert "scope_mode" not in resolved
    assert intent.publication_context.kind == authority.PublicationContextKind.USER
    assert intent.publication_context.concept_id == "#V#member"
    assert intent.delta["stored_scope"] == {
        "effective_scope_mode": "user_only_default",
        "specific_to_user_concept_ids": ["#V#member"],
        "specific_to_organisation_concept_ids": [],
    }


@pytest.mark.parametrize(
    ("concept", "expected_reason"),
    (
        ({"name": "Ambiguous creation"}, "concept_kind_required"),
        (
            {"name": "Ambiguous creation", "kind": "candidature"},
            "invalid_concept_kind",
        ),
    ),
)
def test_governed_create_requires_explicit_supported_kind(
    concept: dict[str, str], expected_reason: str
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    with pytest.raises(command.OntologyMutationCommandError) as exc_info:
        command.resolve_governed_ontology_arguments(
            "create_concepts",
            {
                "parent_id": "#V#research_object",
                "concepts": [concept],
            },
        )

    assert exc_info.value.reason_code == expected_reason


def test_governed_create_tool_rejects_missing_kind_before_any_effect() -> None:
    from src.backend.integrations.internal_mcp import catalogue

    result = catalogue._create_concepts(
        parent_id="#V#research_object",
        concepts=[{"name": "Ambiguous creation"}],
    )

    assert result["error_code"] == "concept_kind_required"
    assert result["effect_status"] == "not_started"
    assert result["changed"] is False


@pytest.mark.parametrize(
    ("scope_mode", "expected_kind", "role", "role_organisation"),
    (
        (None, "user", None, None),
        (
            "organisation_general",
            "organisation",
            "organisation_ontology_administrator",
            "#V#organisation_a",
        ),
        ("global_general", "global", "global_ontology_administrator", None),
    ),
)
def test_governed_create_authorises_each_exact_scope_with_matching_live_role(
    monkeypatch: pytest.MonkeyPatch,
    scope_mode: str | None,
    expected_kind: str,
    role: str | None,
    role_organisation: str | None,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "_concept_exists_unfiltered", lambda _concept_id: False)
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    evidence = (
        ()
        if role is None
        else (
            authority.AuthorityRoleEvidence(
                role=role,
                actor_concept_id="#V#member",
                organisation_concept_id=role_organisation,
                relation_id=f"role:{role}",
                revision="role-revision-1",
            ),
        )
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda actor: evidence if actor == "#V#member" else (),
    )
    arguments: dict[str, Any] = {
        "parent_id": "#V#research_object",
        "concepts": [
            {
                "concept_id": f"#V#{expected_kind}_creation",
                "name": f"{expected_kind.title()} creation",
                "kind": "type",
            }
        ],
    }
    if scope_mode is not None:
        arguments["scope_mode"] = scope_mode

    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        intent = command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments=arguments,
        )
        decision = authority.authorise_ontology_mutation(intent)

    assert intent.publication_context.kind.value == expected_kind
    assert decision.allowed is True
    assert decision.reason_code == "semantic_ontology_authority_verified"


def test_canonical_relationship_rejects_an_ignored_independent_scope() -> None:
    from src.backend.services import ontology_mutation_command_service as command

    with pytest.raises(command.OntologyMutationCommandError) as exc_info:
        command.resolve_governed_ontology_arguments(
            "add_relationship",
            {
                "source_id": "#V#phd_student",
                "predicate": "#V#supervised_by",
                "target": "#V#supervisor",
                "scope_mode": "organisation",
            },
        )

    assert exc_info.value.reason_code == (
        "canonical_relationship_scope_is_source_bound"
    )
    assert "upsert_scoped_assertion" in exc_info.value.public_message


def test_organisation_create_without_current_org_has_exact_no_effect_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "_concept_exists_unfiltered", lambda _concept_id: False)
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)

    with (
        authority.override_current_actor("#V#member", None),
        pytest.raises(command.OntologyMutationCommandError) as exc_info,
    ):
        command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#research_object",
                "concepts": [
                    {
                        "concept_id": "#V#organisation_creation_without_org",
                        "name": "Organisation creation without org",
                        "kind": "type",
                    }
                ],
                "scope_mode": "organisation_general",
            },
        )

    assert exc_info.value.reason_code == "organisation_context_required"


def test_unauthorised_global_create_does_not_mutate_or_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "_concept_exists_unfiltered", lambda _concept_id: False)
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(authority, "resolve_live_semantic_roles", lambda _actor: ())
    mutation_calls = 0

    def mutate() -> dict[str, Any]:
        nonlocal mutation_calls
        mutation_calls += 1
        return {"success": True, "changed": True}

    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#research_object",
                "concepts": [
                    {
                        "concept_id": "#V#unauthorised_global_creation",
                        "name": "Unauthorised global creation",
                        "kind": "type",
                    }
                ],
                "scope_mode": "global_general",
            },
            mutate=mutate,
        )

    assert mutation_calls == 0
    assert result["success"] is False
    assert result["changed"] is False
    assert result["effect_status"] == "not_started"
    assert result["error_code"] == "global_ontology_admin_authority_required"
    assert result["required_authority"]["authority_kind"] == (
        "global_ontology_administrator"
    )
    assert result["authority_recovery"] == {
        "action_type": "ask_for_exact_ontology_authority",
        "required_authority": result["required_authority"],
        "automatic_scope_change_allowed": False,
    }
    assert "scope_selection" not in result


def test_governed_create_rejects_conflicting_instance_types_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    mutation_calls = 0

    def mutate() -> dict[str, Any]:
        nonlocal mutation_calls
        mutation_calls += 1
        return {"success": True, "changed": True}

    with authority.override_current_actor("#V#member", None):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#research_object",
                "instance_of_type": "#V#top_level_type",
                "concepts": [
                    {
                        "concept_id": "#V#typed_record",
                        "name": "Typed record",
                        "kind": "type",
                        "instance_of_type": "#V#per_concept_type",
                    }
                ],
                "scope_mode": "user_only_default",
            },
            mutate=mutate,
        )

    assert mutation_calls == 0
    assert result["success"] is False
    assert result["effect_status"] == "not_started"
    assert result["changed"] is False
    assert result["error_code"] == "conflicting_create_instance_of_type"

    resolved = command.resolve_governed_ontology_arguments(
        "create_concepts",
        {
            "parent_id": "#V#research_object",
            "instance_of_type": "#V#same_type",
            "concepts": [
                {
                    "concept_id": "#V#typed_record",
                    "name": "Typed record",
                    "kind": "type",
                    "instance_of_type": "#V#same_type",
                }
            ],
        },
    )
    assert "instance_of_type" not in resolved
    assert resolved["concepts"][0]["instance_of_type"] == "#V#same_type"


def test_create_intent_fingerprints_exact_scope_and_complete_effect(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)

    base_arguments = {
        "parent_id": "#V#research_object",
        "concepts": [
            {
                "concept_id": "#V#candidate_record",
                "name": "Candidate record",
                "kind": "instance",
                "description": "Exact description",
                "notes": "Exact note",
            }
        ],
        "scope_mode": "user_only_default",
        "create_as_instance": True,
        "instance_of_type": "#V#record_type",
        "maintain_relationship_inverses": True,
    }
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        intent = command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments=base_arguments,
        )
        changed = command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments={
                **base_arguments,
                "concepts": [
                    {
                        **base_arguments["concepts"][0],
                        "notes": "A different exact note",
                    }
                ],
            },
        )

    assert intent.publication_context.kind == authority.PublicationContextKind.USER
    assert intent.publication_context.concept_id == "#V#member"
    assert intent.delta["stored_scope"] == {
        "effective_scope_mode": "user_only_default",
        "specific_to_user_concept_ids": ["#V#member"],
        "specific_to_organisation_concept_ids": [],
    }
    assert intent.delta["maintain_parent_inverse"] is False
    assert "#V#research_object" in intent.delta["referenced_concept_ids"]
    assert "#V#record_type" in intent.delta["referenced_concept_ids"]
    assert intent.fingerprint != changed.fingerprint

    with (
        authority.override_current_actor("#V#member", "#V#organisation_a"),
        pytest.raises(command.OntologyMutationCommandError) as exc_info,
    ):
        command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments={
                **base_arguments,
                "concepts": [
                    {
                        **base_arguments["concepts"][0],
                        "attributes": {"source": "registry"},
                    }
                ],
            },
        )

    assert exc_info.value.reason_code == "complex_create_requires_typed_effects"
    assert exc_info.value.error_details == {
        "rejected_fields": ["attributes"],
        "supported_core_concept_fields": [
            "concept_id",
            "name",
            "kind",
            "description",
            "notes",
            "vontology_path",
            "instance_of_type",
        ],
    }


def test_governed_create_shape_rejections_are_exact_and_have_no_fake_recovery(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        command,
        "issue_agent_delegation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("shape rejection must happen before delegation")
        ),
    )

    rejected_fields = sorted(
        {
            "allow_duplicate_instances",
            "attributes",
            "external_identifiers",
            "identity_candidate_concept_ids",
            "identity_rejected_candidate_concept_ids",
            "linked_concepts",
            "parent_concept_ids",
            "system_tags",
            "user_tags",
        }
    )
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        complex_result = command.issue_same_turn_method_delegation(
            method_name="create_concepts",
            arguments={
                "concepts": [
                    {
                        "name": "Profiled person",
                        "kind": "instance",
                        "attributes": {"source": "registry"},
                        "system_tags": ["system"],
                        "user_tags": ["user"],
                        "linked_concepts": [{"concept_id": "#V#linked"}],
                        "external_identifiers": [
                            {
                                "scheme": "profiles.example",
                                "canonical_value": "person-42",
                                "role": "identity",
                            }
                        ],
                        "identity_rejected_candidate_concept_ids": ["#V#rejected"],
                    }
                ],
                "identity_candidate_concept_ids": ["#V#candidate"],
                "parent_concept_ids": ["#V#person", "#V#researcher"],
                "allow_duplicate_instances": True,
            },
            actor_concept_id="#V#member",
            organisation_concept_id="#V#organisation_a",
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id="effect-complex-create",
            turn_id="turn-complex-create",
        )
        batch_result = command.issue_same_turn_method_delegation(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#person",
                "concepts": [
                    {"name": "Person one", "kind": "instance"},
                    {"name": "Person two", "kind": "instance"},
                ],
            },
            actor_concept_id="#V#member",
            organisation_concept_id="#V#organisation_a",
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id="effect-batch-create",
            turn_id="turn-batch-create",
        )

    assert complex_result["error_code"] == "complex_create_requires_typed_effects"
    assert complex_result["effect_status"] == "not_started"
    assert complex_result["changed"] is False
    assert complex_result["error_details"]["rejected_fields"] == rejected_fields
    assert all(field in complex_result["error"] for field in rejected_fields)
    assert "recovery_affordances" not in complex_result

    assert batch_result["error_code"] == "multi_create_requires_individual_effects"
    assert batch_result["effect_status"] == "not_started"
    assert batch_result["changed"] is False
    assert batch_result["error_details"] == {
        "constraint": "exactly_one_core_concept_per_effect",
        "received_concept_count": 2,
    }
    assert "Submit each concept as its own create_concepts effect" in (
        batch_result["error"]
    )
    assert "recovery_affordances" not in batch_result


def test_decorated_create_projects_rejected_fields_without_running_the_handler(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("rejected governed input must not reach the handler")
        ),
    )

    result = catalogue._create_concepts(
        parent_id="#V#person",
        concepts=[
            {
                "name": "Profiled person",
                "kind": "instance",
                "external_identifiers": [
                    {
                        "scheme": "profiles.example",
                        "canonical_value": "person-42",
                        "role": "identity",
                    }
                ],
            }
        ],
    )

    assert result["error_code"] == "complex_create_requires_typed_effects"
    assert result["effect_status"] == "not_started"
    assert result["changed"] is False
    assert result["error_details"]["rejected_fields"] == [
        "external_identifiers"
    ]
    assert "recovery_affordances" not in result


def test_dual_user_org_create_is_rejected_and_org_publication_is_explicit(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import (
        ontology_publication_authority_service as authority,
    )

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(authority, "resolve_live_semantic_roles", lambda _actor: ())
    with (
        authority.override_current_actor("#V#member", "#V#organisation_a"),
        pytest.raises(
            command.OntologyMutationCommandError,
            match="Dual user-and-organisation visibility",
        ),
    ):
        command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#research_object",
                "concepts": [{"name": "Ambiguous record", "kind": "type"}],
                "scope_mode": "user_org_default",
            },
        )

    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        intent = command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#research_object",
                "concepts": [{"name": "Organisation record", "kind": "type"}],
                "scope_mode": "organisation_general",
            },
        )
        decision = authority.authorise_ontology_mutation(intent)

    assert intent.publication_context.kind == (
        authority.PublicationContextKind.ORGANISATION
    )
    assert decision.allowed is False
    assert decision.reason_code == "organisation_ontology_admin_authority_required"


def test_duplicate_reuse_cannot_repair_existing_identity_markers(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.concept_external_identity_service import (
        ExternalIdentifier,
    )
    from src.backend.services.create_concepts_duplicate_guard_service import (
        CreateConceptDuplicateGuardMatch,
    )

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service."
        "resolve_event_actor_context",
        lambda **_kwargs: ("#V#member", "#V#organisation_a"),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "normalise_create_external_identifiers",
        lambda **_kwargs: (
            ExternalIdentifier(
                scheme="catalogue",
                value="record-42",
            ),
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "canonical_concept_id_for_external_identifiers",
        lambda *_args, **_kwargs: "#V#stable_record_42",
    )
    monkeypatch.setattr(
        "src.backend.services.create_concepts_duplicate_guard_service."
        "find_existing_concept_for_create_concepts",
        lambda **_kwargs: CreateConceptDuplicateGuardMatch(
            existing_concept_id="#V#existing_record",
            match_source="external_identifier:catalogue",
            guard_scope="instance",
            existing_kind="instance",
            requested_kind="instance",
            existing_parent_ids=("#V#record",),
            requested_parent_id="#V#record",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate reuse must not write identity markers")
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate reuse must not create another concept")
        ),
    )

    result = catalogue._create_concepts.__wrapped__(
        parent_id="#V#record",
        concepts=[
            {
                "name": "Existing record",
                "kind": "instance",
                "external_identifiers": [
                    {"scheme": "catalogue", "canonical_value": "record-42"}
                ],
            }
        ],
        scope_mode="user_only_default",
    )

    item = result["results"][0]
    assert item["existing_concept_id"] == "#V#existing_record"
    assert item["changed"] is False
    assert item["external_identity"]["persistence"] == {
        "success": True,
        "effect_status": "not_started",
        "changed": False,
        "reason_code": "duplicate_reuse_is_read_only",
    }


def test_http_create_binds_every_actual_field_and_disables_inverse_writes(
    monkeypatch,
) -> None:
    from src.backend.server.routes import concept_routes
    from src.backend.services import ontology_mutation_command_service as command

    captured: dict[str, Any] = {}
    mutation_kwargs: dict[str, Any] = {}

    monkeypatch.setattr(
        command,
        "resolve_governed_ontology_arguments",
        lambda _method, arguments: dict(arguments),
    )
    monkeypatch.setattr(
        concept_routes,
        "_get_current_user_concept_id",
        lambda: "#V#member",
    )
    monkeypatch.setattr(
        concept_routes,
        "_get_current_org_concept_id",
        lambda: "#V#organisation_a",
    )
    monkeypatch.setattr(concept_routes, "_get_request_namespace", lambda: "trusted")
    monkeypatch.setattr(
        concept_routes.concept_service,
        "create_concept",
        lambda **kwargs: (
            mutation_kwargs.update(kwargs) or {"concept_id": kwargs["concept_id"]}
        ),
    )

    def execute(**kwargs):
        captured.update(kwargs["arguments"])
        kwargs["mutate"]()
        return {"success": True, "created_concept_ids": ["#V#record"]}

    monkeypatch.setattr(concept_routes, "_execute_governed_http_mutation", execute)
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")

    response = app.test_client().post(
        "/api/concepts/",
        json={
            "name": "Record",
            "concept_id": "#V#record",
            "description": "description",
            "notes": "notes",
            "attributes": {"source": "catalogue"},
            "system_tags": ["system"],
            "user_tags": ["user"],
            "linked_concepts": [{"concept_id": "#V#linked"}],
            "parent_concept_ids": ["#V#record_type", "#V#research_object"],
            "create_as_instance": False,
            "instance_of_type": "#V#represented_entity",
            "scope_mode": "organisation_general",
            "request_id": "create-record-1",
        },
    )

    assert response.status_code == 201
    assert captured["parent_concept_ids"] == [
        "#V#record_type",
        "#V#research_object",
    ]
    assert captured["linked_concepts"] == [{"concept_id": "#V#linked"}]
    assert captured["attributes"] == {"source": "catalogue"}
    assert captured["concepts"][0]["instance_of_type"] == ("#V#represented_entity")
    assert mutation_kwargs["maintain_relationship_inverses"] is False
    assert mutation_kwargs["parent_concept_ids"] == captured["parent_concept_ids"]
    assert mutation_kwargs["linked_concepts"] == captured["linked_concepts"]


def test_failed_create_readback_never_follows_an_existing_concept_id(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    documents = {
        "#V#existing_record": {
            "concept_id": "#V#existing_record",
            "relationships": {
                "#V#specific_to_user": ["#V#member"],
                "is_an_instance_of": ["#V#record_type"],
            },
        },
        "#V#record_type": {
            "concept_id": "#V#record_type",
            "relationships": {},
        },
    }
    monkeypatch.setattr(
        command.ConceptsRepository,
        "find_one",
        lambda query: documents.get(query.get("concept_id")),
    )
    monkeypatch.setattr(
        authority.ConceptsRepository,
        "find_one",
        lambda query: documents.get(query.get("concept_id")),
    )

    readback = command.canonical_read_back_for_method(
        method_name="create_concepts",
        arguments={
            "parent_id": "#V#record_type",
            "concepts": [{"name": "Existing record", "kind": "instance"}],
        },
        result={
            "created_concept_ids": [],
            "results": [
                {
                    "error_code": "already_exists",
                    "existing_concept_id": "#V#existing_record",
                }
            ],
        },
    )

    assert readback["concepts"] == []


def test_create_readback_resolves_string_text_value_references(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    concept_id = "#V#represented_article"
    text_value_id = "507f1f77bcf86cd799439011"
    observed_text_value_ids: list[Any] = []
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        command.ConceptsRepository,
        "find_one",
        lambda _query: {
            "concept_id": concept_id,
            "relationships": {
                "#V#specific_to_user": ["#V#member"],
                "is_an_instance_of": ["#V#scholarly_article"],
            },
        },
    )
    monkeypatch.setattr(
        command.TextRelationsRepository,
        "find",
        lambda _query: [
            {
                "subject_concept_id": concept_id,
                "predicate": "hasName",
                "object_text_id": text_value_id,
                "context": {"name_type": "NL"},
            }
        ],
    )

    def find_text_value(value: Any) -> dict[str, Any]:
        observed_text_value_ids.append(value)
        return {"_id": text_value_id, "text": "Represented article", "lang": "en-NZ"}

    monkeypatch.setattr(
        command.TextValuesRepository,
        "find_one_by_id",
        find_text_value,
    )
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda _concept_id: authority.PublicationContext.user("#V#member"),
    )

    readback = command._concept_read_back(concept_id)

    assert observed_text_value_ids == [text_value_id]
    assert readback["text_relations"] == [
        {
            "predicate": "hasName",
            "text": "Represented article",
            "language": "en-NZ",
            "context": {"name_type": "NL"},
        }
    ]


def test_later_create_postcondition_reuses_the_original_immutable_intent(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    concept_id = "#V#nichola_raihani"
    arguments = {
        "instance_of_type": "#V#person",
        "concepts": [
            {
                "concept_id": concept_id,
                "name": "Nichola Raihani",
                "description": "A behavioural scientist.",
                "notes": "Represented from the conversation.",
                "kind": "instance",
            }
        ],
    }
    governed_arguments = command.resolve_governed_ontology_arguments(
        "create_concepts",
        command.normalise_governed_ontology_arguments(
            "create_concepts",
            arguments,
        ),
    )
    intent = authority.OntologyMutationIntent(
        operation="concept.create",
        publication_context=authority.PublicationContext.user("#V#person"),
        target_concept_ids=(concept_id, "#V#person"),
        tool_name="create_concepts",
        delta={
            "concepts": [
                {
                    "concept_id": concept_id,
                    "name": "Nichola Raihani",
                    "description": "A behavioural scientist.",
                    "notes": "Represented from the conversation.",
                    "kind": "instance",
                    "instance_of_type": "#V#person",
                    "vontology_path": None,
                }
            ],
            "expected_concept_id": concept_id,
            "parent_id": None,
            "parent_concept_ids": [],
            "instance_of_type": "#V#person",
            "referenced_concept_ids": ["#V#person"],
            "scope_mode": "user_only_default",
            "stored_scope": {},
            "maintain_parent_inverse": False,
            "effect_arguments_sha256": command._effect_arguments_sha256(
                governed_arguments
            ),
        },
    )
    canonical_state = {
        "concepts": [
            {
                "concept_id": concept_id,
                "exists": True,
                "publication_context": authority.PublicationContext.user(
                    "#V#person"
                ).to_mapping(),
                "forward_relationships": {
                    "is_a_type_of": [],
                    "is_an_instance_of": ["#V#person"],
                    "linked_to": [],
                },
                "attributes": {},
                "system_tags": [],
                "user_tags": [],
                "vontology_path": None,
                "text_relations": [
                    {
                        "predicate": "hasName",
                        "text": "Nichola Raihani",
                        "context": {"name_type": "NL"},
                    },
                    {
                        "predicate": "hasDescription",
                        "text": "A behavioural scientist.",
                        "context": {},
                    },
                    {
                        "predicate": "hasNote",
                        "text": "Represented from the conversation.",
                        "context": {},
                    },
                ],
            }
        ]
    }
    monkeypatch.setattr(
        command,
        "canonical_read_back_for_method",
        lambda **_kwargs: canonical_state,
    )
    monkeypatch.setattr(
        command,
        "reconcile_indeterminate_mutation_receipt",
        lambda **kwargs: {
            "receipt_id": kwargs["receipt_id"],
            "status": "succeeded",
        },
    )
    original_result = {
        "success": False,
        "effect_status": "indeterminate",
        "changed": None,
        "outcome_finality": "requires_canonical_reconciliation",
        "created_concept_ids": [concept_id],
        "authority_receipt": {
            "receipt_id": "omr-nichola",
            "status": "indeterminate",
            "intent_fingerprint": intent.fingerprint,
        },
        "postcondition_reconciliation": (
            command._postcondition_reconciliation_contract(
                method_name="create_concepts",
                intent=intent,
            )
        ),
    }

    reconciled = command.reconcile_governed_ontology_postcondition(
        method_name="create_concepts",
        arguments=arguments,
        original_result=original_result,
    )

    assert reconciled["verified"] is True
    assert reconciled["target_concept_ids"] == [concept_id, "#V#person"]
    assert reconciled["authority_receipt"]["status"] == "succeeded"

    changed_arguments = {**arguments, "instance_of_type": "#V#other_type"}
    rejected = command.reconcile_governed_ontology_postcondition(
        method_name="create_concepts",
        arguments=changed_arguments,
        original_result=original_result,
    )
    assert rejected["verified"] is False
    assert rejected["error_code"] == "ontology_reconciliation_contract_invalid"


def test_inaccessible_create_collision_is_non_disclosing(monkeypatch) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    secret = {
        "concept_id": "#V#secret",
        "relationships": {
            "#V#specific_to_user": ["#V#other_user"],
            "is_an_instance_of": ["#V#private_parent"],
        },
        "attributes": {"secret": "LEAK"},
        "system_tags": ["private-tag"],
    }
    monkeypatch.setattr(
        command.ConceptsRepository,
        "find_one",
        lambda query: secret if query.get("concept_id") == "#V#secret" else None,
    )
    mutate_calls = {"count": 0}

    def mutate() -> dict[str, Any]:
        mutate_calls["count"] += 1
        return {"success": True, "changed": True}

    with authority.override_current_actor("#V#attacker", None):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "concepts": [
                    {
                        "concept_id": "#V#secret",
                        "name": "Secret",
                        "kind": "instance",
                    }
                ],
                "scope_mode": "user_only_default",
            },
            mutate=mutate,
        )

    assert result["success"] is False
    assert result["effect_status"] == "not_started"
    assert result["mutation_outcome"] == "not_started"
    assert result["changed"] is False
    assert result["error_code"] == "ontology_create_concept_id_conflict"
    assert result["error"] == "The requested concept ID cannot be created."
    assert "LEAK" not in repr(result)
    assert "private_parent" not in repr(result)
    assert "recovery_affordances" not in result
    assert mutate_calls["count"] == 0


def test_hidden_instance_id_conflict_advertises_only_executable_referent_recovery(
    monkeypatch,
) -> None:
    from src.backend.services import actor_scoped_referent_identity_service as identity
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    requested_id = "#V#hidden_person"
    actor_id = "#V#member"
    parent_id = "#V#person"
    effective_id = identity.actor_scoped_referent_concept_id(
        requested_concept_id=requested_id,
        actor_concept_id=actor_id,
    )
    hidden_document = {
        "concept_id": requested_id,
        "relationships": {
            "#V#specific_to_user": ["#V#other_actor"],
            "is_an_instance_of": ["#V#hidden_parent_sentinel"],
        },
        "attributes": {"hidden_metadata": "HIDDEN_METADATA_SENTINEL"},
    }
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        command.ConceptsRepository,
        "find_one",
        lambda query: (
            hidden_document if query.get("concept_id") == requested_id else None
        ),
    )
    monkeypatch.setattr(
        command,
        "can_access_concept",
        lambda concept_id: concept_id == parent_id,
    )

    with authority.override_current_actor(actor_id, "#V#organisation_a"):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": parent_id,
                "concepts": [
                    {
                        "concept_id": requested_id,
                        "name": "Hidden Person",
                        "kind": "instance",
                        "description": "A requested core description.",
                    }
                ],
                "scope_mode": "user_only_default",
            },
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("a colliding create must not mutate")
            ),
        )

    assert result == {
        "success": False,
        "effect_status": "not_started",
        "mutation_outcome": "not_started",
        "changed": False,
        "error_code": "ontology_create_concept_id_conflict",
        "error": "The requested concept ID cannot be created.",
        "recovery_affordances": [
            {
                "action_type": ("create_actor_scoped_referent_after_id_conflict"),
                "tool": "create_concepts",
                "recovery_contract": ("ontology_actor_scoped_instance_referent.v1"),
                "arguments": {
                    "parent_id": parent_id,
                    "concepts": [
                        {
                            "concept_id": effective_id,
                            "name": "Hidden Person",
                            "kind": "instance",
                            "description": "A requested core description.",
                        }
                    ],
                    "duplicate_resolution_mode": "canonical_id_only",
                    "collision_resolution_mode": "actor_scoped_referent",
                    "requested_concept_id": requested_id,
                    "scope_mode": "user_only_default",
                },
                "semantic_effect": (
                    "Create or idempotently reuse an actor-private instance "
                    "referent. This does not create an alias, assert identity "
                    "or equivalence, publish knowledge, or access the concept "
                    "occupying the requested ID."
                ),
            }
        ],
    }
    assert "create_scoped_assertion" not in repr(result)
    assert "request_ontology_administrator_delegation" not in repr(result)
    assert actor_id not in repr(result["recovery_affordances"])
    assert "HIDDEN_METADATA_SENTINEL" not in repr(result)
    assert "hidden_parent_sentinel" not in repr(result)

    raw_recovery_arguments = {
        **result["recovery_affordances"][0]["arguments"],
        "created_by_concept_id": "#V#untrusted_actor",
        "organisation_concept_id": "#V#untrusted_organisation",
    }
    with authority.override_current_actor(actor_id, "#V#organisation_a"):
        resolved = command.resolve_governed_ontology_arguments(
            "create_concepts",
            command.normalise_governed_ontology_arguments(
                "create_concepts",
                raw_recovery_arguments,
            ),
        )

    assert resolved["concepts"][0]["concept_id"] == effective_id
    assert resolved["created_by_concept_id"] == actor_id
    assert resolved["organisation_concept_id"] == "#V#organisation_a"


def test_direct_actor_scoped_referent_mode_never_probes_requested_id(
    monkeypatch,
) -> None:
    from src.backend.services import actor_scoped_referent_identity_service as identity
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    requested_id = "#V#opaque_requested_id"
    actor_id = "#V#member"
    effective_id = identity.actor_scoped_referent_concept_id(
        requested_concept_id=requested_id,
        actor_concept_id=actor_id,
    )
    probed_ids: list[str] = []

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )

    def record_unfiltered_probe(concept_id: str) -> bool:
        probed_ids.append(concept_id)
        if concept_id == requested_id:
            raise AssertionError("recovery mode must not inspect the requested ID")
        return False

    monkeypatch.setattr(command, "_concept_exists_unfiltered", record_unfiltered_probe)

    def visible(concept_id: str) -> bool:
        if concept_id == requested_id:
            raise AssertionError("recovery mode must not inspect requested visibility")
        return concept_id == "#V#person"

    monkeypatch.setattr(command, "can_access_concept", visible)
    arguments = {
        "parent_id": "#V#person",
        "concepts": [
            {
                "concept_id": effective_id,
                "name": "Opaque requested person",
                "kind": "instance",
            }
        ],
        "collision_resolution_mode": "actor_scoped_referent",
        "requested_concept_id": requested_id,
        "duplicate_resolution_mode": "canonical_id_only",
        "scope_mode": "user_only_default",
    }

    with authority.override_current_actor(actor_id, None):
        resolved = command.resolve_governed_ontology_arguments(
            "create_concepts",
            arguments,
        )

    assert resolved["concepts"][0]["concept_id"] == effective_id
    assert probed_ids == []

    with (
        authority.override_current_actor(actor_id, None),
        pytest.raises(command.OntologyMutationCommandError) as exc_info,
    ):
        command.resolve_governed_ontology_arguments(
            "create_concepts",
            {
                **arguments,
                "concepts": [
                    {
                        "concept_id": requested_id,
                        "name": "Opaque requested person",
                        "kind": "instance",
                    }
                ],
            },
        )

    assert exc_info.value.reason_code == "actor_scoped_referent_identity_mismatch"
    assert probed_ids == []


def test_visible_incompatible_and_hidden_conflicts_share_generic_public_failure(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    requested_id = "#V#colliding_person"
    parent_id = "#V#person"
    requested_is_visible = {"value": False}
    arguments = {
        "parent_id": parent_id,
        "concepts": [
            {
                "concept_id": requested_id,
                "name": "Colliding Person",
                "kind": "instance",
            }
        ],
        "scope_mode": "user_only_default",
    }
    monkeypatch.setattr(
        command,
        "_concept_exists_unfiltered",
        lambda concept_id: concept_id == requested_id,
    )
    monkeypatch.setattr(
        command,
        "can_access_concept",
        lambda concept_id: concept_id == parent_id
        or (concept_id == requested_id and requested_is_visible["value"]),
    )
    monkeypatch.setattr(
        command,
        "_visible_concept_satisfies_requested_core",
        lambda **_kwargs: False,
    )

    with authority.override_current_actor("#V#member", None):
        hidden = command._safe_command_error(
            command._create_id_conflict_error(
                arguments=arguments,
                expected_concept_id=requested_id,
            )
        )
        requested_is_visible["value"] = True
        visible_incompatible = command._safe_command_error(
            command._create_id_conflict_error(
                arguments=arguments,
                expected_concept_id=requested_id,
            )
        )

    hidden_base = {
        key: value for key, value in hidden.items() if key != "recovery_affordances"
    }
    assert hidden_base == visible_incompatible
    assert hidden_base == {
        "success": False,
        "effect_status": "not_started",
        "mutation_outcome": "not_started",
        "changed": False,
        "error_code": "ontology_create_concept_id_conflict",
        "error": "The requested concept ID cannot be created.",
    }
    assert hidden["recovery_affordances"][0]["arguments"][
        "requested_concept_id"
    ] == requested_id
    assert "recovery_affordances" not in visible_incompatible


def test_actor_scoped_referent_first_create_and_repeat_are_explicitly_idempotent(
    monkeypatch,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from src.backend.services import actor_scoped_referent_identity_service as identity
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    requested_id = "#V#hidden_person"
    actor_id = "#V#member"
    parent_id = "#V#person"
    effective_id = identity.actor_scoped_referent_concept_id(
        requested_concept_id=requested_id,
        actor_concept_id=actor_id,
    )
    state = {"created": False}
    mutation_calls = {"count": 0}
    canonical_concept = {
        "concept_id": effective_id,
        "exists": True,
        "publication_context": authority.PublicationContext.user(actor_id).to_mapping(),
        "forward_relationships": {
            "is_a_type_of": [],
            "is_an_instance_of": [parent_id],
            "linked_to": [],
        },
        "attributes": {},
        "system_tags": [],
        "user_tags": [],
        "vontology_path": None,
        "text_relations": [
            {
                "predicate": "hasName",
                "text": "Hidden Person",
                "context": {"name_type": "NL"},
            }
        ],
    }

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        command,
        "_concept_exists_unfiltered",
        lambda concept_id: (
            concept_id == requested_id
            or (concept_id == effective_id and state["created"])
        ),
    )
    monkeypatch.setattr(
        command,
        "can_access_concept",
        lambda concept_id: (
            concept_id == parent_id or (concept_id == effective_id and state["created"])
        ),
    )
    monkeypatch.setattr(
        command,
        "_concept_read_back",
        lambda concept_id: (
            dict(canonical_concept)
            if concept_id == effective_id and state["created"]
            else {"concept_id": concept_id, "exists": False}
        ),
    )

    def scope_read_back(concept_id: str) -> dict[str, Any]:
        if concept_id == effective_id and not state["created"]:
            raise LookupError(concept_id)
        return {"scope_fingerprint": f"scope:{concept_id}"}

    monkeypatch.setattr(command, "scope_read_back", scope_read_back)
    monkeypatch.setattr(
        command,
        "ontology_mutation_resource_lock",
        lambda _resource_key: nullcontext(),
    )
    monkeypatch.setattr(command, "ontology_authority_resource_keys", lambda _intent: ())
    monkeypatch.setattr(
        command,
        "authorise_ontology_mutation",
        lambda _intent: SimpleNamespace(allowed=True),
    )

    def execute_authorised(**kwargs: Any) -> dict[str, Any]:
        kwargs["read_before"]()
        result = dict(kwargs["mutate"]())
        canonical_state = kwargs["read_back"]()
        assert kwargs["verify_read_back"](result, canonical_state) is True
        return {**result, "canonical_read_back": canonical_state}

    monkeypatch.setattr(
        command,
        "execute_authorised_ontology_mutation",
        execute_authorised,
    )
    recovery_arguments = {
        "parent_id": parent_id,
        "concepts": [
            {
                "concept_id": effective_id,
                "name": "Hidden Person",
                "kind": "instance",
            }
        ],
        "duplicate_resolution_mode": "canonical_id_only",
        "collision_resolution_mode": "actor_scoped_referent",
        "requested_concept_id": requested_id,
        "scope_mode": "user_only_default",
    }

    def create_once() -> dict[str, Any]:
        mutation_calls["count"] += 1
        state["created"] = True
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": [effective_id],
            "results": [
                {
                    "success": True,
                    "changed": True,
                    "concept_id": effective_id,
                }
            ],
            "total": 1,
            "successful": 1,
        }

    with authority.override_current_actor(actor_id, "#V#organisation_a"):
        first = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments=recovery_arguments,
            mutate=create_once,
        )
        repeated = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments=recovery_arguments,
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("compatible repeat must not mutate")
            ),
        )

    expected_metadata = {
        "schema_version": "ontology_actor_scoped_instance_referent.v1",
        "effective_concept_id": effective_id,
        "scope_mode": "user_only_default",
        "identity_status": "unreconciled_actor_scoped_referent",
        "equivalence_asserted": False,
        "alias_created": False,
    }
    assert mutation_calls["count"] == 1
    assert first["changed"] is True
    assert first["created_concept_ids"] == [effective_id]
    assert first["actor_scoped_referent"] == expected_metadata
    assert repeated["success"] is True
    assert repeated["changed"] is False
    assert repeated["idempotent_reuse"] is True
    assert repeated["created_concept_ids"] == []
    assert repeated["resolved_concept_ids"] == [effective_id]
    assert repeated["actor_scoped_referent"] == expected_metadata


def test_visible_compatible_exact_create_is_reused_without_mutation(
    monkeypatch,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    concept_id = "#V#visible_person"
    actor_id = "#V#member"
    parent_id = "#V#person"
    canonical_concept = {
        "concept_id": concept_id,
        "exists": True,
        "publication_context": authority.PublicationContext.global_context().to_mapping(),
        "forward_relationships": {
            "is_a_type_of": [],
            "is_an_instance_of": [parent_id],
            "linked_to": ["#V#additional_visible_context"],
        },
        "attributes": {},
        "system_tags": [],
        "user_tags": [],
        "vontology_path": None,
        "text_relations": [
            {
                "predicate": "hasName",
                "text": "Visible Person",
                "context": {"name_type": "NL"},
            }
        ],
    }

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(command, "_concept_exists_unfiltered", lambda _concept_id: True)
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        command, "_concept_read_back", lambda _concept_id: canonical_concept
    )
    monkeypatch.setattr(
        command,
        "scope_read_back",
        lambda value: {"scope_fingerprint": f"scope:{value}"},
    )
    monkeypatch.setattr(
        command,
        "ontology_mutation_resource_lock",
        lambda _resource_key: nullcontext(),
    )
    monkeypatch.setattr(command, "ontology_authority_resource_keys", lambda _intent: ())
    monkeypatch.setattr(
        command,
        "authorise_ontology_mutation",
        lambda _intent: SimpleNamespace(allowed=True),
    )

    def execute_authorised(**kwargs: Any) -> dict[str, Any]:
        result = dict(kwargs["mutate"]())
        canonical_state = kwargs["read_back"]()
        assert kwargs["verify_read_back"](result, canonical_state) is True
        return {**result, "canonical_read_back": canonical_state}

    monkeypatch.setattr(
        command,
        "execute_authorised_ontology_mutation",
        execute_authorised,
    )
    with authority.override_current_actor(actor_id, None):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": parent_id,
                "concepts": [
                    {
                        "concept_id": concept_id,
                        "name": "Visible Person",
                        "kind": "instance",
                    }
                ],
                "scope_mode": "user_only_default",
            },
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("visible compatible reuse must not mutate")
            ),
        )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["idempotent_reuse"] is True
    assert result["created_concept_ids"] == []
    assert result["resolved_concept_ids"] == [concept_id]
    assert "actor_scoped_referent" not in result


def test_actor_scoped_referent_created_concurrently_is_reused_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from src.backend.services import actor_scoped_referent_identity_service as identity
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    actor_id = "#V#member"
    requested_id = "#V#concurrent_person"
    parent_id = "#V#person"
    effective_id = identity.actor_scoped_referent_concept_id(
        requested_concept_id=requested_id,
        actor_concept_id=actor_id,
    )
    existence_checks = 0
    canonical_concept = {
        "concept_id": effective_id,
        "exists": True,
        "publication_context": authority.PublicationContext.user(actor_id).to_mapping(),
        "forward_relationships": {
            "is_a_type_of": [],
            "is_an_instance_of": [parent_id],
            "linked_to": [],
        },
        "attributes": {},
        "system_tags": [],
        "user_tags": [],
        "vontology_path": None,
        "text_relations": [
            {
                "predicate": "hasName",
                "text": "Concurrent Person",
                "context": {"name_type": "NL"},
            }
        ],
    }

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )

    def concept_exists(concept_id: str) -> bool:
        nonlocal existence_checks
        assert concept_id == effective_id
        existence_checks += 1
        return existence_checks > 1

    monkeypatch.setattr(command, "_concept_exists_unfiltered", concept_exists)
    monkeypatch.setattr(
        command,
        "can_access_concept",
        lambda concept_id: concept_id in {parent_id, effective_id},
    )
    monkeypatch.setattr(
        command,
        "_concept_read_back",
        lambda concept_id: (
            dict(canonical_concept)
            if concept_id == effective_id
            else {"concept_id": concept_id, "exists": False}
        ),
    )
    monkeypatch.setattr(
        command,
        "scope_read_back",
        lambda value: {"scope_fingerprint": f"scope:{value}"},
    )
    monkeypatch.setattr(
        command,
        "ontology_mutation_resource_lock",
        lambda _resource_key: nullcontext(),
    )
    monkeypatch.setattr(command, "ontology_authority_resource_keys", lambda _intent: ())
    monkeypatch.setattr(
        command,
        "authorise_ontology_mutation",
        lambda _intent: SimpleNamespace(allowed=True),
    )

    def execute_authorised(**kwargs: Any) -> dict[str, Any]:
        result = dict(kwargs["mutate"]())
        canonical_state = kwargs["read_back"]()
        assert kwargs["verify_read_back"](result, canonical_state) is True
        return {**result, "canonical_read_back": canonical_state}

    monkeypatch.setattr(
        command,
        "execute_authorised_ontology_mutation",
        execute_authorised,
    )
    with authority.override_current_actor(actor_id, None):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": parent_id,
                "concepts": [
                    {
                        "concept_id": effective_id,
                        "name": "Concurrent Person",
                        "kind": "instance",
                    }
                ],
                "duplicate_resolution_mode": "canonical_id_only",
                "collision_resolution_mode": "actor_scoped_referent",
                "requested_concept_id": requested_id,
                "scope_mode": "user_only_default",
            },
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("a compatible concurrent create must not mutate")
            ),
        )

    assert existence_checks == 2
    assert result["success"] is True
    assert result["changed"] is False
    assert result["idempotent_reuse"] is True
    assert result["resolved_concept_ids"] == [effective_id]
    assert result["actor_scoped_referent"]["effective_concept_id"] == effective_id
