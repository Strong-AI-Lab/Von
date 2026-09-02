from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

import mongomock
import pytest

USER_ID = "#V#user"
ORG_ID = "#V#org"
NAMESPACE = "#V#user@org"
CONCEPT_ID = "#V#primary_labs"
SESSION_ID = "3ed2de92-cdef-4e76-a800-8cd0af811c91"


def _chat_collection():
    return mongomock.MongoClient()["von_test"]["chat_history"]


def _session_doc(*, assistant_metadata=None):
    now = datetime.now(UTC)
    history = [
        {
            "role": "assistant",
            "content": "Who is a member of Primary Labs?",
            "turn_id": "assistant-initial",
            "concept_q_and_a": assistant_metadata or {"kind": "initial_question"},
            "timestamp": now,
        }
    ]
    return {
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "namespace": NAMESPACE,
        "organisation_concept_id": ORG_ID,
        "mode": "concept_q_and_a",
        "origin_kind": "concept_q_and_a",
        "focal_concept_ids": [CONCEPT_ID],
        "created_at": now,
        "updated_at": now,
        "history": history,
        "concept_q_and_a": {
            "schema_version": "concept_q_and_a_session.v1",
            "concept": {"concept_id": CONCEPT_ID, "name": "Primary Labs"},
            "lifecycle": {"status": "active", "revision": 0},
            "turn_receipts": {},
        },
    }


def _patch_chat_store(monkeypatch, service, collection):
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(service, "_collection", lambda **_kwargs: collection)
    monkeypatch.setattr(
        service,
        "_load_focal_concept",
        lambda *_args, **_kwargs: {
            "_id": "507f1f77bcf86cd799439011",
            "concept_id": CONCEPT_ID,
            "name": "Primary Labs",
        },
    )


def test_question_or_meta_input_is_transcript_only(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    monkeypatch.setattr(
        service.concept_service,
        "_store_concept_qa_input_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a user question must not become a knowledge assertion")
        ),
    )
    receipt = service._aggregate_exact_input_receipt(
        session_id=SESSION_ID,
        turn_id="turn-question",
        answer="Did you represent that in Vontology?",
        notes_input="",
        concept={"concept_id": CONCEPT_ID},
        session_doc=_session_doc(),
    )

    assert receipt["status"] == "not_admitted"
    assert receipt["items"][0]["classification"] == "user_question"
    assert receipt["items"][0]["asserted_knowledge"] is False


def test_representation_status_question_is_answered_from_receipt_without_model(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    doc = _session_doc()
    doc["history"].insert(
        0,
        {
            "role": "user",
            "content": "Michael Witbrock",
            "turn_id": "source-turn",
            "concept_q_and_a": {
                "kind": "user_input",
                "input_kind": "asserted_knowledge",
            },
        },
    )
    doc["concept_q_and_a"]["turn_receipts"]["source-turn"] = {
        "turn_id": "source-turn",
        "exact_input": {
            "status": "stored",
            "effect_status": "succeeded",
            "assertion_ids": ["ska_exact_source"],
        },
        "formalisation": {
            "status": "tentative",
            "effect_status": "succeeded",
            "assertion_id": "ska_member",
            "predicate_concept_id": "#V#memberOfVonOrg",
            "activation_effect": "organisation_membership",
            "canonical_publication": False,
        },
        "notes": {
            "status": "not_updated",
            "effect_status": "not_needed",
            "notes_updated": False,
        },
    }
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)
    monkeypatch.setattr(
        service.concept_service,
        "_store_concept_qa_input_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a representation-status question is transcript-only")
        ),
    )
    monkeypatch.setattr(
        service.concept_service,
        "submit_concept_answer",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt-aware status must not be improvised by a model")
        ),
    )

    result = service.submit_concept_qa_turn(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        turn_id="status-question",
        user_id=USER_ID,
        answer="Did you represent that info in Vontology?",
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )

    assistant = result["turns"][-1]
    status = assistant["concept_q_and_a"]["representation_status"]
    assert assistant["concept_q_and_a"]["kind"] == "representation_status_response"
    assert "actor-scoped text claim with provenance" in assistant["content"]
    assert "stored as tentative" in assistant["content"]
    assert "does not activate organisation membership" in assistant["content"]
    assert "canonical concept notes were not changed" in assistant["content"]
    assert status["source_turn_id"] == "source-turn"
    assert status["exact_assertion_ids"] == ["ska_exact_source"]
    assert status["formalisation_status"] == "tentative"
    assert result["receipts"]["exact_input"]["status"] == "not_admitted"
    assert result["receipts"]["representation_status"] == status


def test_autoformalisation_requires_declared_tentative_permission(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    monkeypatch.setattr(
        service.concept_resolution_service,
        "resolve_concept_by_name",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolution must not run without declared permission")
        ),
    )
    exact = {
        "items": [
            {
                "input_kind": "answer",
                "status": "stored",
                "assertion_id": "ska_source",
            }
        ]
    }
    result = service._tentative_formalisation_receipt(
        exact_input=exact,
        answer="Michael Witbrock",
        proposed_predicate={
            "predicate_concept_id": "#V#memberOfVonOrg",
            "focal_argument": "object",
            "other_argument_type_concept_id": "#V#von_user",
            "auto_formalisation_allowed": False,
            "auto_formalisation_target_status": "tentative",
        },
        concept_id=CONCEPT_ID,
        concept_name="Primary Labs",
        session_id=SESSION_ID,
        turn_id="turn-1",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
    )

    assert result["status"] == "deferred"
    assert result["reason_code"] == "auto_formalisation_permission_missing"


