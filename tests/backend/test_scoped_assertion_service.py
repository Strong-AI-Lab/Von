from __future__ import annotations

import json
from datetime import datetime, timezone

import mongomock
import pytest


def _collection():
    return mongomock.MongoClient()["von_test"]["scoped_knowledge_assertions"]


def _stored_text_assertion(
    *,
    assertion_id: str,
    subject_concept_id: str,
    audience_key: str = "org:#V#trusted_org",
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "scoped_knowledge_assertion.v1",
        "assertion_id": assertion_id,
        "subject_concept_id": subject_concept_id,
        "predicate": "#V#has_description",
        "object_kind": "text",
        "object_text": {
            "text": f"Text for {assertion_id}",
            "language": "en-NZ",
        },
        "object_concept_id": None,
        "scope": {
            "mode": "organisation",
            "user_concept_id": "#V#author",
            "organisation_concept_id": "#V#trusted_org",
            "audience_keys": [audience_key],
        },
        "provenance": {
            "asserted_by_user_concept_id": "#V#author",
        },
        "canonical_publication": False,
        "status": "asserted",
        "created_at": now,
        "updated_at": now,
    }


def test_standalone_text_admission_preserves_exact_body_without_analysis(
    monkeypatch,
):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )

    def _analysis_must_not_run(*_args, **_kwargs):
        raise AssertionError("standalone text admission must not analyse concepts")

    monkeypatch.setattr(service, "can_access_concept", _analysis_must_not_run)
    monkeypatch.setattr(
        service,
        "resolve_text_relation_predicate_for_write",
        _analysis_must_not_run,
    )
    exact_text = "  Susan hosted salon-style gatherings in Svalbard.\n"
    receipt = service.store_text_assertion(
        text=exact_text,
        language="en-NZ",
        acting_user_concept_id="#V#member",
        turn_id="turn-raw-1",
    )

    assertion = receipt["canonical_read_back"]
    assert receipt["changed"] is True
    assert assertion["schema_version"] == "scoped_knowledge_assertion.v2"
    assert assertion["assertion_form"] == "standalone_text"
    assert assertion["subject_concept_id"] is None
    assert assertion["predicate"] is None
    assert assertion["concept_links"] == []
    assert assertion["object_text"]["text"] == exact_text
    assert assertion["assertion_context"] == {
        "context_id": "intake:user:#V#member",
        "selection": "implicit",
        "kind": "intake",
    }
    assert assertion["rag_index"]["status"] == "pending"
    assert assertion["rag_index"]["desired_operation"] == "upsert"
    assert receipt["derived_maintenance"]["durable"] is True


