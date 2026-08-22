from __future__ import annotations

from contextlib import nullcontext
from typing import Any


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


def test_exact_create_reuses_visible_descendant_without_applying_annotations(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    existing_id = "#V#directory_person_alpha"
    actor_id = "#V#member"
    parent_id = "#V#person"
    existing = {
        "concept_id": existing_id,
        "exists": True,
        "publication_context": authority.PublicationContext.global_context().to_mapping(),
        "forward_relationships": {
            "is_a_type_of": [],
            "is_an_instance_of": ["#V#student"],
            "linked_to": [],
        },
        "attributes": {},
        "system_tags": [],
        "user_tags": [],
        "vontology_path": "/existing/path",
        "text_relations": [
            {
                "predicate": "hasName",
                "text": "Alex Example",
                "context": {"name_type": "NL"},
            },
            {
                "predicate": "hasDescription",
                "text": "Existing grounded description.",
                "context": {},
            },
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
        lambda concept_id: concept_id == existing_id,
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(command, "_concept_read_back", lambda _concept_id: existing)
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology."
        "get_vontology_node_and_descendant_ids",
        lambda concept_id, **_kwargs: [concept_id, "#V#student"],
    )
    monkeypatch.setattr(
        command,
        "ontology_mutation_resource_lock",
        lambda _resource_key: nullcontext(),
    )

    with authority.override_current_actor(actor_id, None):
        result = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": parent_id,
                "concepts": [
                    {
                        "concept_id": existing_id,
                        "name": "Alex Example",
                        "kind": "instance",
                        "description": "New ungrounded description.",
                        "notes": "Requested note.",
                        "vontology_path": "/requested/path",
                    }
                ],
                "scope_mode": "user_only_default",
            },
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("exact reuse must not invoke create")
            ),
        )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["idempotent_reuse"] is True
    assert result["requested_concept_id"] == existing_id
    assert result["effective_concept_id"] == existing_id
    assert result["resolved_concept_ids"] == [existing_id]
    assert result["reuse_match_source"] == "exact_concept_id"
    assert result["publication_context"]["kind"] == "global"
    assert result["requested_scope_applied"] is False
    assert result["requested_scope_satisfied"] is False
    assert set(result["unapplied_fields"]) == {
        "description",
        "notes",
        "scope_mode",
        "vontology_path",
    }
    assert result["canonical_read_back"]["concepts"][0]["concept_id"] == existing_id


def test_exact_reuse_preserves_parent_and_instance_typing_axes(monkeypatch) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    monkeypatch.setattr(
        command,
        "_concept_read_back",
        lambda _concept_id: {
            "concept_id": "#V#directory_person_alpha",
            "exists": True,
            "publication_context": {"kind": "global", "concept_id": None},
            "forward_relationships": {
                "is_a_type_of": ["#V#researcher"],
                "is_an_instance_of": ["#V#lecturer"],
            },
            "text_relations": [
                {
                    "predicate": "hasName",
                    "text": "Alex Example",
                    "context": {"name_type": "NL"},
                }
            ],
        },
    )

    descendant_sets = {
        "#V#person": ["#V#person", "#V#researcher"],
        "#V#academic_staff": ["#V#academic_staff", "#V#lecturer"],
    }
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology."
        "get_vontology_node_and_descendant_ids",
        lambda concept_id, **_kwargs: descendant_sets[concept_id],
    )

    assert command._visible_concept_satisfies_requested_core(
        concept_id="#V#directory_person_alpha",
        concept_spec={"name": "Alex Example", "kind": "instance"},
        parent_id="#V#person",
        instance_of_type="#V#academic_staff",
    )


def test_missing_exact_id_is_not_reused_from_a_namestring_candidate(
    monkeypatch,
) -> None:
    from src.backend.services import create_concepts_duplicate_guard_service as guard
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        guard,
        "find_existing_concept_for_create_concepts",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("command preflight must not equate namestring with identity")
        ),
    )
    monkeypatch.setattr(command, "_concept_exists_unfiltered", lambda _cid: False)
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)

    with authority.override_current_actor("#V#member", None):
        intent = command.build_ontology_mutation_intent(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#person",
                "concepts": [
                    {
                        "concept_id": "#V#requested_person",
                        "name": "Matching Person",
                        "kind": "instance",
                    }
                ],
                "scope_mode": "user_only_default",
            },
        )

    assert intent.delta["expected_concept_id"] == "#V#requested_person"
    assert intent.delta["requested_concept_id"] == "#V#requested_person"
    assert intent.delta["visible_exact_reuse"] is False
    assert "visible_semantic_reuse" not in intent.delta


def test_inaccessible_target_advertises_no_scoped_or_admin_recovery() -> None:
    from src.backend.services import ontology_mutation_command_service as command

    result = command._safe_command_error(
        command.OntologyMutationCommandError(
            "ontology_mutation_target_not_accessible",
            "The requested ontology target is not accessible in this context.",
        )
    )

    assert result["effect_status"] == "not_started"
    assert "recovery_affordances" not in result
    assert "create_scoped_assertion" not in repr(result)
    assert "request_ontology_administrator_delegation" not in repr(result)


