from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services import chat_history_service


def test_get_chat_session_links_normalises_and_deduplicates():
    coll = MagicMock()
    coll.find_one.return_value = {
        "session_links": {
            "programmes": ["#V#programme", "  #V#programme  ", "not-a-concept"],
            "project_ids": ["#V#project_a", "#V#project_a", ""],
            "activity_concept_ids": ["#V#activity_1", None, 123],
            "modalities": "#V#zoom",
        }
    }

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=coll,
        ),
        patch(
            "src.backend.services.chat_history_service.build_chat_history_query",
            return_value={"user_id": "#V#u", "session_id": "s"},
        ),
    ):
        links = chat_history_service.get_chat_session_links(
            user_id="#V#u",
            session_id="s",
            namespace="#V#u",
        )

    assert links == {
        "programmes": ["#V#programme"],
        "projects": ["#V#project_a"],
        "activities": ["#V#activity_1"],
        "modalities": ["#V#zoom"],
    }


def test_set_chat_session_links_normalises_and_does_not_touch_updated_at():
    coll = MagicMock()
    coll.update_one.return_value = SimpleNamespace(modified_count=1, matched_count=1)

    session_links = {
        "programme_ids": ["#V#programme", "#V#programme"],
        "projects": ["#V#project_a", "not-a-concept"],
        "activities": "#V#activity_1",
        "modalities": ["  #V#teams  ", "#V#teams"],
    }

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=coll,
        ),
        patch(
            "src.backend.services.chat_history_service.build_chat_history_query",
            return_value={"user_id": "#V#u", "session_id": "s"},
        ),
    ):
        result = chat_history_service.set_chat_session_links(
            user_id="#V#u",
            session_id="s",
            session_links=session_links,
            namespace="#V#u",
        )

    assert result["matched"] is True
    assert result["updated"] is True
    assert result["session_links"] == {
        "programmes": ["#V#programme"],
        "projects": ["#V#project_a"],
        "activities": ["#V#activity_1"],
        "modalities": ["#V#teams"],
    }

    # Ensure we only update the session_links fields and *not* the session recency.
    args, kwargs = coll.update_one.call_args
    assert kwargs == {}
    update_doc = args[1]

    assert "$set" in update_doc
    set_doc = update_doc["$set"]
    assert "session_links" in set_doc
    assert "session_links_updated_at" in set_doc
    assert "updated_at" not in set_doc


def test_set_chat_session_links_returns_not_matched_when_session_missing():
    coll = MagicMock()
    coll.update_one.return_value = SimpleNamespace(modified_count=0, matched_count=0)

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=coll,
        ),
        patch(
            "src.backend.services.chat_history_service.build_chat_history_query",
            return_value={"user_id": "#V#u", "session_id": "s"},
        ),
    ):
        result = chat_history_service.set_chat_session_links(
            user_id="#V#u",
            session_id="s",
            session_links={},
            namespace="#V#u",
        )

    assert result["matched"] is False
    assert result["updated"] is False