def test_safe_short_answer_is_stored_as_tentative_direction_aware_relation(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service

    monkeypatch.setattr(
        service.concept_resolution_service,
        "resolve_concept_by_name",
        lambda **kwargs: {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": "#V#michael_witbrock",
            "match": {"stage": "exact_name"},
            "candidates": [],
        },
    )
    captured: dict = {}

    def _upsert(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "effect_status": "succeeded",
            "assertion_id": "ska_tentative_member",
            "canonical_read_back": {
                "assertion_id": "ska_tentative_member",
                "subject_concept_id": "#V#michael_witbrock",
                "predicate": "#V#memberOfVonOrg",
                "object_concept_id": CONCEPT_ID,
                "epistemic_status": "tentative",
            },
        }

    monkeypatch.setattr(
        service.scoped_assertion_service, "upsert_scoped_assertion", _upsert
    )
    exact = {
        "items": [
            {
                "input_kind": "answer",
                "status": "stored",
                "assertion_id": "ska_source_text",
            }
        ]
    }
    result = service._tentative_formalisation_receipt(
        exact_input=exact,
        answer="Michael Witbrock",
        proposed_predicate={
            "requirement_id": "organisation_has_von_user",
            "predicate_concept_id": "#V#memberOfVonOrg",
            "focal_argument": "object",
            "other_argument_type_concept_id": "#V#von_user",
            "declared_on_type_concept_id": "#V#von_user_organisation",
            "declaration_source": "represented",
            "profile_predicate": "#V#hasConstitutiveRelationRequirement",
            "auto_formalisation_allowed": True,
            "auto_formalisation_target_status": "tentative",
            "confirmation_question_template": (
                "Should {counterpart_name} be confirmed as a member of {instance_name}?"
            ),
        },
        concept_id=CONCEPT_ID,
        concept_name="Primary Labs",
        session_id=SESSION_ID,
        turn_id="turn-member",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
    )

    assert result["status"] == "tentative"
    assert result["subject_concept_id"] == "#V#michael_witbrock"
    assert result["object_concept_id"] == CONCEPT_ID
    assert result["source_assertion_ids"] == ["ska_source_text"]
    assert result["confirmation"]["available"] is True
    assert result["confirmation"]["requires_operational_authority"] is True
    assert result["requirement_id"] == "organisation_has_von_user"
    assert result["declaration_source"] == "represented"
    assert result["profile_predicate"] == "#V#hasConstitutiveRelationRequirement"
    assert (
        service._confirmation_question(
            formalisation=result,
            answer_text="Michael Witbrock",
        )
        == "Should Michael Witbrock be confirmed as a member of Primary Labs?"
    )
    assert captured["epistemic_status"] == "tentative"
    assert captured["scope_mode"] == "user"
    assert captured["canonical_publication"] is False


def test_code_string_answer_cannot_bypass_counterpart_type_resolution(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    captured: dict = {}

    def _resolve(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
        }

    monkeypatch.setattr(
        service.concept_resolution_service,
        "resolve_concept_by_name",
        _resolve,
    )
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "upsert_scoped_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an incompatible code string must not become a relation")
        ),
    )
    result = service._tentative_formalisation_receipt(
        exact_input={
            "items": [
                {
                    "input_kind": "answer",
                    "status": "stored",
                    "assertion_id": "ska_source",
                }
            ]
        },
        answer="#V#some_organisation",
        proposed_predicate={
            "predicate_concept_id": "#V#memberOfVonOrg",
            "focal_argument": "object",
            "other_argument_type_concept_id": "#V#von_user",
            "auto_formalisation_allowed": True,
            "auto_formalisation_target_status": "tentative",
        },
        concept_id=CONCEPT_ID,
        concept_name="Primary Labs",
        session_id=SESSION_ID,
        turn_id="turn-code",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
    )

    assert captured["match_code_strings"] is False
    assert captured["instance_of"] == "#V#von_user"
    assert result["status"] == "deferred"
    assert result["reason_code"] == "entity_reference_not_found"


