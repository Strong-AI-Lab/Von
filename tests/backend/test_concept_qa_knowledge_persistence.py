from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import mongomock
import pytest
from bson import ObjectId

from src.backend.security import role_resolver
from src.backend.services import concept_service, scoped_assertion_service


def test_update_concept_notes_resolves_session_object_id_to_relation_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_id = ObjectId()
    calls: dict[str, Any] = {}

    def fake_find_one(query: dict[str, Any], projection=None):
        calls["lookup"] = query
        return {"_id": object_id, "concept_id": "#V#primary_labs"}

    def fake_upsert_text_for_concept(**kwargs: Any) -> dict[str, Any]:
        calls["upsert"] = kwargs
        return {"text_value_id": "tv-1", "relation_id": "rel-1"}

    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "find_one",
        fake_find_one,
    )
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "update_one",
        lambda *_args, **_kwargs: SimpleNamespace(modified_count=1),
    )
    monkeypatch.setattr(
        concept_service.TextRelationsRepository,
        "delete_many",
        lambda *_args, **_kwargs: SimpleNamespace(deleted_count=0),
    )
    monkeypatch.setattr(
        concept_service,
        "upsert_text_for_concept",
        fake_upsert_text_for_concept,
    )

    assert concept_service.update_concept_notes(
        str(object_id),
        "Primary Labs' main product is the news site theprimary.com.",
        provenance={"source": "concept_q_and_a_synthesis"},
        context={"source_assertion_id": "ska-answer-1"},
    )

    assert {"_id": object_id} in calls["lookup"]["$or"]
    assert calls["upsert"]["subject_concept_id"] == "#V#primary_labs"
    assert calls["upsert"]["predicate"] == "hasNote"
    assert calls["upsert"]["provenance"]["source"] == ("concept_q_and_a_synthesis")
    assert calls["upsert"]["context"]["source_assertion_id"] == "ska-answer-1"


def test_synthesis_preserves_generated_statement_when_notes_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def generate(self, **_kwargs: Any) -> str:
            return "Primary Labs' main product is the news site theprimary.com."

    monkeypatch.setattr(concept_service, "get_concept_notes", lambda _concept: "")
    monkeypatch.setattr(
        concept_service, "update_concept_notes", lambda *_a, **_k: False
    )

    result = concept_service.synthesize_and_update_concept_notes(
        concept_id=str(ObjectId()),
        concept={"concept_id": "#V#primary_labs", "name": "Primary Labs"},
        question_or_statement="What is Primary Labs' main product or project?",
        user_answer="The news site theprimary.com",
        concept_type_name="#V#primary_labs",
        ollama_client=FakeClient(),
        model="gemini-3.7-flash",
        session={"user_id": "#V#michael_witbrock"},
        source_assertion_id="ska-answer-1",
    )

    assert result == {
        "status": "persistence_failed",
        "effect_status": "failed",
        "synthesis": "Primary Labs' main product is the news site theprimary.com.",
        "notes_updated": False,
        "error_code": "concept_notes_write_failed",
        "source_assertion_id": "ska-answer-1",
    }


def test_q_and_a_input_is_stored_exactly_and_linked_to_target_concept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def fake_store(**kwargs: Any) -> dict[str, Any]:
        calls["store"] = kwargs
        return {
            "assertion_id": "ska-answer-1",
            "storage_surface": "scoped_knowledge_assertions",
            "changed": True,
        }

    def fake_link(**kwargs: Any) -> dict[str, Any]:
        calls["link"] = kwargs
        return {"links_added": 1}

    monkeypatch.setattr(scoped_assertion_service, "store_text_assertion", fake_store)
    monkeypatch.setattr(
        scoped_assertion_service,
        "add_text_assertion_concept_links",
        fake_link,
    )

    result = concept_service._store_concept_qa_input_assertion(
        interaction_id="interaction-1",
        input_kind="answer",
        input_ordinal=2,
        question_or_statement="What is Primary Labs' main product or project?",
        input_text="The news site theprimary.com",
        concept={"concept_id": "#V#primary_labs"},
        session={
            "user_id": "#V#michael_witbrock",
            "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "namespace": ("#V#michael_witbrock@university_of_auckland_strong_ai_lab"),
        },
    )

    assert result["status"] == "stored"
    assert result["assertion_id"] == "ska-answer-1"
    assert result["target_concept_id"] == "#V#primary_labs"
    assert result["canonical_publication"] is False
    assert calls["store"]["text"] == "The news site theprimary.com"
    assert calls["store"]["scope_mode"] == "user"
    assert calls["store"]["source_event_id"] == (
        "concept_q_and_a:interaction-1:answer:2"
    )
    assert calls["store"]["evidence"]["input_kind"] == "answer"
    assert calls["link"]["links"][0] == {
        "concept_id": "#V#primary_labs",
        "role": "about",
        "method": "concept_q_and_a_target",
        "confidence": 1.0,
        "evidence": {
            "source": "concept_q_and_a",
            "question_or_statement": ("What is Primary Labs' main product or project?"),
        },
    }