def test_authority_denial_retains_only_proven_scoped_recovery_marker(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    intent = authority.OntologyMutationIntent(
        operation="relationship.add",
        publication_context=authority.PublicationContext.global_context(),
        target_concept_ids=("#V#subject", "#V#target"),
        tool_name="add_relationship",
        predicate="#V#related_to",
    )
    monkeypatch.setattr(command, "build_ontology_mutation_intent", lambda **_kw: intent)
    monkeypatch.setattr(command, "actor_can_access_intent_targets", lambda **_kw: True)
    monkeypatch.setattr(
        command,
        "issue_agent_delegation",
        lambda **_kw: (_ for _ in ()).throw(
            PermissionError("global_ontology_admin_authority_required")
        ),
    )
    monkeypatch.setattr(
        command,
        "_scoped_assertion_recovery_is_executable",
        lambda **_kw: True,
    )

    result = command.issue_same_turn_method_delegation(
        method_name="add_relationship",
        arguments={
            "source_id": "#V#subject",
            "predicate": "#V#related_to",
            "target": "#V#target",
        },
        actor_concept_id="#V#member",
        organisation_concept_id=None,
        delegate_concept_id="#V#von_system",
        audience="adaptive_turn",
        effect_id="effect-1",
        turn_id="turn-1",
    )

    assert result["recovery_affordances"] == [
        {"action_type": "create_scoped_assertion"}
    ]
    assert "request_ontology_administrator_delegation" not in repr(result)


def test_direct_authority_denial_adds_only_proven_scoped_recovery(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    intent = authority.OntologyMutationIntent(
        operation="relationship.add",
        publication_context=authority.PublicationContext.global_context(),
        target_concept_ids=("#V#subject", "#V#target"),
        tool_name="add_relationship",
        predicate="#V#related_to",
    )
    decision = authority.OntologyAuthorityDecision(
        allowed=False,
        decision_id="decision-1",
        reason_code="global_ontology_admin_authority_required",
        message="Global ontology publication authority is required.",
        actor_concept_id="#V#member",
        organisation_concept_id=None,
        intent_fingerprint=intent.fingerprint,
    )
    bare_denial = authority.ontology_authority_denial_payload(decision, intent)
    assert "recovery_affordances" not in bare_denial

    monkeypatch.setattr(command, "build_ontology_mutation_intent", lambda **_kw: intent)
    monkeypatch.setattr(
        command,
        "execute_authorised_ontology_mutation",
        lambda **_kw: dict(bare_denial),
    )
    monkeypatch.setattr(
        command,
        "_scoped_assertion_recovery_is_executable",
        lambda **_kw: True,
    )
    result = command.execute_governed_ontology_method(
        method_name="add_relationship",
        arguments={
            "source_id": "#V#subject",
            "predicate": "#V#related_to",
            "target": "#V#target",
        },
        mutate=lambda: (_ for _ in ()).throw(
            AssertionError("authority denial must not invoke mutation")
        ),
    )

    assert result["recovery_affordances"] == [
        {"action_type": "create_scoped_assertion"}
    ]
    assert "request_ontology_administrator_delegation" not in repr(result)


def test_governed_add_relationship_authorises_and_reads_only_the_source(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    source_id = "#V#actor_private_candidature"
    target_id = "#V#global_person"
    predicate = "#V#is_about"
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda concept_id: (
            authority.PublicationContext.user("#V#member")
            if concept_id == source_id
            else authority.PublicationContext.global_context()
        ),
    )
    monkeypatch.setattr(
        command,
        "get_structural_inverse_map",
        lambda: {"is_about": "is_subject_of"},
    )
    monkeypatch.setattr(command, "scope_read_back", lambda _concept_id: {})

    with authority.override_current_actor("#V#member", None):
        intent = command.build_ontology_mutation_intent(
            method_name="add_relationship",
            arguments={
                "source_id": source_id,
                "predicate": predicate,
                "target": target_id,
            },
        )

    assert intent.publication_context.kind.value == "user"
    assert intent.source_contexts == ()
    assert intent.target_concept_ids == (source_id, target_id)
    assert intent.delta["maintain_inverse"] is False
    assert (
        command.normalise_governed_ontology_arguments(
            "add_relationship",
            {"maintain_inverse": True},
        )["maintain_inverse"]
        is False
    )

    reads: list[str] = []

    def find_one(query: dict[str, Any], *_args: Any, **_kwargs: Any):
        concept_id = query.get("concept_id")
        reads.append(concept_id)
        if concept_id != source_id:
            raise AssertionError("source-only read-back must not inspect the target")
        return {"concept_id": source_id, "relationships": {"is_about": [target_id]}}

    monkeypatch.setattr(command.ConceptsRepository, "find_one", find_one)
    read_back = command.canonical_read_back_for_method(
        method_name="add_relationship",
        arguments={
            "source_id": source_id,
            "predicate": predicate,
            "target": target_id,
        },
    )

    assert reads == [source_id]
    assert read_back["relationship_present"] is True
    assert read_back["inverse_relationship_present"] is None