def test_answer_and_notes_are_distinct_exact_transcript_turns_before_downstream(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    collection.insert_one(_session_doc())
    _patch_chat_store(monkeypatch, service, collection)
    assertion_number = 0

    def _store_exact(**kwargs):
        nonlocal assertion_number
        assertion_number += 1
        return {
            "status": "stored",
            "effect_status": "succeeded",
            "assertion_id": f"ska_exact_{assertion_number}",
            "target_concept_id": CONCEPT_ID,
            "canonical_publication": False,
        }

    monkeypatch.setattr(
        service.concept_service,
        "_store_concept_qa_input_assertion",
        _store_exact,
    )
    monkeypatch.setattr(service, "_current_elicitation_predicate", lambda _cid: None)

    def _downstream(**kwargs):
        assert kwargs["allow_canonical_notes_update"] is False
        stored = collection.find_one({"session_id": SESSION_ID})
        pending = stored["concept_q_and_a"]["turn_receipts"]["turn-dual"]
        assert "completed_at" not in pending
        assert [
            item["content"] for item in stored["history"] if item["role"] == "user"
        ] == [
            "The CEO is Aron D'Souza.",
            "The website is theprimary.com.",
        ]
        return {
            "status": "success",
            "next_step_type": "llm_question",
            "next_step_content": "Who is the CTO?",
            "representation": {
                "concept_notes": {
                    "status": "updated",
                    "effect_status": "succeeded",
                    "notes_updated": True,
                    "source_assertion_id": "ska_exact_1",
                }
            },
            "llm_debug_data": {
                "schema_version": "concept_q_and_a_llm_debug.v1",
                "selected": {"provider": "test", "model": "non-sol-test"},
            },
        }

    monkeypatch.setattr(service.concept_service, "submit_concept_answer", _downstream)

    result = service.submit_concept_qa_turn(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        turn_id="turn-dual",
        user_id=USER_ID,
        answer="The CEO is Aron D'Souza.",
        notes_input="The website is theprimary.com.",
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )

    assert result["status"] == "success"
    assert len(result["receipts"]["exact_input"]["assertion_ids"]) == 2
    assert len(result["receipts"]["transcript_turn_ids"]) == 2
    assert result["receipts"]["completed_at"]
    assistant = result["turns"][-1]
    assert assistant["content"] == "Who is the CTO?"
    assert assistant["llm_debug_data"]["selected"]["model"] == "non-sol-test"
    assert assistant["llm_debug_data"]["selected"]["provider"] == "test"
    assert assistant["llm_debug_data"]["model"] == "non-sol-test"
    assert assistant["llm_debug_data"]["provider"] == "test"

    replay = service.submit_concept_qa_turn(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        turn_id="turn-dual",
        user_id=USER_ID,
        answer="The CEO is Aron D'Souza.",
        notes_input="The website is theprimary.com.",
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )
    assert replay["replayed"] is True
    assert collection.count_documents({"session_id": SESSION_ID}) == 1

    with pytest.raises(service.ConceptQAConversationConflict) as conflict:
        service.submit_concept_qa_turn(
            concept_id=CONCEPT_ID,
            session_id=SESSION_ID,
            turn_id="turn-dual",
            user_id=USER_ID,
            answer="A different answer.",
            notes_input="The website is theprimary.com.",
            namespace=NAMESPACE,
            organisation_concept_id=ORG_ID,
        )
    assert conflict.value.error_code == "turn_id_content_conflict"


def test_membership_activation_success_is_retained_when_promotion_needs_retry(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service
    from src.backend.services import (
        organisation_membership_governance_service,
        organisation_membership_service,
    )

    assertion = {
        "assertion_id": "ska_member",
        "subject_concept_id": "#V#michael_witbrock",
        "predicate": "#V#memberOfVonOrg",
        "object_concept_id": CONCEPT_ID,
        "epistemic_status": "tentative",
    }
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "get_visible_scoped_assertion_by_id",
        lambda *_args, **_kwargs: assertion,
    )
    governed = {
        "success": True,
        "effect_status": "succeeded",
        "canonical_read_back": {
            "membership_present": True,
            "role": "member",
        },
        "governance_receipt": {"receipt_id": "omr_1", "status": "succeeded"},
    }
    monkeypatch.setattr(
        organisation_membership_governance_service,
        "manage_organisation_membership",
        lambda **_kwargs: governed,
    )
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "promote_scoped_assertion_epistemic_status",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary failure")),
    )
    formalisation = {
        "assertion_id": "ska_member",
        "subject_concept_id": "#V#michael_witbrock",
        "predicate_concept_id": "#V#memberOfVonOrg",
        "object_concept_id": CONCEPT_ID,
        "focal_argument": "object",
        "other_argument_type_concept_id": "#V#von_user",
        "focal_concept_id": CONCEPT_ID,
    }

    result = service._confirm_formalisation_effect(
        formalisation=formalisation,
        assertion_id="ska_member",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
        request_id="confirm-member",
        expected_focal_concept_id=CONCEPT_ID,
    )

    assert result["success"] is False
    assert result["effect_status"] == "partial_success"
    assert result["reconciliation_required"] is True
    assert result["governed_activation"] == governed
    assert result["canonical_read_back"]["membership_present"] is True


