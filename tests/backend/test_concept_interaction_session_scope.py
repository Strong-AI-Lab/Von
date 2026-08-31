from __future__ import annotations

from typing import Any

import mongomock
import pytest
from bson import ObjectId

from src.backend.languagemodels import llm_interface
from src.backend.security import role_resolver
from src.backend.services import concept_service, settings_service


def test_interaction_session_preserves_actor_org_namespace_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = mongomock.MongoClient()["concept_interaction_scope"]
    concept_id = ObjectId()
    database.concepts.insert_one(
        {
            "_id": concept_id,
            "concept_id": "#V#primary_labs",
            "name": "Primary Labs",
        }
    )
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "collection",
        lambda: database.concepts,
    )
    monkeypatch.setattr(concept_service, "get_db", lambda: database)
    monkeypatch.setattr(role_resolver, "get_user_role", lambda *_args: "member")

    started = concept_service.start_interaction_session(
        "#V#primary_labs",
        user_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        model_provider="gemini",
        model="gemini-3.7-flash",
        model_parameters={"temperature": 0.2},
    )

    stored = database.interactions.find_one(
        {"_id": ObjectId(started["interaction_id"])}
    )
    assert stored is not None
    assert (
        stored["organisation_concept_id"] == "#V#university_of_auckland_strong_ai_lab"
    )
    assert (
        stored["namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )
    assert stored["llm_selection"] == {
        "provider": "gemini",
        "model": "gemini-3.7-flash",
        "source": "browser_preference",
        "model_parameters": {"temperature": 0.2},
    }

    matching = concept_service.get_interaction_session_by_id(
        started["interaction_id"],
        user_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
    )
    assert matching is not None

    wrong_org = concept_service.get_interaction_session_by_id(
        started["interaction_id"],
        user_id="#V#michael_witbrock",
        organisation_concept_id="#V#another_organisation",
    )
    assert wrong_org is None


def test_interaction_model_runtime_reuses_exact_persisted_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_get_llm_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(llm_interface, "get_llm_client", fake_get_llm_client)
    monkeypatch.setattr(
        llm_interface,
        "get_active_model_name",
        lambda **_kwargs: pytest.fail("persisted model must not be re-resolved"),
    )
    monkeypatch.setattr(
        llm_interface,
        "get_active_model_parameters",
        lambda **_kwargs: pytest.fail("persisted parameters must not be re-resolved"),
    )
    monkeypatch.setattr(
        settings_service,
        "resolve_llm_setting",
        lambda **_kwargs: pytest.fail("persisted provider must not be re-resolved"),
    )

    client, model, params = concept_service._resolve_interaction_llm_runtime(
        {
            "user_id": "#V#michael_witbrock",
            "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "llm_selection": {
                "provider": "gemini",
                "model": "gemini-3.7-flash",
                "model_parameters": {"temperature": 0.2},
            },
        }
    )

    assert client is sentinel
    assert model == "gemini-3.7-flash"
    assert params == {"temperature": 0.2}
    assert captured == {
        "client_type": "gemini",
        "user_concept_id": "#V#michael_witbrock",
        "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
    }
