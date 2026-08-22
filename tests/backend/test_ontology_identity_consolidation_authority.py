from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import mongomock
import pytest


@pytest.fixture
def consolidation_runtime(monkeypatch: pytest.MonkeyPatch):
    from src.backend.db.repositories import concepts_repository, text_value_repository
    from src.backend.services import concept_merge_service as merge
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    database = mongomock.MongoClient()["identity_consolidation_authority"]
    concepts = database["concepts"]
    text_relations = database["text_relations"]
    text_values = database["text_values"]
    delegations = database["delegations"]
    receipts = database["receipts"]
    delegations.create_index("delegation_id", unique=True)
    receipts.create_index("receipt_id", unique=True)
    receipts.create_index(
        [("actor_concept_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    monkeypatch.setattr(
        concepts_repository,
        "get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        text_value_repository,
        "get_text_relations_collection",
        lambda: text_relations,
    )
    monkeypatch.setattr(
        text_value_repository,
        "get_text_values_collection",
        lambda: text_values,
    )
    monkeypatch.setattr(
        authority,
        "get_ontology_authority_delegations_collection",
        lambda: delegations,
    )
    monkeypatch.setattr(
        authority,
        "get_ontology_mutation_receipts_collection",
        lambda: receipts,
    )
    monkeypatch.setattr(authority, "_gateway_actor_trust_source", lambda: None)
    monkeypatch.setattr(authority, "resolve_live_semantic_roles", lambda _actor: ())
    monkeypatch.setattr(
        "src.backend.services.concept_service._invalidate_concept_mutation_caches",
        lambda: None,
    )
    # Access filtering itself is exercised by the authority service tests. Keep
    # this fixture focused on exact plan/receipt execution.
    monkeypatch.setattr(merge, "can_access_concept", lambda _item: True)
    monkeypatch.setattr(command, "can_access_concept", lambda _item: True)
    return authority, merge, concepts, text_relations, text_values, receipts


def _concept(
    concept_id: str,
    *,
    relationships: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    return {
        "concept_id": concept_id,
        "guid": f"guid:{concept_id}",
        "created_at": now,
        "updated_at": now,
        "embedding_status": "ready",
        "attributes": {},
        "system_tags": [],
        "user_tags": [],
        "relationships": relationships or {},
    }


def _insert_code_alias(
    text_relations,
    text_values,
    *,
    relation_id: str,
    subject_id: str,
    text: str,
) -> None:
    text_id = f"text:{relation_id}"
    text_values.insert_one({"_id": text_id, "text": text, "lang": "en-NZ"})
    text_relations.insert_one(
        {
            "_id": relation_id,
            "subject_concept_id": subject_id,
            "predicate": "hasName",
            "object_text_id": text_id,
            "context": {"name_type": "CODE"},
        }
    )


def _seed_private_merge(concepts, text_relations, text_values) -> tuple[str, str]:
    source_id = "#V#duplicate"
    target_id = "#V#canonical"
    scope = {"#V#specific_to_user": ["#V#admin"]}
    concepts.insert_many(
        [
            _concept(source_id, relationships={**scope, "related_to": ["#V#topic"]}),
            _concept(target_id, relationships=scope),
            _concept(
                "#V#incoming",
                relationships={**scope, "related_to": [source_id]},
            ),
            _concept("#V#topic"),
        ]
    )
    _insert_code_alias(
        text_relations,
        text_values,
        relation_id="source-id",
        subject_id=source_id,
        text=source_id,
    )
    _insert_code_alias(
        text_relations,
        text_values,
        relation_id="source-guid",
        subject_id=source_id,
        text=f"guid:{source_id}",
    )
    return source_id, target_id


def test_governed_identity_consolidation_has_receipt_and_exact_read_back(
    consolidation_runtime,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    authority, merge, concepts, text_relations, text_values, receipts = (
        consolidation_runtime
    )
    source_id, target_id = _seed_private_merge(
        concepts,
        text_relations,
        text_values,
    )
    with authority.override_current_actor("#V#admin", None):
        arguments = command.resolve_governed_ontology_arguments(
            "merge_concepts",
            {
                "source_id": source_id,
                "target_id": target_id,
                "simulate": False,
                "request_id": "merge:one",
            },
        )
        result = command.execute_governed_ontology_method(
            method_name="merge_concepts",
            arguments=arguments,
            mutate=lambda: merge.merge_concepts(
                source_id,
                target_id,
                simulate=False,
                exact_plan=arguments["exact_plan"],
            ),
        )

    assert result["success"] is True
    assert result["authority_receipt"]["status"] == "succeeded"
    assert result["canonical_read_back"]["matches_exact_plan"] is True
    assert concepts.find_one({"concept_id": source_id}) is None
    assert receipts.count_documents({"record_kind": {"$ne": "resource_lock"}}) == 1


def test_graph_drift_after_resolution_fails_before_merge_write(
    consolidation_runtime,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    authority, merge, concepts, text_relations, text_values, _receipts = (
        consolidation_runtime
    )
    source_id, target_id = _seed_private_merge(
        concepts,
        text_relations,
        text_values,
    )
    with authority.override_current_actor("#V#admin", None):
        arguments = command.resolve_governed_ontology_arguments(
            "merge_concepts",
            {
                "source_id": source_id,
                "target_id": target_id,
                "simulate": False,
                "request_id": "merge:drift",
            },
        )
        concepts.insert_one(
            _concept(
                "#V#late_reference",
                relationships={
                    "#V#specific_to_user": ["#V#admin"],
                    "related_to": [source_id],
                },
            )
        )
        result = command.execute_governed_ontology_method(
            method_name="merge_concepts",
            arguments=arguments,
            mutate=lambda: merge.merge_concepts(
                source_id,
                target_id,
                simulate=False,
                exact_plan=arguments["exact_plan"],
            ),
        )

    assert result["success"] is False
    assert result["changed"] is False
    assert result["error_code"] == "identity_consolidation_precondition_failed"
    assert concepts.find_one({"concept_id": source_id}) is not None
    assert concepts.find_one({"concept_id": "#V#incoming"})["relationships"][
        "related_to"
    ] == [source_id]


def test_client_supplied_merge_plan_is_ignored(consolidation_runtime) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    _authority, _merge, concepts, text_relations, text_values, _receipts = (
        consolidation_runtime
    )
    source_id, target_id = _seed_private_merge(
        concepts,
        text_relations,
        text_values,
    )

    resolved = command.resolve_governed_ontology_arguments(
        "merge_concepts",
        {
            "source_id": source_id,
            "target_id": target_id,
            "simulate": False,
            "exact_plan": {
                "plan_sha256": "attacker",
                "affected_concept_ids": [source_id, target_id],
            },
        },
    )

    assert resolved["exact_plan"]["plan_sha256"] != "attacker"
    assert "#V#incoming" in resolved["exact_plan"]["affected_concept_ids"]


def test_merge_requires_authority_for_an_incoming_reference_context(
    consolidation_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    authority, _merge, concepts, text_relations, text_values, _receipts = (
        consolidation_runtime
    )
    source_id, target_id = _seed_private_merge(
        concepts,
        text_relations,
        text_values,
    )
    concepts.update_many(
        {"concept_id": {"$in": [source_id, target_id, "#V#incoming"]}},
        {"$unset": {"relationships.#V#specific_to_user": ""}},
    )
    concepts.update_many(
        {"concept_id": {"$in": [source_id, target_id]}},
        {"$set": {"relationships.#V#specific_to_organisation": ["#V#org_a"]}},
    )
    concepts.update_one(
        {"concept_id": "#V#incoming"},
        {"$set": {"relationships.#V#specific_to_organisation": ["#V#org_b"]}},
    )
    org_a_role = authority.AuthorityRoleEvidence(
        role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#admin",
        organisation_concept_id="#V#org_a",
        relation_id="role:org-a",
        revision="1",
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda _actor: (org_a_role,),
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _item: True)

    with authority.override_current_actor("#V#admin", "#V#org_a"):
        intent = command.build_ontology_mutation_intent(
            method_name="merge_concepts",
            arguments={
                "source_id": source_id,
                "target_id": target_id,
                "simulate": False,
            },
        )
        decision = authority.authorise_ontology_mutation(intent)

    assert set(intent.target_concept_ids) == {
        source_id,
        target_id,
        "#V#incoming",
    }
    assert decision.allowed is False
    assert decision.reason_code == "organisation_ontology_admin_authority_required"


def test_merge_keeps_distinct_composite_contexts_in_authority_plan(
    consolidation_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    authority, _merge, concepts, text_relations, text_values, _receipts = (
        consolidation_runtime
    )
    source_id, target_id = _seed_private_merge(
        concepts,
        text_relations,
        text_values,
    )
    concepts.update_many(
        {"concept_id": {"$in": [source_id, target_id]}},
        {
            "$set": {
                "relationships.#V#specific_to_organisation": ["#V#org_a"]
            }
        },
    )
    concepts.update_one(
        {"concept_id": "#V#incoming"},
        {
            "$set": {
                "relationships.#V#specific_to_organisation": ["#V#org_b"]
            }
        },
    )
    org_a_role = authority.AuthorityRoleEvidence(
        role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#admin",
        organisation_concept_id="#V#org_a",
        relation_id="role:org-a",
        revision="1",
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda _actor: (org_a_role,),
    )

    with authority.override_current_actor("#V#admin", "#V#org_a"):
        intent = command.build_ontology_mutation_intent(
            method_name="merge_concepts",
            arguments={
                "source_id": source_id,
                "target_id": target_id,
                "simulate": False,
            },
        )
        decision = authority.authorise_ontology_mutation(intent)

    assert intent.publication_context.kind == authority.PublicationContextKind.COMPOSITE
    assert any(
        context.kind == authority.PublicationContextKind.COMPOSITE
        and {
            component.concept_id for component in context.components
        } == {"#V#admin", "#V#org_b"}
        for context in intent.source_contexts
    )
    assert decision.allowed is False
    assert decision.reason_code == "organisation_ontology_admin_authority_required"


def test_update_read_back_must_preserve_exact_composite_components() -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    intended = authority.PublicationContext.composite(
        user_concept_id="#V#admin",
        organisation_concept_id="#V#org_a",
        source="intent",
    )
    intent = authority.OntologyMutationIntent(
        operation="concept.update",
        publication_context=intended,
        target_concept_ids=("#V#subject",),
        tool_name="update_concept",
        delta={},
    )
    wrong_context = authority.PublicationContext.composite(
        user_concept_id="#V#admin",
        organisation_concept_id="#V#org_b",
        source="concept_visibility",
    )
    exact_context = authority.PublicationContext.composite(
        user_concept_id="#V#admin",
        organisation_concept_id="#V#org_a",
        source="concept_visibility",
    )

    assert command._verify_method_postcondition(
        method_name="update_concept",
        intent=intent,
        result={"success": True},
        canonical_state={
            "exists": True,
            "publication_context": wrong_context.to_mapping(),
        },
    ) is False
    assert command._verify_method_postcondition(
        method_name="update_concept",
        intent=intent,
        result={"success": True},
        canonical_state={
            "exists": True,
            "publication_context": exact_context.to_mapping(),
        },
    ) is True


def test_partial_identity_consolidation_is_receipted_as_indeterminate(
    consolidation_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    authority, merge, concepts, text_relations, text_values, _receipts = (
        consolidation_runtime
    )
    source_id, target_id = _seed_private_merge(
        concepts,
        text_relations,
        text_values,
    )

    def fail_after_concept_steps(_step):
        raise merge._ConceptMergeApplyError(
            "forced_text_failure",
            "forced test failure",
            changed=False,
        )

    monkeypatch.setattr(merge, "_conditional_apply_text_step", fail_after_concept_steps)
    with authority.override_current_actor("#V#admin", None):
        arguments = command.resolve_governed_ontology_arguments(
            "merge_concepts",
            {
                "source_id": source_id,
                "target_id": target_id,
                "simulate": False,
                "request_id": "merge:partial",
            },
        )
        result = command.execute_governed_ontology_method(
            method_name="merge_concepts",
            arguments=arguments,
            mutate=lambda: merge.merge_concepts(
                source_id,
                target_id,
                simulate=False,
                exact_plan=arguments["exact_plan"],
            ),
        )

    assert result["success"] is False
    assert result["effect_status"] == "indeterminate"
    assert result["mutation_outcome"] == "partial"
    assert result["changed"] is None
    assert result["authority_receipt"]["status"] == "indeterminate"
    assert result["canonical_read_back"]["matches_exact_plan"] is False
    assert concepts.find_one({"concept_id": "#V#incoming"})["relationships"][
        "related_to"
    ] == [target_id]