def test_standalone_occurrence_replay_and_distinct_turns(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    kwargs = {
        "text": "The same formulation.",
        "acting_user_concept_id": "#V#member",
    }
    first = service.store_text_assertion(**kwargs, turn_id="turn-1")
    replay = service.store_text_assertion(**kwargs, turn_id="turn-1")
    other_turn = service.store_text_assertion(**kwargs, turn_id="turn-2")
    source_first = service.store_text_assertion(
        **kwargs,
        turn_id="turn-3",
        source_event_id="source:item:7",
    )
    source_replay = service.store_text_assertion(
        **kwargs,
        turn_id="turn-4",
        source_event_id="source:item:7",
    )

    assert replay["changed"] is False
    assert replay["assertion_id"] == first["assertion_id"]
    assert other_turn["assertion_id"] != first["assertion_id"]
    assert source_replay["changed"] is False
    assert source_replay["assertion_id"] == source_first["assertion_id"]
    assert collection.count_documents({}) == 3


def test_standalone_source_replay_rejects_different_payload(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    service.store_text_assertion(
        text="Original body.",
        source_event_id="source:item:8",
        acting_user_concept_id="#V#member",
    )
    with pytest.raises(ValueError, match="different text assertion"):
        service.store_text_assertion(
            text="Changed body.",
            source_event_id="source:item:8",
            acting_user_concept_id="#V#member",
        )
    assert collection.count_documents({}) == 1


def test_standalone_text_is_visible_without_links_and_links_are_optional(
    monkeypatch,
):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    visible = {"#V#susan", "#V#svalbard"}
    monkeypatch.setattr(
        service,
        "can_access_concept",
        lambda concept_id: concept_id in visible,
    )
    def _filter_link_concepts(concept_ids):
        requested = set(concept_ids)
        if not requested:
            raise AssertionError(
                "linkless standalone retrieval must not require concept access"
            )
        return requested & visible

    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        _filter_link_concepts,
    )
    text = "Susan hosted salon-style gatherings in Svalbard."
    receipt = service.store_text_assertion(
        text=text,
        acting_user_concept_id="#V#member",
        turn_id="turn-links",
    )
    broad = service.list_visible_scoped_assertions(
        assertion_form="standalone_text",
        user_concept_id="#V#member",
    )
    assert [row["assertion_id"] for row in broad] == [receipt["assertion_id"]]

    linked = service.add_text_assertion_concept_links(
        assertion_id=receipt["assertion_id"],
        links=[
            {
                "concept_id": "#V#susan",
                "role": "about",
                "method": "explicit_user_link",
                "spans": [{"start": 0, "end": 5}],
            },
            {
                "concept_id": "#V#svalbard",
                "role": "involves",
                "confidence": 0.9,
                "spans": [{"start": 39, "end": 47}],
            },
        ],
        acting_user_concept_id="#V#member",
        turn_id="turn-links",
    )
    assert linked["changed"] is True
    assert linked["links_added"] == 2
    assert linked["assertion"]["object_text"]["text"] == text
    for concept_id in ("#V#susan", "#V#svalbard"):
        by_concept = service.list_visible_scoped_assertions(
            argument_concept_id=concept_id,
            assertion_form="standalone_text",
            user_concept_id="#V#member",
        )
        assert [row["assertion_id"] for row in by_concept] == [
            receipt["assertion_id"]
        ]

    visible.remove("#V#svalbard")
    redacted = service.list_visible_scoped_assertions(
        assertion_form="standalone_text",
        user_concept_id="#V#member",
    )
    assert [link["concept_id"] for link in redacted[0]["concept_links"]] == [
        "#V#susan"
    ]

    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda _concept_ids: (_ for _ in ()).throw(
            RuntimeError("concept visibility temporarily unavailable")
        ),
    )
    degraded = service.list_visible_scoped_assertions(
        assertion_form="standalone_text",
        user_concept_id="#V#member",
    )
    assert [row["assertion_id"] for row in degraded] == [receipt["assertion_id"]]
    assert degraded[0]["concept_links"] == []


def test_exact_assertion_read_keeps_retracted_lifecycle_and_hides_other_audience(
    monkeypatch,
) -> None:
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    row = _stored_text_assertion(
        assertion_id="ska_exact_retracted",
        subject_concept_id="#V#event",
    )
    row["status"] = "retracted"
    row["assertion_revision"] = 3
    collection.insert_one(row)
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )

    visible = service.get_visible_scoped_assertion_by_id(
        "ska_exact_retracted",
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    hidden = service.get_visible_scoped_assertion_by_id(
        "ska_exact_retracted",
        user_concept_id="#V#outsider",
        organisation_concept_id="#V#other_org",
    )

    assert visible is not None
    assert visible["status"] == "retracted"
    assert visible["assertion_revision"] == 3
    assert hidden is None


def test_exact_assertion_read_rejects_lookalike_without_store_access(
    monkeypatch,
) -> None:
    from src.backend.services import scoped_assertion_service as service

    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: (_ for _ in ()).throw(
            AssertionError("malformed IDs must not reach storage")
        ),
    )

    assert service.get_visible_scoped_assertion_by_id(
        "not_ska_123",
        user_concept_id="#V#member",
    ) is None