def test_generic_confirmation_is_idempotent_and_top_level_receipt_is_coherent(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    doc = _session_doc()
    formalisation = {
        "schema_version": "concept_q_and_a_formalisation_receipt.v1",
        "kind": "formalisation",
        "status": "tentative",
        "effect_status": "succeeded",
        "assertion_id": "ska_relation",
        "subject_concept_id": CONCEPT_ID,
        "predicate_concept_id": "#V#hasChiefScientist",
        "object_concept_id": "#V#michael_witbrock",
        "requirement_id": "organisation_has_chief_scientist",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#person",
        "declared_on_type_concept_id": "#V#organisation",
        "declaration_source": "represented",
        "profile_predicate": "#V#hasConstitutiveRelationRequirement",
        "canonical_read_back": {"epistemic_status": "tentative"},
        "confirmation": {"available": True, "method": "POST"},
    }
    doc["concept_q_and_a"]["turn_receipts"]["source-turn"] = {
        "turn_id": "source-turn",
        "formalisation": formalisation,
    }
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)
    assertion_state = {
        "assertion_id": "ska_relation",
        "subject_concept_id": CONCEPT_ID,
        "predicate": "#V#hasChiefScientist",
        "object_concept_id": "#V#michael_witbrock",
        "epistemic_status": "tentative",
    }
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "get_visible_scoped_assertion_by_id",
        lambda *_args, **_kwargs: dict(assertion_state),
    )

    def _promote(**_kwargs):
        changed = assertion_state["epistemic_status"] != "asserted"
        assertion_state["epistemic_status"] = "asserted"
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": changed,
            "canonical_read_back": dict(assertion_state),
        }

    monkeypatch.setattr(
        service.scoped_assertion_service,
        "promote_scoped_assertion_epistemic_status",
        _promote,
    )
    monkeypatch.setattr(
        service.concept_service,
        "_get_concept_elicitation_plan",
        lambda _concept_id: [
            {
                **formalisation,
                "question": "Who is the chief scientist?",
            },
            {
                "requirement_id": "organisation_has_research_lead",
                "predicate_concept_id": "#V#hasChiefScientist",
                "focal_argument": "subject",
                "other_argument_type_concept_id": "#V#research_lead",
                "declared_on_type_concept_id": "#V#organisation",
                "question": "Who is the research lead?",
            },
        ],
    )

    first = service.confirm_concept_qa_formalisation(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        assertion_id="ska_relation",
        user_id=USER_ID,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )
    repeated = service.confirm_concept_qa_formalisation(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        assertion_id="ska_relation",
        user_id=USER_ID,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )

    confirmed = first["receipts"]["formalisation"]
    assert first["success"] is True
    assert confirmed["status"] == "asserted"
    assert confirmed["effect_status"] == "succeeded"
    assert confirmed["canonical_read_back"]["epistemic_status"] == "asserted"
    assert confirmed["confirmation"]["available"] is False
    assert repeated["success"] is True
    assert repeated["confirmation"]["idempotent_replay"] is True
    result_turns = [
        turn
        for turn in repeated["turns"]
        if turn.get("concept_q_and_a", {}).get("kind")
        == "formalisation_confirmation_result"
    ]
    assert len(result_turns) == 1
    assert "Who is the research lead?" in result_turns[0]["content"]
    assert (
        result_turns[0]["concept_q_and_a"]["elicitation_predicate"]["requirement_id"]
        == "organisation_has_research_lead"
    )
    assert (
        service._pending_confirmation(collection.find_one({"session_id": SESSION_ID}))
        is None
    )


def test_direct_confirmation_updates_finished_receipt_without_reopening_transcript(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    doc = _session_doc()
    doc["concept_q_and_a"]["lifecycle"] = {
        "status": "finished",
        "revision": 1,
        "terminal_at": datetime.now(UTC),
    }
    formalisation = {
        "schema_version": "concept_q_and_a_formalisation_receipt.v1",
        "kind": "formalisation",
        "status": "tentative",
        "effect_status": "succeeded",
        "assertion_id": "ska_finished_relation",
        "subject_concept_id": CONCEPT_ID,
        "predicate_concept_id": "#V#hasChiefScientist",
        "object_concept_id": "#V#michael_witbrock",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#person",
        "declared_on_type_concept_id": "#V#organisation",
        "canonical_publication": False,
        "confirmation": {
            "available": True,
            "available_after_finish": True,
            "available_after_cancel": False,
        },
    }
    doc["concept_q_and_a"]["turn_receipts"]["source-turn"] = {
        "turn_id": "source-turn",
        "formalisation": formalisation,
    }
    history_count = len(doc["history"])
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)
    monkeypatch.setattr(
        service,
        "_confirm_formalisation_effect",
        lambda **_kwargs: {
            "success": True,
            "status": "asserted",
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {"epistemic_status": "asserted"},
        },
    )

    result = service.confirm_concept_qa_formalisation(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        assertion_id="ska_finished_relation",
        user_id=USER_ID,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )

    assert result["success"] is True
    assert result["confirmed_after_finish"] is True
    assert result["assistant_turn_id"] is None
    assert result["lifecycle"]["status"] == "finished"
    assert len(result["turns"]) == history_count
    assert result["receipts"]["formalisation"]["status"] == "asserted"
    assert result["receipts"]["formalisation"]["confirmation"]["available"] is False


