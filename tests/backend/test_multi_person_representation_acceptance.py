"""End-to-end regression for bounded multi-person KR recovery.

The fixture deliberately uses invented people and identifiers.  It checks the
general contract that a hydrated resolver batch remains actionable, exact IDs
returned by lookup can be reused without taking ownership, a genuinely missing
identity is created once, and source-owned relationship assertions may refer to
unchanged global concepts.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any


class _InMemoryConceptRepository:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs

    def find_one(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        del projection
        return self.docs.get(query.get("concept_id"))

    def _ensure_relationship_array(self, concept_id: str, predicate: str) -> bool:
        relationships = self.docs[concept_id].setdefault("relationships", {})
        created = predicate not in relationships
        relationships.setdefault(predicate, [])
        return created

    def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> SimpleNamespace:
        del upsert
        concept_id = query["concept_id"]
        field, target_id = next(iter(update["$addToSet"].items()))
        predicate = field.removeprefix("relationships.")
        values = (
            self.docs[concept_id]
            .setdefault("relationships", {})
            .setdefault(
                predicate,
                [],
            )
        )
        modified = target_id not in values
        if modified:
            values.append(target_id)
        return SimpleNamespace(matched_count=1, modified_count=int(modified))

    def canonical_read(self, concept_id: str) -> dict[str, Any]:
        doc = self.docs.get(concept_id)
        if doc is None:
            return {"concept_id": concept_id, "exists": False}
        relationships = doc.get("relationships", {})
        name = doc.get("name")
        return {
            "concept_id": concept_id,
            "exists": True,
            "publication_context": doc["_publication_context"].to_mapping(),
            "publication_scope_edges": {},
            "forward_relationships": {
                key: list(relationships.get(key) or [])
                for key in ("is_a_type_of", "is_an_instance_of", "linked_to")
            },
            "attributes": {},
            "system_tags": [],
            "user_tags": [],
            "vontology_path": None,
            "text_relations": (
                [
                    {
                        "predicate": "hasName",
                        "text": name,
                        "language": "en-NZ",
                        "context": {"name_type": "NL"},
                    }
                ]
                if name
                else []
            ),
        }


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


def test_multi_person_resolution_create_reuse_link_and_repeat_are_complete(
    monkeypatch,
) -> None:
    from contextlib import nullcontext

    from src.backend.languagemodels.structured_tool_calling.types import ToolResult
    from src.backend.services import adaptive_turn_service as adaptive
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority
    from src.backend.services import relationship_write_service as relationships

    actor_id = "#V#test_researcher"
    person_type_id = "#V#person"
    existing_people = {
        "Alex Example": "#V#directory_person_alpha",
        "Jordan Example": "#V#directory_person_beta",
    }
    missing_name = "Taylor Example"
    missing_id = "#V#taylor_example"
    global_context = authority.PublicationContext.global_context()
    user_context = authority.PublicationContext.user(actor_id)

    selected_values = [
        None,
        *existing_people.values(),
        *[f"#V#resolved_person_{index}" for index in range(26)],
    ]
    assert len(selected_values) == 29
    hydrated_results = [
        ToolResult(
            call_id=f"hydrate-person-{index}",
            tool_name="turn_read_evidence",
            status="ok",
            output={
                "schema_version": "turn_evidence_slice.v1",
                "success": True,
                "status": "ok",
                "evidence_id": f"person-resolution-{index}",
                "selector": {"json_pointer": "/resolved_concept_id"},
                "content": value,
                "projected_payload": {
                    "status": "not_found" if value is None else "resolved",
                    "resolved_concept_id": value,
                },
                # Force aggregate compaction while leaving every selected value
                # comfortably within the shared batch budget.
                "provenance": {"source_excerpt": "x" * 900},
            },
        )
        for index, value in enumerate(selected_values)
    ]
    unbounded_payload = [
        {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "status": result.status,
            "output": result.output,
        }
        for result in hydrated_results
    ]
    assert len(json.dumps(unbounded_payload).encode("utf-8")) > 24_000

    bounded = adaptive._bound_tool_results_for_model(hydrated_results)

    assert bounded is not None
    assert [result.output["content"] for result in bounded] == selected_values
    assert [
        result.output["projected_payload"]["resolved_concept_id"] for result in bounded
    ] == selected_values
    resolved_existing_people = {
        name: bounded[index].output["content"]
        for index, name in enumerate(existing_people, start=1)
    }
    assert resolved_existing_people == existing_people

    docs: dict[str, dict[str, Any]] = {
        person_type_id: {
            "concept_id": person_type_id,
            "name": "Person",
            "relationships": {"is_a_type_of": ["#V#agent"]},
            "_publication_context": global_context,
        },
        **{
            concept_id: {
                "concept_id": concept_id,
                "name": name,
                "computed_kind": "instance",
                "relationships": {"is_an_instance_of": [person_type_id]},
                # No visibility-owner edge: these are globally referable.
                "_publication_context": global_context,
            }
            for name, concept_id in existing_people.items()
        },
    }
    candidature_sources = {
        target_id: f"#V#candidature_{index}"
        for index, target_id in enumerate(
            (missing_id, *existing_people.values()),
            start=1,
        )
    }
    docs.update(
        {
            source_id: {
                "concept_id": source_id,
                "relationships": {},
                "_publication_context": user_context,
            }
            for source_id in candidature_sources.values()
        }
    )
    repo = _InMemoryConceptRepository(docs)

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        _parent_resolution,
    )
    monkeypatch.setattr(
        command,
        "_concept_exists_unfiltered",
        lambda concept_id: concept_id in docs,
    )
    monkeypatch.setattr(
        command, "can_access_concept", lambda concept_id: concept_id in docs
    )
    monkeypatch.setattr(command, "_concept_read_back", repo.canonical_read)
    monkeypatch.setattr(
        command.ConceptsRepository,
        "find_one",
        repo.find_one,
    )

    def scope_read_back(concept_id: str) -> dict[str, Any]:
        if concept_id not in docs:
            raise LookupError(concept_id)
        context = docs[concept_id]["_publication_context"]
        return {
            "concept_id": concept_id,
            "scope_fingerprint": (
                f"{context.kind.value}:{context.concept_id or 'global'}:{concept_id}"
            ),
        }

    monkeypatch.setattr(command, "scope_read_back", scope_read_back)
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda concept_id: docs[concept_id]["_publication_context"],
    )
    monkeypatch.setattr(
        command,
        "ontology_mutation_resource_lock",
        lambda _resource_key: nullcontext(),
    )
    monkeypatch.setattr(command, "ontology_authority_resource_keys", lambda _intent: ())

    authorised_intents = []

    def authorise(intent):
        authorised_intents.append(intent)
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(command, "authorise_ontology_mutation", authorise)

    def execute_authorised(**kwargs: Any) -> dict[str, Any]:
        result = dict(kwargs["mutate"]())
        canonical_state = kwargs["read_back"]()
        assert kwargs["verify_read_back"](result, canonical_state) is True
        return {
            "effect_status": "succeeded",
            "mutation_outcome": "succeeded",
            **result,
            "canonical_read_back": canonical_state,
        }

    monkeypatch.setattr(
        command,
        "execute_authorised_ontology_mutation",
        execute_authorised,
    )

    create_calls = 0

    def create_missing() -> dict[str, Any]:
        nonlocal create_calls
        create_calls += 1
        docs[missing_id] = {
            "concept_id": missing_id,
            "name": missing_name,
            "computed_kind": "instance",
            "relationships": {"is_an_instance_of": [person_type_id]},
            "_publication_context": user_context,
        }
        return {
            "success": True,
            "changed": True,
            "created_concept_ids": [missing_id],
            "results": [{"success": True, "changed": True, "concept_id": missing_id}],
        }

    def create_arguments(*, requested_id: str, name: str) -> dict[str, Any]:
        return {
            "parent_id": person_type_id,
            "scope_mode": "user_only_default",
            "concepts": [
                {
                    "concept_id": requested_id,
                    "name": name,
                    "kind": "instance",
                }
            ],
        }

    with authority.override_current_actor(actor_id, None):
        created = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments=create_arguments(requested_id=missing_id, name=missing_name),
            mutate=create_missing,
        )
        reused = []
        for name, effective_id in resolved_existing_people.items():
            result = command.execute_governed_ontology_method(
                method_name="create_concepts",
                arguments=create_arguments(
                    requested_id=effective_id,
                    name=name,
                ),
                mutate=lambda: (_ for _ in ()).throw(
                    AssertionError("an exact visible person must be reused")
                ),
            )
            assert result["requested_concept_id"] == effective_id
            assert result["effective_concept_id"] == effective_id
            assert result["resolved_concept_ids"] == [effective_id]
            assert result["publication_context"]["kind"] == "global"
            assert result["requested_scope_applied"] is False
            reused.append(result)

        repeated_create = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments=create_arguments(requested_id=missing_id, name=missing_name),
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("an exact repeat must not create again")
            ),
        )
        repeated_reuse = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments=create_arguments(
                requested_id=resolved_existing_people["Alex Example"],
                name="Alex Example",
            ),
            mutate=lambda: (_ for _ in ()).throw(
                AssertionError("exact reuse must remain idempotent")
            ),
        )

    assert created["created_concept_ids"] == [missing_id]
    assert create_calls == 1
    assert all(result["changed"] is False for result in reused)
    assert repeated_create["idempotent_reuse"] is True
    assert repeated_reuse["effective_concept_id"] == existing_people["Alex Example"]

    structural_predicate = "has_candidate"
    inverse_predicate = "is_candidate_of"
    monkeypatch.setattr(command, "normalise_structural_predicate", lambda value: value)
    monkeypatch.setattr(
        command,
        "get_structural_inverse_map",
        lambda: {structural_predicate: inverse_predicate},
    )
    monkeypatch.setattr(
        relationships,
        "normalise_structural_predicate",
        lambda value: value,
    )
    monkeypatch.setattr(
        relationships,
        "get_relationship_kinds_set",
        lambda: {structural_predicate},
    )
    monkeypatch.setattr(
        relationships,
        "get_structural_inverse_map",
        lambda: {structural_predicate: inverse_predicate},
    )
    monkeypatch.setattr(
        relationships,
        "_sync_relationship_extent_index_for_sources",
        lambda _source_ids: None,
    )
    monkeypatch.setattr(
        relationships,
        "_invalidate_workflow_routing_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        relationships,
        "_invalidate_vontology_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        relationships,
        "_emit_relationship_mutation_event",
        lambda **_kwargs: None,
    )

    target_snapshots = {
        target_id: deepcopy(repo.find_one({"concept_id": target_id}))
        for target_id in candidature_sources
    }

    def add_candidature_link(source_id: str, target_id: str) -> dict[str, Any]:
        raw_result = relationships.add_relationship(
            source_id,
            structural_predicate,
            target_id,
            repo=repo,
            maintain_inverse=False,
        )
        return {
            **raw_result,
            "changed": bool(raw_result.get("forward_modified")),
        }

    link_results = []
    repeat_results = []
    with authority.override_current_actor(actor_id, None):
        for target_id, source_id in candidature_sources.items():
            arguments = {
                "source_id": source_id,
                "predicate": structural_predicate,
                "target": target_id,
            }
            link_results.append(
                command.execute_governed_ontology_method(
                    method_name="add_relationship",
                    arguments=arguments,
                    mutate=lambda source_id=source_id, target_id=target_id: (
                        add_candidature_link(source_id, target_id)
                    ),
                )
            )
            repeat_results.append(
                command.execute_governed_ontology_method(
                    method_name="add_relationship",
                    arguments=arguments,
                    mutate=lambda source_id=source_id, target_id=target_id: (
                        add_candidature_link(source_id, target_id)
                    ),
                )
            )

    assert all(result["success"] is True for result in link_results)
    assert all(result["changed"] is True for result in link_results)
    assert all(
        result["canonical_read_back"]["relationship_present"] is True
        for result in link_results
    )
    assert all(
        result["canonical_read_back"]["inverse_relationship_present"] is None
        for result in link_results
    )
    assert all(result["changed"] is False for result in repeat_results)
    assert all(
        docs[source_id]["relationships"][structural_predicate] == [target_id]
        for target_id, source_id in candidature_sources.items()
    )
    assert all(
        repo.find_one({"concept_id": target_id}) == target_snapshots[target_id]
        for target_id in candidature_sources
    )

    relationship_intents = [
        intent
        for intent in authorised_intents
        if intent.tool_name == "add_relationship"
    ]
    assert len(relationship_intents) == 6
    assert all(
        intent.delta["maintain_inverse"] is False for intent in relationship_intents
    )
    assert all(
        intent.publication_context.kind.value == "user"
        for intent in relationship_intents
    )
    assert all(intent.source_contexts == () for intent in relationship_intents)
