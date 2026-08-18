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


def test_ordinary_create_is_explicitly_actor_private() -> None:
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue

    definition = build_default_catalogue().get("create_concepts")

    assert definition.ordinary_turn_fixed_arguments["scope_mode"] == (
        "user_only_default"
    )


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
    assert resolved["scope_mode"] == "user_only_default"
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
        pytest.raises(
            command.OntologyMutationCommandError,
            match="ancillary or multi-parent effects",
        ),
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
    from src.backend.services.create_concepts_duplicate_guard_service import (
        CreateConceptDuplicateGuardMatch,
    )
    from src.backend.services.concept_external_identity_service import (
        ExternalIdentifier,
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
    assert mutate_calls["count"] == 0