def test_direct_confirmation_is_not_available_after_cancel(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    doc = _session_doc()
    doc["concept_q_and_a"]["lifecycle"] = {
        "status": "cancelled",
        "revision": 1,
        "terminal_at": datetime.now(UTC),
    }
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)

    with pytest.raises(service.ConceptQAConversationConflict) as exc_info:
        service.confirm_concept_qa_formalisation(
            concept_id=CONCEPT_ID,
            session_id=SESSION_ID,
            assertion_id="ska_cancelled_relation",
            user_id=USER_ID,
            namespace=NAMESPACE,
            organisation_concept_id=ORG_ID,
        )

    assert exc_info.value.error_code == "conversation_cancelled"


def test_member_confirmation_authority_denial_preserves_tentative_candidate(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service
    from src.backend.services import (
        organisation_membership_governance_service,
        organisation_membership_service,
    )

    assertion = {
        "assertion_id": "ska_member_denied",
        "subject_concept_id": "#V#person",
        "predicate": "#V#memberOfVonOrg",
        "object_concept_id": CONCEPT_ID,
        "epistemic_status": "tentative",
    }
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "get_visible_scoped_assertion_by_id",
        lambda *_args, **_kwargs: assertion,
    )
    monkeypatch.setattr(
        organisation_membership_governance_service,
        "manage_organisation_membership",
        lambda **_kwargs: {
            "success": False,
            "effect_status": "not_started",
            "error_code": "organisation_membership_management_authority_required",
            "authority_decision": {
                "allowed": False,
                "required_permissions": ["MANAGE_MEMBERS"],
            },
            "governance_receipt": {"status": "denied"},
        },
    )
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "promote_scoped_assertion_epistemic_status",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("denied membership must not promote")
        ),
    )

    result = service._confirm_formalisation_effect(
        formalisation={
            "assertion_id": "ska_member_denied",
            "subject_concept_id": "#V#person",
            "predicate_concept_id": "#V#memberOfVonOrg",
            "object_concept_id": CONCEPT_ID,
            "focal_argument": "object",
            "other_argument_type_concept_id": "#V#von_user",
            "focal_concept_id": CONCEPT_ID,
        },
        assertion_id="ska_member_denied",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
        request_id="confirm-denied",
        expected_focal_concept_id=CONCEPT_ID,
    )

    assert result["success"] is False
    assert result["status"] == "tentative"
    assert result["authority_decision"]["allowed"] is False
    assert result["actionable_denial"]["tentative_assertion_preserved"] is True
    assert result["actionable_denial"]["required_permissions"] == ["MANAGE_MEMBERS"]
    assert result["actionable_denial"]["mechanism"] == (
        "canonical_organisation_membership_handoff"
    )
    assert (
        result["actionable_denial"]["same_q_and_a_action_available_to_other_actor"]
        is False
    )


def test_preexisting_membership_lets_original_actor_confirm_without_self_grant(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service
    from src.backend.services import (
        organisation_membership_governance_service,
        organisation_membership_service,
    )

    assertion = {
        "assertion_id": "ska_member_preexisting",
        "subject_concept_id": "#V#person",
        "predicate": "#V#memberOfVonOrg",
        "object_concept_id": CONCEPT_ID,
        "epistemic_status": "tentative",
    }
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "get_visible_scoped_assertion_by_id",
        lambda *_args, **_kwargs: assertion,
    )
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args, **_kwargs: {
            "user_concept_id": "#V#person",
            "organisation_concept_id": CONCEPT_ID,
            "role": "contributor",
        },
    )
    monkeypatch.setattr(
        organisation_membership_governance_service,
        "manage_organisation_membership",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("preexisting membership must not request a new grant")
        ),
    )
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "promote_scoped_assertion_epistemic_status",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {
                **assertion,
                "epistemic_status": "asserted",
            },
        },
    )

    result = service._confirm_formalisation_effect(
        formalisation={
            "assertion_id": "ska_member_preexisting",
            "subject_concept_id": "#V#person",
            "predicate_concept_id": "#V#memberOfVonOrg",
            "object_concept_id": CONCEPT_ID,
            "focal_argument": "object",
            "other_argument_type_concept_id": "#V#von_user",
            "focal_concept_id": CONCEPT_ID,
        },
        assertion_id="ska_member_preexisting",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
        request_id="confirm-preexisting",
        expected_focal_concept_id=CONCEPT_ID,
    )

    assert result["success"] is True
    assert result["status"] == "asserted"
    assert result["governed_activation"]["postcondition_preexisting"] is True
    assert result["governed_activation"]["changed"] is False


def test_inverse_membership_alias_never_invokes_governed_membership_add(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service
    from src.backend.services import organisation_membership_governance_service

    assertion = {
        "assertion_id": "ska_inverse_member",
        "subject_concept_id": CONCEPT_ID,
        "predicate": "#V#has_member",
        "object_concept_id": "#V#person",
        "epistemic_status": "tentative",
    }
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "get_visible_scoped_assertion_by_id",
        lambda *_args, **_kwargs: assertion,
    )
    monkeypatch.setattr(
        organisation_membership_governance_service,
        "manage_organisation_membership",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("inverse/read-compatible aliases must not grant membership")
        ),
    )
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "promote_scoped_assertion_epistemic_status",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {
                **assertion,
                "epistemic_status": "asserted",
            },
        },
    )

    result = service._confirm_formalisation_effect(
        formalisation={
            "assertion_id": "ska_inverse_member",
            "subject_concept_id": CONCEPT_ID,
            "predicate_concept_id": "#V#has_member",
            "object_concept_id": "#V#person",
            "focal_argument": "subject",
            "other_argument_type_concept_id": "#V#von_user",
            "focal_concept_id": CONCEPT_ID,
        },
        assertion_id="ska_inverse_member",
        user_id=USER_ID,
        organisation_concept_id=ORG_ID,
        namespace=NAMESPACE,
        request_id="confirm-inverse",
        expected_focal_concept_id=CONCEPT_ID,
    )

    assert result["success"] is True
    assert result["governed_activation"] is None