def test_q_and_a_answer_retains_salient_predicate_as_formalisation_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_store(**kwargs: Any) -> dict[str, Any]:
        captured["store"] = kwargs
        return {
            "assertion_id": "ska-role-answer",
            "storage_surface": "scoped_knowledge_assertions",
            "changed": True,
        }

    def fake_link(**kwargs: Any) -> dict[str, Any]:
        captured["link"] = kwargs
        return {"links_added": 1}

    monkeypatch.setattr(scoped_assertion_service, "store_text_assertion", fake_store)
    monkeypatch.setattr(
        scoped_assertion_service,
        "add_text_assertion_concept_links",
        fake_link,
    )

    result = concept_service._store_concept_qa_input_assertion(
        interaction_id="interaction-role",
        input_kind="answer",
        input_ordinal=1,
        question_or_statement="What role do you have in Primary Labs?",
        input_text="I advise on its knowledge systems.",
        concept={"concept_id": "#V#primary_labs"},
        session={
            "user_id": "#V#michael_witbrock",
            "namespace": "#V#michael_witbrock@personal",
        },
        proposed_predicate={
            "predicate_concept_id": "#V#has_member_role",
            "predicate_label": "has member role",
        },
    )

    assert result["formalisation_status"] == "candidate"
    assert result["proposed_predicate_concept_id"] == "#V#has_member_role"
    assert captured["store"]["evidence"]["proposed_formalisation"] == {
        "predicate_concept_id": "#V#has_member_role",
        "predicate_label": "has member role",
        "status": "candidate_from_salient_question",
    }
    assert (
        captured["link"]["links"][0]["evidence"]["proposed_predicate_concept_id"]
        == "#V#has_member_role"
    )


def test_initial_question_is_persisted_before_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = mongomock.MongoClient()["concept_qa_question"]
    session_id = ObjectId()
    session = {
        "_id": session_id,
        "status": "active",
        "user_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
        "history": [
            {
                "interaction_type": "user_provided_initial_notes",
                "details": {"notes": "I'm a member of Primary Labs."},
            }
        ],
    }
    database.interactions.insert_one(dict(session))
    monkeypatch.setattr(concept_service, "get_db", lambda: database)
    monkeypatch.setattr(role_resolver, "get_user_role", lambda *_args: "member")

    concept_service._record_initial_interaction_question(
        interaction_session=session,
        question_or_statement="What role do you have in Primary Labs?",
    )

    stored = database.interactions.find_one({"_id": session_id})
    assert stored is not None
    assert stored["history"][-1]["interaction_type"] == "llm_question"
    assert stored["history"][-1]["details"] == {
        "question": "What role do you have in Primary Labs?"
    }
    assert session["history"][-1]["details"] == {
        "question": "What role do you have in Primary Labs?"
    }


def test_start_session_represents_unsaved_initial_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = mongomock.MongoClient()["concept_qa_initial_notes"]
    concept_object_id = ObjectId()
    database.concepts.insert_one(
        {
            "_id": concept_object_id,
            "concept_id": "#V#primary_labs",
            "name": "Primary Labs",
        }
    )
    captured: dict[str, Any] = {}

    def fake_store(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "status": "stored",
            "effect_status": "succeeded",
            "assertion_id": "ska-initial-notes",
            "target_concept_id": "#V#primary_labs",
            "canonical_publication": False,
        }

    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "collection",
        lambda: database.concepts,
    )
    monkeypatch.setattr(concept_service, "get_db", lambda: database)
    monkeypatch.setattr(role_resolver, "get_user_role", lambda *_args: "member")
    monkeypatch.setattr(
        concept_service,
        "_store_concept_qa_input_assertion",
        fake_store,
    )

    result = concept_service.start_interaction_session(
        "#V#primary_labs",
        user_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        initial_notes="I'm a member of Primary Labs.",
    )

    assert result["initial_notes_representation"]["status"] == "stored"
    assert captured["input_kind"] == "initial_notes"
    assert captured["input_text"] == "I'm a member of Primary Labs."
    stored = database.interactions.find_one({"_id": ObjectId(result["interaction_id"])})
    assert stored is not None
    initial_entry = stored["history"][0]
    assert initial_entry["details"]["notes"] == "I'm a member of Primary Labs."
    assert initial_entry["details"]["representation"]["assertion_id"] == (
        "ska-initial-notes"
    )