def test_standalone_retraction_is_canonical_and_marks_rag_delete(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    stored = service.store_text_assertion(
        text="A raw assertion.",
        acting_user_concept_id="#V#member",
        turn_id="turn-retract",
    )
    retracted = service.retract_scoped_assertion(
        assertion_id=stored["assertion_id"],
        acting_user_concept_id="#V#member",
    )
    repeated = service.retract_scoped_assertion(
        assertion_id=stored["assertion_id"],
        acting_user_concept_id="#V#member",
    )

    assert retracted["changed"] is True
    assert retracted["assertion"]["status"] == "retracted"
    assert retracted["assertion"]["assertion_revision"] == 2
    assert retracted["assertion"]["rag_index"]["status"] == "pending"
    assert retracted["assertion"]["rag_index"]["desired_operation"] == "delete"
    assert retracted["assertion"]["rag_index"]["desired_revision"] == 2
    assert retracted["derived_maintenance"]["durable"] is True
    assert repeated["changed"] is False
    assert service.list_visible_scoped_assertions(
        user_concept_id="#V#member"
    ) == []

    reasserted = service.store_text_assertion(
        text="A raw assertion.",
        acting_user_concept_id="#V#member",
        turn_id="turn-retract",
    )
    assert reasserted["changed"] is True
    assert reasserted["assertion_id"] == stored["assertion_id"]
    assert reasserted["assertion"]["status"] == "asserted"
    assert reasserted["assertion"]["assertion_revision"] == 3
    assert reasserted["assertion"]["rag_index"]["status"] == "pending"
    assert reasserted["assertion"]["rag_index"]["desired_operation"] == "upsert"


def test_visible_global_subject_accepts_user_scoped_text_assertion(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    first = service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Gillian leads our research programme.",
        scope_mode="organisation",
        evidence={"source": "user_instruction"},
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        turn_id="turn-1",
        canonical_publication=False,
    )
    second = service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Gillian leads our research programme.",
        scope_mode="organisation",
        evidence={"source": "user_instruction"},
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        turn_id="turn-1",
        canonical_publication=False,
    )

    assert first["success"] is True
    assert first["changed"] is True
    assert second["changed"] is False
    assert first["assertion_id"] == second["assertion_id"]
    assert collection.count_documents({}) == 1
    read_back = first["canonical_read_back"]
    assert read_back["subject_concept_id"] == "#V#gillian_dobbie"
    assert read_back["scope"]["mode"] == "organisation"
    assert read_back["canonical_publication"] is False
    assert read_back["provenance"]["turn_id"] == "turn-1"


def test_scoped_assertion_mutations_emit_actor_bound_content_free_events(monkeypatch):
    from src.backend.services import scoped_assertion_service as service
    from src.backend.services import workflow_event_integration_service as events

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    launches: list[dict] = []
    monkeypatch.setattr(
        events,
        "maybe_launch_vontology_mutation_workflow",
        lambda **kwargs: launches.append(kwargs) or {"success": True},
    )

    first = service.upsert_scoped_assertion(
        subject_concept_id="#V#bin",
        predicate="hasDescription",
        target_text="Private matching profile content.",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#strong_ai_lab",
        namespace="#V#michael_witbrock@strong_ai_lab",
    )
    replay = service.upsert_scoped_assertion(
        subject_concept_id="#V#bin",
        predicate="hasDescription",
        target_text="Private matching profile content.",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#strong_ai_lab",
        namespace="#V#michael_witbrock@strong_ai_lab",
    )
    service.retract_scoped_assertion(
        assertion_id=first["assertion_id"],
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#strong_ai_lab",
        namespace="#V#michael_witbrock@strong_ai_lab",
    )

    assert replay["changed"] is False
    assert [item["mutation_event_type"] for item in launches] == [
        events.EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
        events.EVENT_TYPE_SCOPED_ASSERTION_RETRACTED,
    ]
    assert all(item["user_id"] == "#V#michael_witbrock" for item in launches)
    assert all(item["org_id"] == "#V#strong_ai_lab" for item in launches)
    assert all("text" not in item["event_payload"] for item in launches)
    assert launches[0]["event_payload"]["subject_concept_id"] == "#V#bin"