@pytest.mark.parametrize(
    ("reply", "reply_kind", "expected_status"),
    [
        ("yes", "affirm", "asserted"),
        ("not now", "defer", "tentative"),
        ("I don't know", "defer", "tentative"),
        ("no", "reject", "rejected"),
    ],
)
def test_conversational_confirmation_reply_binds_to_prior_tentative_without_new_claim(
    monkeypatch, reply, reply_kind, expected_status
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    pending = {
        "assertion_id": "ska_pending",
        "subject_concept_id": CONCEPT_ID,
        "predicate_concept_id": "#V#hasChiefScientist",
        "object_concept_id": "#V#michael_witbrock",
        "requirement_id": "organisation_has_chief_scientist",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#person",
        "declared_on_type_concept_id": "#V#organisation",
        "declaration_source": "represented",
        "profile_predicate": "#V#hasConstitutiveRelationRequirement",
        "status": "tentative",
        "epistemic_status": "tentative",
        "confirmation": {"available": True},
    }
    doc = _session_doc(
        assistant_metadata={
            "kind": "formalisation_confirmation_request",
            "formalisation": pending,
        }
    )
    doc["concept_q_and_a"]["turn_receipts"]["source-turn"] = {
        "turn_id": "source-turn",
        "formalisation": pending,
    }
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)
    monkeypatch.setattr(
        service.concept_service,
        "_store_concept_qa_input_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("confirmation control must not create a text assertion")
        ),
    )
    monkeypatch.setattr(
        service.concept_service,
        "submit_concept_answer",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("typed confirmation takes priority over fresh elicitation")
        ),
    )
    monkeypatch.setattr(
        service.concept_service,
        "_get_concept_elicitation_plan",
        lambda _concept_id: [
            {
                "requirement_id": "organisation_has_chief_scientist",
                "predicate_concept_id": "#V#hasChiefScientist",
                "focal_argument": "subject",
                "other_argument_type_concept_id": "#V#person",
                "declared_on_type_concept_id": "#V#organisation",
                "question": "Who is the chief scientist?",
                "requirement_kind": "constitutive_relation",
            },
            {
                "requirement_id": "organisation_has_research_lead",
                "predicate_concept_id": "#V#hasChiefScientist",
                "focal_argument": "subject",
                "other_argument_type_concept_id": "#V#research_lead",
                "declared_on_type_concept_id": "#V#organisation",
                "question": "Who is the organisation's research lead?",
                "requirement_kind": "constitutive_relation",
            },
        ],
    )
    monkeypatch.setattr(
        service,
        "_confirm_formalisation_effect",
        lambda **_kwargs: {
            "success": True,
            "status": "asserted",
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {"epistemic_status": "asserted"},
        },
    )
    monkeypatch.setattr(
        service.scoped_assertion_service,
        "retract_scoped_assertion",
        lambda **_kwargs: {
            "success": True,
            "changed": True,
            "canonical_read_back": {"status": "retracted"},
        },
    )

    result = service.submit_concept_qa_turn(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        turn_id=f"confirmation-{reply_kind}",
        user_id=USER_ID,
        answer=reply,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )

    current = result["receipts"]
    assert current["exact_input"]["status"] == "not_admitted"
    assert current["exact_input"]["items"][0]["classification"] == "meta_or_control"
    assert current["formalisation"]["reply_kind"] == reply_kind
    assert current["formalisation"]["status"] == expected_status
    assert (
        result["turns"][-1]["concept_q_and_a"]["kind"]
        == "formalisation_confirmation_result"
    )
    stored = collection.find_one({"session_id": SESSION_ID})
    original = stored["concept_q_and_a"]["turn_receipts"]["source-turn"][
        "formalisation"
    ]
    if reply_kind in {"affirm", "reject"}:
        assert original["status"] == expected_status
    else:
        assert original["status"] == "tentative"
    if reply_kind in {"affirm", "defer", "reject"}:
        assistant = result["turns"][-1]
        assert "Who is the organisation's research lead?" in assistant["content"]
        assert "Who is the chief scientist?" not in assistant["content"]
        assert (
            assistant["concept_q_and_a"]["elicitation_predicate"][
                "predicate_concept_id"
            ]
            == "#V#hasChiefScientist"
        )
        assert (
            assistant["concept_q_and_a"]["elicitation_predicate"]["requirement_id"]
            == "organisation_has_research_lead"
        )
    if reply_kind in {"defer", "reject"}:
        assert (
            service.concept_service._elicitation_requirement_identity(pending)
            in stored["concept_q_and_a"]["suppressed_requirement_keys"]
        )
        assert stored["concept_q_and_a"].get("suppressed_predicate_ids", []) == []
    if reply_kind == "affirm":
        assert (
            service.concept_service._elicitation_requirement_identity(pending)
            in stored["concept_q_and_a"]["addressed_requirement_keys"]
        )


