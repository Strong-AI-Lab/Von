import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from src.backend.services import conversation_model_context_service as svc


@pytest.fixture()
def roster_sources(monkeypatch):
    invites = [
        {"status": status, "inviter_user_id": "#V#owner", "invitee_user_id": person}
        for person, status in [
            ("#V#viewer", "accepted"),
            ("#V#pending", "pending"),
            ("#V#revoked", "revoked"),
        ]
    ]
    invites.append(
        {
            "status": "accepted",
            "inviter_user_id": "#V#other_owner",
            "invitee_user_id": "#V#foreign",
        }
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.list_active_invites_for_session",
        lambda **_: invites,
    )
    queries = []

    def find(query):
        queries.append(query)
        # The owner's private profile is not visible; do not synthesize a
        # profile read bypass merely because they share a conversation.
        return [{"concept_id": "#V#viewer"}]

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find", find
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.resolve_concept_display_names",
        lambda docs, **_: {doc["concept_id"]: "Viewer" for doc in docs},
    )
    return queries


def test_roster_includes_only_owner_and_accepted_participants(roster_sources):
    roster = svc.conversation_participants(
        session_id="session", owner_id="#V#owner", actor_id="#V#viewer"
    )
    assert roster["status"] == "available"
    assert roster["participants"] == [
        {
            "concept_id": "#V#owner",
            "display_name": "#V#owner",
            "conversation_role": "owner",
        },
        {
            "concept_id": "#V#viewer",
            "display_name": "Viewer",
            "conversation_role": "accepted_participant",
        },
    ]
    assert roster_sources == [{"concept_id": {"$in": ["#V#owner", "#V#viewer"]}}]


def test_nonparticipant_receives_no_roster_or_profile_reads(roster_sources):
    assert svc.conversation_participants(
        session_id="session", owner_id="#V#owner", actor_id="#V#pending"
    ) == {"status": "access_changed", "participants": []}
    assert roster_sources == []


def test_roster_unavailability_does_not_invent_members(monkeypatch):
    def unavailable(**_):
        raise RuntimeError("offline")

    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.list_active_invites_for_session",
        unavailable,
    )
    assert svc.conversation_participants(
        session_id="session", owner_id="#V#owner", actor_id="#V#owner"
    ) == {"status": "unavailable", "participants": []}


def _project(messages):
    return svc.project_conversation_model_context(
        messages,
        actor_id="#V#viewer",
        organisation_id="#V#org",
        namespace="#V#viewer@org",
        owner_id="#V#owner",
        roster={"status": "available", "participants": []},
        now=datetime(2026, 9, 11, tzinfo=UTC),
    )


def test_authors_survive_provider_text_projection_without_rewriting_history():
    messages = [
        {
            "role": "user",
            "author_user_id": "#V#owner",
            "content": "Create tasks, not emails.",
            "timestamp": "2026-09-11T00:00:00Z",
        },
        {
            "role": "user",
            "author_user_id": "#V#viewer",
            "content": "Tell me about the tasks.",
        },
        {"role": "user", "content": "A legacy turn with unknown author"},
    ]
    before = deepcopy(messages)
    result = _project(messages)
    assert messages == before
    assert "#V#owner" in result[1]["content"] and result[1]["content"].endswith(
        messages[0]["content"]
    )
    assert "#V#viewer" in result[2]["content"] and result[2]["content"].endswith(
        messages[1]["content"]
    )
    assert result[3] == messages[2]  # no invented author
    # These are the actual provider-safe fields; custom author metadata is not required.
    from src.backend.languagemodels.structured_tool_calling.providers.openai_client import (
        OpenAIClient,
    )

    assert (
        "#V#owner" in OpenAIClient._responses_context_message_item(result[1])["content"]
    )
    assert "#V#viewer" in OpenAIClient._chat_context_message(result[2])["content"]


@pytest.mark.parametrize(
    "source,comparison",
    [
        (
            {
                "user_concept_id": "#V#viewer",
                "organisation_concept_id": None,
                "namespace": "#V#viewer",
            },
            "different",
        ),
        (
            {
                "user_concept_id": "#V#owner",
                "organisation_concept_id": "#V#org",
                "namespace": "#V#owner@org",
            },
            "different",
        ),
        (
            {
                "user_concept_id": "#V#viewer",
                "organisation_concept_id": "#V#org",
                "namespace": "#V#viewer@org",
            },
            "same",
        ),
        ({}, "unknown"),
    ],
)
def test_prior_results_retain_scope_and_are_never_labelled_fresh(source, comparison):
    original = json.dumps(
        {"error_code": "NOT_FOUND", "provenance": source, "turn_id": "previous-turn"}
    )
    result = _project([{"role": "tool", "content": original}])
    assert '"scope_comparison": "' + comparison + '"' in result[1]["content"]
    assert "historical_tool_result" in result[1]["content"]
    assert result[1]["content"].endswith(original)
    assert '"organisation_concept_id": "#V#org"' in result[0]["content"]
    assert "not additional authority" in result[0]["content"]


def test_truncated_tool_evidence_is_retained_without_invented_scope():
    original = '{"incomplete":'
    result = _project([{"role": "tool", "content": original}])
    assert result[1]["content"].endswith(original)
    assert '"scope_comparison": "unknown"' in result[1]["content"]