def test_org_members_keep_distinct_immutable_assertion_provenance(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    first = service.upsert_scoped_assertion(
        subject_concept_id="#V#shared_subject",
        predicate="hasDescription",
        target_text="The same organisation-visible claim.",
        scope_mode="organisation",
        evidence={"source": "first-member"},
        acting_user_concept_id="#V#member_one",
        organisation_concept_id="#V#trusted_org",
        turn_id="member-one-turn",
    )
    second = service.upsert_scoped_assertion(
        subject_concept_id="#V#shared_subject",
        predicate="hasDescription",
        target_text="The same organisation-visible claim.",
        scope_mode="organisation",
        evidence={"source": "second-member"},
        acting_user_concept_id="#V#member_two",
        organisation_concept_id="#V#trusted_org",
        turn_id="member-two-turn",
    )

    assert first["assertion_id"] != second["assertion_id"]
    assert collection.count_documents({}) == 2
    assert (
        first["assertion"]["provenance"]["asserted_by_user_concept_id"]
        == "#V#member_one"
    )
    assert (
        second["assertion"]["provenance"]["asserted_by_user_concept_id"]
        == "#V#member_two"
    )

    repeated = service.upsert_scoped_assertion(
        subject_concept_id="#V#shared_subject",
        predicate="hasDescription",
        target_text="The same organisation-visible claim.",
        scope_mode="organisation",
        evidence={"source": "replacement-attempt"},
        acting_user_concept_id="#V#member_one",
        organisation_concept_id="#V#trusted_org",
        turn_id="later-turn",
    )
    assert repeated["changed"] is False
    assert repeated["assertion_id"] == first["assertion_id"]
    assert repeated["assertion"]["provenance"]["turn_id"] == "member-one-turn"
    assert repeated["assertion"]["provenance"]["evidence"] == {
        "source": "first-member"
    }


def test_scoped_assertions_do_not_cross_actor_or_organisation(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Organisation-private knowledge.",
        scope_mode="organisation",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#organisation_a",
        namespace="#V#michael_witbrock@organisation_a",
        canonical_publication=False,
    )

    same_org = service.list_visible_scoped_assertions(
        subject_concept_ids=["#V#gillian_dobbie"],
        user_concept_id="#V#someone_else",
        organisation_concept_id="#V#organisation_a",
    )
    other_org = service.list_visible_scoped_assertions(
        subject_concept_ids=["#V#gillian_dobbie"],
        user_concept_id="#V#someone_else",
        organisation_concept_id="#V#organisation_b",
    )

    assert len(same_org) == 1
    assert other_org == []


def test_unfiltered_reads_recheck_subject_and_concept_target_visibility(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    visible_concepts = {
        "#V#subject",
        "#V#target",
        "#V#hasDescription",
        "#V#related_to",
    }
    monkeypatch.setattr(
        service,
        "can_access_concept",
        lambda concept_id: concept_id in visible_concepts,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: {
            concept_id
            for concept_id in concept_ids
            if concept_id in visible_concepts
        },
    )
    monkeypatch.setattr(
        service,
        "validate_predicate_concept",
        lambda _predicate: (True, None, None),
    )
    service.upsert_scoped_assertion(
        subject_concept_id="#V#subject",
        predicate="hasDescription",
        target_text="A visible text claim.",
        scope_mode="organisation",
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    service.upsert_scoped_assertion(
        subject_concept_id="#V#subject",
        predicate="#V#related_to",
        target_concept_id="#V#target",
        scope_mode="organisation",
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    initially_visible = service.list_visible_scoped_assertions(
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    assert len(initially_visible) == 2

    visible_concepts.remove("#V#related_to")
    after_predicate_revocation = service.list_visible_scoped_assertions(
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    assert [item["object_kind"] for item in after_predicate_revocation] == [
        "text"
    ]
    visible_concepts.add("#V#related_to")

    visible_concepts.remove("#V#target")
    target_revocation_page = service.list_visible_scoped_assertions_page(
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    assert [item["object_kind"] for item in target_revocation_page["items"]] == [
        "text"
    ]
    assert target_revocation_page["visibility_filtered"] is False
    assert target_revocation_page["counts_are_lower_bounds"] is False

    visible_concepts.remove("#V#hasDescription")
    assert (
        service.list_visible_scoped_assertions(
            user_concept_id="#V#member",
            organisation_concept_id="#V#trusted_org",
        )
        == []
    )

    visible_concepts.remove("#V#subject")
    after_subject_revocation = service.list_visible_scoped_assertions(
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    assert after_subject_revocation == []


def test_explicit_read_actor_must_match_ambient_trusted_actor(monkeypatch):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service as service

    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        _collection,
    )
    with override_current_actor("#V#member", "#V#organisation_a"):
        with pytest.raises(PermissionError, match="organisation audience"):
            service.list_visible_scoped_assertions(
                user_concept_id="#V#member",
                organisation_concept_id="#V#organisation_b",
            )
        with pytest.raises(PermissionError, match="user audience"):
            service.list_visible_scoped_assertions(
                user_concept_id="#V#other_member",
                organisation_concept_id="#V#organisation_a",
            )
    # A user whose effective organisation has been revoked cannot revive the
    # old audience by supplying its identifier explicitly.
    with override_current_actor("#V#member", None):
        with pytest.raises(PermissionError, match="organisation audience"):
            service.list_visible_scoped_assertions(
                user_concept_id="#V#member",
                organisation_concept_id="#V#organisation_a",
            )


def test_explicit_read_actor_binds_current_visibility_checks(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    collection.insert_one(
        _stored_text_assertion(
            assertion_id="ska_one",
            subject_concept_id="#V#subject",
        )
    )
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    observed_actors = []

    def _filter_accessible(concept_ids):
        observed_actors.append(
            (
                service.get_effective_user_concept_id(),
                service.get_effective_organisation_concept_id(),
            )
        )
        return set(concept_ids)

    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        _filter_accessible,
    )
    assertions = service.list_visible_scoped_assertions(
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    assert len(assertions) == 1
    assert observed_actors
    assert set(observed_actors) == {
        ("#V#member", "#V#trusted_org"),
    }


def test_per_subject_page_is_one_bounded_non_starving_aggregate(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    subjects = ["#V#subject_a", "#V#subject_b"]
    documents = [
        _stored_text_assertion(
            assertion_id=f"ska_{subject[-1]}_{index}",
            subject_concept_id=subject,
        )
        for subject in subjects
        for index in range(3)
    ]

    class _AggregateCollection:
        name = "scoped_knowledge_assertions"

        def __init__(self):
            self.pipelines = []

        def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return iter(documents)

    collection = _AggregateCollection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )

    page = service.list_visible_scoped_assertions_page(
        subject_concept_ids=subjects,
        object_kind="text",
        limit_per_subject=2,
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    assert len(collection.pipelines) == 1
    pipeline = collection.pipelines[0]
    union_stages = [stage["$unionWith"] for stage in pipeline if "$unionWith" in stage]
    assert len(union_stages) == 1
    assert pipeline[2] == {"$limit": 5001}
    assert union_stages[0]["pipeline"][2] == {"$limit": 5001}
    assert all("$skip" not in stage for stage in pipeline)
    assert all(
        "$skip" not in stage
        for stage in union_stages[0]["pipeline"]
    )
    assert page["returned"] == 4
    assert page["has_more"] is True
    assert page["truncated"] is True
    assert page["counts_are_lower_bounds"] is True
    for subject in subjects:
        subject_page = page["per_subject"][subject]
        assert subject_page["returned"] == 2
        assert subject_page["has_more"] is True
        assert subject_page["next_offset"] == 2
        assert all(
            item["subject_concept_id"] == subject
            for item in subject_page["items"]
        )


def test_hidden_candidates_do_not_change_visible_paging_or_diagnostics(
    monkeypatch,
):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    newest_hidden = _stored_text_assertion(
        assertion_id="ska_hidden",
        subject_concept_id="#V#hidden",
    )
    first_visible = _stored_text_assertion(
        assertion_id="ska_visible_1",
        subject_concept_id="#V#visible_1",
    )
    second_visible = _stored_text_assertion(
        assertion_id="ska_visible_2",
        subject_concept_id="#V#visible_2",
    )
    newest_hidden["updated_at"] = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    first_visible["updated_at"] = datetime(
        2026, 7, 29, 11, tzinfo=timezone.utc
    )
    second_visible["updated_at"] = datetime(
        2026, 7, 29, 10, tzinfo=timezone.utc
    )
    collection.insert_many([newest_hidden, first_visible, second_visible])
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    visibility_batches = []

    def _filter_accessible(concept_ids):
        batch = set(concept_ids)
        if batch:
            visibility_batches.append(batch)
        return {
            concept_id
            for concept_id in batch
            if concept_id != "#V#hidden"
        }

    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        _filter_accessible,
    )

    first_page = service.list_visible_scoped_assertions_page(
        limit=1,
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    second_page = service.list_visible_scoped_assertions_page(
        limit=1,
        offset=1,
        user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    assert [item["assertion_id"] for item in first_page["items"]] == [
        "ska_visible_1"
    ]
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 1
    assert first_page["visibility_filtered"] is False
    assert first_page["counts_are_lower_bounds"] is True
    assert [item["assertion_id"] for item in second_page["items"]] == [
        "ska_visible_2"
    ]
    assert second_page["has_more"] is False
    assert second_page["next_offset"] is None
    assert second_page["visibility_filtered"] is False
    assert second_page["counts_are_lower_bounds"] is False
    assert len(visibility_batches) == 2
    assert all("#V#hidden" in batch for batch in visibility_batches)


def test_core_predicate_filters_match_storage_and_concept_id_forms(
    monkeypatch,
):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    stored = _stored_text_assertion(
        assertion_id="ska_core_predicate",
        subject_concept_id="#V#visible",
    )
    stored["predicate"] = "hasNote"
    collection.insert_one(stored)
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )

    for predicate in ("hasNote", "#V#hasNote"):
        page = service.list_visible_scoped_assertions_page(
            predicates=[predicate],
            user_concept_id="#V#member",
            organisation_concept_id="#V#trusted_org",
        )
        assert [row["assertion_id"] for row in page["items"]] == [
            "ska_core_predicate"
        ]


def test_service_rejects_namespace_that_disagrees_with_trusted_actor(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        _collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    with pytest.raises(ValueError, match="namespace does not match"):
        service.upsert_scoped_assertion(
            subject_concept_id="#V#gillian_dobbie",
            predicate="hasDescription",
            target_text="Attempted scope substitution.",
            acting_user_concept_id="#V#michael_witbrock",
            organisation_concept_id="#V#trusted_org",
            namespace="#V#attacker@other_org",
            canonical_publication=False,
        )


def test_write_actor_cannot_conflict_with_ambient_trusted_actor(monkeypatch):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )

    def _unexpected_access(_concept_id):
        raise AssertionError("actor conflict must fail before concept access")

    monkeypatch.setattr(service, "can_access_concept", _unexpected_access)
    with override_current_actor("#V#member", "#V#organisation_a"):
        with pytest.raises(PermissionError, match="acting user"):
            service.upsert_scoped_assertion(
                subject_concept_id="#V#subject",
                predicate="hasDescription",
                target_text="Conflicting user.",
                acting_user_concept_id="#V#other_member",
                organisation_concept_id="#V#organisation_a",
            )
        with pytest.raises(PermissionError, match="organisation"):
            service.upsert_scoped_assertion(
                subject_concept_id="#V#subject",
                predicate="hasDescription",
                target_text="Conflicting organisation.",
                acting_user_concept_id="#V#member",
                organisation_concept_id="#V#organisation_b",
            )
    assert collection.count_documents({}) == 0


def test_predicate_visibility_precedes_concept_predicate_validation(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    events = []

    def _can_access(concept_id):
        events.append(("access", concept_id))
        return concept_id != "#V#hidden_predicate"

    def _validate(predicate):
        events.append(("validate", predicate))
        return True, None, None

    monkeypatch.setattr(service, "can_access_concept", _can_access)
    monkeypatch.setattr(service, "validate_predicate_concept", _validate)

    with pytest.raises(PermissionError, match="predicate concept"):
        service.upsert_scoped_assertion(
            subject_concept_id="#V#subject",
            predicate="#V#hidden_predicate",
            target_concept_id="#V#target",
            acting_user_concept_id="#V#member",
        )

    assert ("access", "#V#hidden_predicate") in events
    assert not any(event[0] == "validate" for event in events)
    assert collection.count_documents({}) == 0


def test_user_scope_is_org_independent_and_replay_preserves_origin(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    first = service.upsert_scoped_assertion(
        subject_concept_id="#V#subject",
        predicate="hasDescription",
        target_text="User-enduring claim.",
        scope_mode="user",
        evidence={"source": "first-context"},
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#organisation_a",
        namespace="#V#member@organisation_a",
        turn_id="first-turn",
    )
    replay = service.upsert_scoped_assertion(
        subject_concept_id="#V#subject",
        predicate="hasDescription",
        target_text="User-enduring claim.",
        scope_mode="user",
        evidence={"source": "replacement-attempt"},
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#organisation_b",
        namespace="#V#member@organisation_b",
        turn_id="replay-turn",
    )

    assert first["changed"] is True
    assert replay["changed"] is False
    assert replay["assertion_id"] == first["assertion_id"]
    assert collection.count_documents({}) == 1
    assertion = replay["canonical_read_back"]
    assert assertion["scope"]["organisation_concept_id"] is None
    assert assertion["scope"]["namespace"] == service.resolve_canonical_namespace(
        None,
        "#V#member",
        None,
    )
    assert assertion["provenance"]["organisation_concept_id"] == "#V#organisation_a"
    assert assertion["provenance"]["namespace"] == "#V#member@organisation_a"
    assert assertion["provenance"]["turn_id"] == "first-turn"
    assert assertion["provenance"]["evidence"] == {
        "source": "first-context"
    }
    assert replay["canonical_read_back"] == first["canonical_read_back"]

    retracted = service.retract_scoped_assertion(
        assertion_id=first["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#organisation_b",
        namespace="#V#member@organisation_b",
    )
    assert retracted["changed"] is True
    assert retracted["assertion"]["status"] == "retracted"


def test_atomic_creation_reports_changed_once_under_concurrency(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    worker_count = 8
    barrier = Barrier(worker_count)

    def _write(index):
        barrier.wait()
        return service.upsert_scoped_assertion(
            subject_concept_id="#V#subject",
            predicate="hasDescription",
            target_text="One concurrently asserted claim.",
            scope_mode="organisation",
            evidence={"worker": index},
            acting_user_concept_id="#V#member",
            organisation_concept_id="#V#trusted_org",
            turn_id=f"turn-{index}",
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        receipts = list(executor.map(_write, range(worker_count)))

    assert sum(receipt["changed"] is True for receipt in receipts) == 1
    assert collection.count_documents({}) == 1
    assert collection.find_one({})["_id"] == receipts[0]["assertion_id"]
    canonical_ids = {
        receipt["canonical_read_back"]["assertion_id"] for receipt in receipts
    }
    assert canonical_ids == {receipts[0]["assertion_id"]}
    original_provenance = collection.find_one(
        {"assertion_id": receipts[0]["assertion_id"]}
    )["provenance"]

    service.retract_scoped_assertion(
        assertion_id=receipts[0]["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        reactivation_receipts = list(
            executor.map(_write, range(worker_count))
        )

    assert (
        sum(receipt["changed"] is True for receipt in reactivation_receipts)
        == 1
    )
    reactivated = collection.find_one(
        {"assertion_id": receipts[0]["assertion_id"]}
    )
    assert reactivated["status"] == "asserted"
    assert reactivated["provenance"] == original_provenance
    assert "retracted_at" not in reactivated
    assert len(reactivated["retraction_history"]) == 1
    assert reactivated["reassertion_provenance"][
        "reasserted_by_user_concept_id"
    ] == "#V#member"
    for receipt in reactivation_receipts:
        json.dumps(receipt["canonical_read_back"])


def test_original_author_can_idempotently_retract_after_visibility_loss(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    receipt = service.upsert_scoped_assertion(
        subject_concept_id="#V#subject",
        predicate="hasDescription",
        target_text="Retractable claim.",
        scope_mode="organisation",
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    def _visibility_must_not_be_consulted(_concept_id):
        raise AssertionError("retraction must survive concept visibility loss")

    monkeypatch.setattr(
        service,
        "can_access_concept",
        _visibility_must_not_be_consulted,
    )
    first = service.retract_scoped_assertion(
        assertion_id=receipt["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    repeated = service.retract_scoped_assertion(
        assertion_id=receipt["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    assert first["success"] is True
    assert first["effect_status"] == "succeeded"
    assert first["changed"] is True
    assert first["assertion"]["status"] == "retracted"
    assert first["assertion"]["retracted_at"]
    assert first["assertion"]["retraction_provenance"] == {
        "retracted_by_user_concept_id": "#V#member",
        "organisation_concept_id": "#V#trusted_org",
        "namespace": "#V#member@trusted_org",
        "capability_name": "retract_scoped_assertion",
    }
    assert repeated["changed"] is False
    assert repeated["canonical_read_back"] == first["canonical_read_back"]
    assert (
        service.list_visible_scoped_assertions(
            user_concept_id="#V#member",
            organisation_concept_id="#V#trusted_org",
        )
        == []
    )


def test_retraction_denial_does_not_reveal_existence(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    receipt = service.upsert_scoped_assertion(
        subject_concept_id="#V#subject",
        predicate="hasDescription",
        target_text="Owned claim.",
        scope_mode="organisation",
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )

    with pytest.raises(PermissionError) as wrong_author:
        service.retract_scoped_assertion(
            assertion_id=receipt["assertion_id"],
            acting_user_concept_id="#V#other_member",
            organisation_concept_id="#V#trusted_org",
        )
    with pytest.raises(PermissionError) as wrong_org:
        service.retract_scoped_assertion(
            assertion_id=receipt["assertion_id"],
            acting_user_concept_id="#V#member",
            organisation_concept_id="#V#other_org",
        )
    with pytest.raises(PermissionError) as missing:
        service.retract_scoped_assertion(
            assertion_id="ska_00000000000000000000000000000000",
            acting_user_concept_id="#V#member",
            organisation_concept_id="#V#trusted_org",
        )

    expected = "scoped assertion is unavailable to the current actor"
    assert str(wrong_author.value) == expected
    assert str(wrong_org.value) == expected
    assert str(missing.value) == expected
    assert collection.find_one(
        {"assertion_id": receipt["assertion_id"]}
    )["status"] == "asserted"


def test_concept_target_assertion_is_retrievable_from_either_argument(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        service,
        "validate_predicate_concept",
        lambda _predicate: (True, None, None),
    )
    receipt = service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="#V#co_directs",
        target_concept_id="#V#centre_for_machine_learning_for_social_good",
        scope_mode="user",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )

    from_subject = service.list_visible_scoped_assertions(
        argument_concept_id="#V#gillian_dobbie",
        object_kind="concept",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
    )
    from_target = service.list_visible_scoped_assertions(
        argument_concept_id="#V#centre_for_machine_learning_for_social_good",
        object_kind="concept",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
    )

    assert receipt["success"] is True
    assert [item["assertion_id"] for item in from_subject] == [receipt["assertion_id"]]
    assert [item["assertion_id"] for item in from_target] == [receipt["assertion_id"]]


def test_trusted_tool_payload_replaces_model_supplied_scope():
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )
    from src.backend.services.adaptive_turn_service import _trusted_tool_payload

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="upsert_scoped_assertion",
        model_payload={
            "subject_concept_id": "#V#gillian_dobbie",
            "predicate": "hasDescription",
            "target_text": "Scoped fact",
            "acting_user_concept_id": "#V#attacker",
            "organisation_concept_id": "#V#other_org",
            "namespace": "#V#attacker@other_org",
            "turn_id": "forged-turn",
            "canonical_publication": True,
        },
        trusted_argument_values={
            "actor_user_concept_id": "#V#michael_witbrock",
            "actor_organisation_concept_id": "#V#trusted_org",
            "turn_namespace": "#V#michael_witbrock@trusted_org",
            "turn_id": "trusted-turn",
        },
    )

    assert payload["acting_user_concept_id"] == "#V#michael_witbrock"
    assert payload["organisation_concept_id"] is None
    assert payload["namespace"] == "#V#michael_witbrock@trusted_org"
    assert payload["turn_id"] == "trusted-turn"
    assert payload["canonical_publication"] is False


def test_actor_effective_text_read_merges_visible_scoped_assertions(monkeypatch):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service as scoped_service
    from src.backend.services import text_value_service

    collection = _collection()
    monkeypatch.setattr(
        scoped_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_service,
        "can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        scoped_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    scoped_service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Scoped programme context.",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )

    with override_current_actor("#V#michael_witbrock", "#V#trusted_org"):
        rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
            context_view="actor_effective",
        )

    assert len(rows) == 1
    assert rows[0]["text"] == "Scoped programme context."
    assert rows[0]["assertion_id"].startswith("ska_")
    assert rows[0]["relation_id"] is None
    assert rows[0]["row_kind"] == "scoped_assertion"
    assert rows[0]["canonical_publication"] is False
    assert rows[0]["storage_surface"] == "scoped_knowledge_assertions"