def test_focal_concept_visibility_failure_is_indistinguishable_from_absence(
    monkeypatch,
):
    from src.backend.security import access_control
    from src.backend.services import concept_qa_conversation_service as service

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: {"concept_id": CONCEPT_ID},
    )
    monkeypatch.setattr(
        access_control, "override_current_actor", lambda *_args: nullcontext()
    )
    monkeypatch.setattr(access_control, "can_access_concept", lambda _concept_id: False)

    with pytest.raises(service.ConceptQAConversationNotFound) as exc_info:
        service._load_focal_concept(
            CONCEPT_ID,
            user_id="#V#other_actor",
            organisation_concept_id="#V#other_org",
        )

    assert exc_info.value.error_code == "concept_not_found"
    assert str(exc_info.value) == "Focal concept was not found"


def test_chat_carrier_active_key_is_an_atomic_uniqueness_boundary(monkeypatch):
    from pymongo.errors import DuplicateKeyError

    from src.backend.services import chat_history_service

    collection = _chat_collection()
    monkeypatch.setattr(chat_history_service, "_CHAT_HISTORY_INDEXES_READY", False)
    chat_history_service._ensure_chat_history_indexes(collection)
    assert (
        collection.index_information()["concept_q_and_a_active_key_unique_v1"]["unique"]
        is True
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(chat_history_service, "get_session_context", dict)
    state = {
        "schema_version": "concept_q_and_a_session.v1",
        "active_key": "concept_q_and_a:one-actor-one-concept",
        "lifecycle": {"status": "active", "revision": 0},
    }

    chat_history_service.create_chat_session(
        user_id=USER_ID,
        session_id="session-first",
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
        mode="concept_q_and_a",
        origin_kind="concept_q_and_a",
        focal_concept_ids=[CONCEPT_ID],
        concept_q_and_a_state=state,
    )
    with pytest.raises(DuplicateKeyError):
        chat_history_service.create_chat_session(
            user_id=USER_ID,
            session_id="session-racing",
            namespace=NAMESPACE,
            organisation_concept_id=ORG_ID,
            mode="concept_q_and_a",
            origin_kind="concept_q_and_a",
            focal_concept_ids=[CONCEPT_ID],
            concept_q_and_a_state=state,
        )

    assert (
        collection.count_documents({"concept_q_and_a.lifecycle.status": "active"}) == 1
    )


def test_generic_chat_state_preserves_terminal_q_and_a_mode_and_focal_state(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    collection = _chat_collection()
    doc = _session_doc()
    doc["concept_q_and_a"]["lifecycle"] = {
        "status": "finished",
        "revision": 1,
        "terminal_at": "2026-09-02T12:00:00+00:00",
    }
    collection.insert_one(doc)
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )

    state = chat_history_service.get_chat_history_session_state(
        user_id=USER_ID,
        session_id=SESSION_ID,
        namespace=NAMESPACE,
    )

    assert state["mode"] == "concept_q_and_a"
    assert state["origin_kind"] == "concept_q_and_a"
    assert state["focal_concept_ids"] == [CONCEPT_ID]
    assert state["concept_q_and_a"]["lifecycle"]["status"] == "finished"
    assert state["lifecycle"]["status"] == "finished"
    summary = chat_history_service.get_chat_history_session_summary(
        user_id=USER_ID,
        session_id=SESSION_ID,
        namespace=NAMESPACE,
        summary_mode="light",
    )
    assert summary["is_completed"] is True
    assert summary["completed_at"] == "2026-09-02T12:00:00+00:00"


def test_start_resumes_same_active_carrier_and_initial_assistant_is_once(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    collection.create_index(
        [("concept_q_and_a.active_key", 1)],
        name="concept_q_and_a_active_key_unique_v1",
        unique=True,
        partialFilterExpression={
            "mode": "concept_q_and_a",
            "origin_kind": "concept_q_and_a",
            "concept_q_and_a.lifecycle.status": "active",
            "concept_q_and_a.active_key": {"$type": "string"},
        },
    )
    _patch_chat_store(monkeypatch, service, collection)
    monkeypatch.setattr(service.chat_history_service, "get_session_context", dict)
    monkeypatch.setattr(
        service.concept_service,
        "_normalise_interaction_llm_selection",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        service.concept_service,
        "generate_initial_question",
        lambda *_args, **_kwargs: {
            "status": "success",
            "question": "Who is a Von user in this organisation?",
            "llm_debug_data": {
                "schema_version": "concept_q_and_a_llm_debug.v1",
                "selected": {"provider": "test", "model": "non-sol-test"},
            },
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "_get_concept_elicitation_plan",
        lambda _concept_id: [],
    )

    first = service.start_concept_qa_conversation(
        concept_id=CONCEPT_ID,
        user_id=USER_ID,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )
    resumed = service.start_concept_qa_conversation(
        concept_id=CONCEPT_ID,
        user_id=USER_ID,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )

    assert first["created"] is True
    assert resumed["created"] is False
    assert resumed["resumed"] is True
    assert resumed["session_id"] == first["session_id"]
    assert len([turn for turn in resumed["turns"] if turn["role"] == "assistant"]) == 1
    assert resumed["turns"][0]["llm_debug_data"]["selected"]["model"] == "non-sol-test"
    assert resumed["turns"][0]["llm_debug_data"]["model"] == "non-sol-test"
    assert resumed["turns"][0]["llm_debug_data"]["provider"] == "test"
    assert (
        collection.count_documents({"concept_q_and_a.lifecycle.status": "active"}) == 1
    )


def test_initial_assistant_omits_model_debug_when_legacy_generator_has_none(
    monkeypatch,
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    doc = _session_doc()
    doc["history"] = []
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)
    monkeypatch.setattr(
        service.concept_service,
        "generate_initial_question",
        lambda *_args, **_kwargs: {
            "status": "success",
            "question": "What should be recorded?",
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "_get_concept_elicitation_plan",
        lambda _concept_id: (_ for _ in ()).throw(
            AssertionError("the wrapper must not attach the first plan item blindly")
        ),
    )

    service._ensure_initial_assistant_turn(
        user_id=USER_ID,
        namespace=NAMESPACE,
        session_id=SESSION_ID,
        concept={"concept_id": CONCEPT_ID},
        session_doc=doc,
    )

    stored = collection.find_one({"session_id": SESSION_ID})["history"][0]
    assert stored["content"] == "What should be recorded?"
    assert "llm_debug_data" not in stored
    assert "elicitation_predicate" not in stored["concept_q_and_a"]


def test_initial_assistant_uses_question_binding_returned_by_generator(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    doc = _session_doc()
    doc["history"] = []
    collection.insert_one(doc)
    _patch_chat_store(monkeypatch, service, collection)
    bound = {
        "requirement_id": "organisation_has_scientist",
        "predicate_concept_id": "#V#hasChiefScientist",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#person",
        "declared_on_type_concept_id": "#V#organisation",
        "question": "Who is the chief scientist of Primary Labs?",
    }
    monkeypatch.setattr(
        service.concept_service,
        "generate_initial_question",
        lambda *_args, **_kwargs: {
            "status": "success",
            "question": bound["question"],
            "elicitation_predicate": bound,
        },
    )

    service._ensure_initial_assistant_turn(
        user_id=USER_ID,
        namespace=NAMESPACE,
        session_id=SESSION_ID,
        concept={"concept_id": CONCEPT_ID},
        session_doc=doc,
    )

    stored = collection.find_one({"session_id": SESSION_ID})["history"][0]
    assert stored["concept_q_and_a"]["elicitation_predicate"]["requirement_id"] == (
        "organisation_has_scientist"
    )


@pytest.mark.parametrize("target_status", ["finished", "cancelled"])
def test_terminal_lifecycle_has_readback_and_rejects_later_turns(
    monkeypatch, target_status
):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    collection.insert_one(_session_doc())
    _patch_chat_store(monkeypatch, service, collection)

    transitioned = service.transition_concept_qa_conversation(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        user_id=USER_ID,
        target_status=target_status,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
        expected_revision=0,
    )

    assert transitioned["changed"] is True
    assert transitioned["lifecycle"]["status"] == target_status
    assert transitioned["lifecycle"]["revision"] == 1
    repeated = service.transition_concept_qa_conversation(
        concept_id=CONCEPT_ID,
        session_id=SESSION_ID,
        user_id=USER_ID,
        target_status=target_status,
        namespace=NAMESPACE,
        organisation_concept_id=ORG_ID,
    )
    assert repeated["changed"] is False

    with pytest.raises(service.ConceptQAConversationConflict) as conflict:
        service.submit_concept_qa_turn(
            concept_id=CONCEPT_ID,
            session_id=SESSION_ID,
            turn_id="after-terminal",
            user_id=USER_ID,
            answer="This must not append.",
            namespace=NAMESPACE,
            organisation_concept_id=ORG_ID,
        )
    assert conflict.value.error_code == "conversation_terminal"


def test_actor_and_namespace_scope_hide_another_users_carrier(monkeypatch):
    from src.backend.services import concept_qa_conversation_service as service

    collection = _chat_collection()
    collection.insert_one(_session_doc())
    _patch_chat_store(monkeypatch, service, collection)
    monkeypatch.setattr(
        service.concept_service,
        "get_active_interactions_for_concept",
        lambda *_args, **_kwargs: [],
    )

    result = service.list_concept_qa_conversations(
        concept_id=CONCEPT_ID,
        user_id="#V#other_user",
        namespace="#V#other_user@other_org",
        organisation_concept_id="#V#other_org",
    )

    assert result["sessions"] == []
    assert result["active_session"] is None
