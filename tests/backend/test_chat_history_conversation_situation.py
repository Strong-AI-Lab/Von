from __future__ import annotations

import types
from datetime import UTC, datetime
from typing import Any

import pytest

from src.backend.services import chat_history_service

_MISSING = object()


@pytest.fixture(autouse=True)
def _reset_chat_history_read_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_history_service,
        "_CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC",
        0.0,
    )
    monkeypatch.setattr(
        chat_history_service,
        "_CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR",
        None,
    )


def _nested_value(document: dict[str, Any], dotted_key: str) -> Any:
    value: Any = document
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(document, clause) for clause in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(document, clause) for clause in expected):
                return False
            continue

        actual = _nested_value(document, key)
        if isinstance(expected, dict) and "$exists" in expected:
            if (actual is not _MISSING) != bool(expected["$exists"]):
                return False
            continue
        if actual is _MISSING or actual != expected:
            return False
    return True


class _FakeCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents
        self.find_calls: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self.update_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def find_one(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        self.find_calls.append((query, projection))
        for document in self.documents:
            if not _matches(document, query):
                continue
            if not projection:
                return dict(document)
            included = {
                key for key, enabled in projection.items() if enabled and key != "_id"
            }
            return {key: document[key] for key in included if key in document}
        return None

    def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
    ) -> types.SimpleNamespace:
        self.update_calls.append((query, update))
        for document in self.documents:
            if not _matches(document, query):
                continue
            document.update(update.get("$set", {}))
            return types.SimpleNamespace(matched_count=1, modified_count=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)


def _install_collection(
    monkeypatch: pytest.MonkeyPatch,
    documents: list[dict[str, Any]],
) -> _FakeCollection:
    collection = _FakeCollection(documents)
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_mongo_operation_comment",
        lambda **_kwargs: None,
    )
    return collection


def test_session_state_returns_history_and_optional_situation_without_changing_history_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 7, 29, 8, 30, tzinfo=UTC)
    history = [
        {"role": "system", "content": "__RESET__"},
        {"role": "user", "content": "Continue the paper work"},
    ]
    collection = _install_collection(
        monkeypatch,
        [
            {
                "user_id": "#V#other-user",
                "session_id": "session-1",
                "namespace": "#V#other-user@org",
                "history": [{"role": "user", "content": "Private other history"}],
            },
            {
                "user_id": "#V#user",
                "session_id": "session-1",
                "namespace": "#V#user@org",
                "history": history,
                "conversation_situation": {
                    "text": "We are continuing the represented paper task.",
                    "revision": 3,
                    "source": "adaptive_turn",
                    "updated_by": "#V#user",
                    "updated_at": timestamp,
                },
            },
        ],
    )

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
        namespace="#V#user@org",
    )

    assert state == {
        "mode": None,
        "origin_kind": None,
        "created_by_actor_concept_id": None,
        "created_by_actor_type": None,
        "is_agent_created": False,
        "test_artifact_kind": None,
        "focal_concept_ids": [],
        "focal_concept_ids_source": None,
        "focal_concept_ids_updated_at": None,
        "session_id": "session-1",
        "history": history,
        "conversation_situation": {
            "text": "We are continuing the represented paper task.",
            "revision": 3,
            "source": "adaptive_turn",
            "updated_by": "#V#user",
            "updated_at": timestamp.isoformat(),
        },
        "conversation_observations": [],
        "conversation_observation_state": {
            "schema_version": "conversation_observation_state.v1",
            "retained_count": 0,
            "total_count": 0,
            "omitted_count": 0,
            "retention_limit": (
                chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS
            ),
        },
    }
    assert collection.find_calls[0][1] == {
        "mode": 1,
        "origin_kind": 1,
        "concept_q_and_a.schema_version": 1,
        "concept_q_and_a.concept": 1,
        "concept_q_and_a.lifecycle": 1,
        "concept_q_and_a.initial_question": 1,
        "_id": 0,
        "session_id": 1,
        "history": 1,
        "conversation_situation": 1,
        "conversation_observations": 1,
        "conversation_observation_total": 1,
        "focal_concept_ids": 1,
        "focal_concept_ids_source": 1,
        "focal_concept_ids_updated_at": 1,
    }

    legacy_history = chat_history_service.get_chat_history(
        "#V#user",
        "session-1",
        namespace="#V#user@org",
    )
    assert isinstance(legacy_history, list)
    assert legacy_history == history


def test_session_state_can_read_carrier_metadata_without_loading_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        [
            {
                "user_id": "#V#user",
                "session_id": "session-1",
                "history": [{"role": "user", "content": "large transcript"}],
                "conversation_situation": {
                    "text": "The shared situation.",
                    "revision": 2,
                },
                "conversation_observations": [
                    {
                        "schema_version": "conversation_observation.v1",
                        "observation_id": "observation-1",
                        "kind": "durable_workflow_terminal",
                    }
                ],
            }
        ],
    )

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
        include_history=False,
    )

    assert state is not None
    assert state["history"] == []
    assert state["conversation_situation"]["revision"] == 2
    assert state["conversation_observations"][0]["observation_id"] == ("observation-1")
    assert collection.find_calls[0][1] == {
        "mode": 1,
        "origin_kind": 1,
        "concept_q_and_a.schema_version": 1,
        "concept_q_and_a.concept": 1,
        "concept_q_and_a.lifecycle": 1,
        "concept_q_and_a.initial_question": 1,
        "_id": 0,
        "session_id": 1,
        "conversation_situation": 1,
        "conversation_observations": 1,
        "conversation_observation_total": 1,
        "focal_concept_ids": 1,
        "focal_concept_ids_source": 1,
        "focal_concept_ids_updated_at": 1,
    }


def test_set_and_clear_situation_use_monotonic_cas_without_perturbing_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_recency = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    document = {
        "user_id": "#V#user",
        "session_id": "session-1",
        "history": [],
        "updated_at": original_recency,
    }
    _install_collection(monkeypatch, [document])

    created = chat_history_service.set_chat_history_conversation_situation(
        user_id="#V#user",
        session_id="session-1",
        text="Objective: answer from the canonical represented paper.",
        expected_revision=0,
        source="adaptive_turn",
        updated_by="#V#user",
        source_request_id="request-situation-1",
    )

    assert created["updated"] is True
    assert created["matched"] is True
    assert created["conflict"] is False
    assert created["current_revision"] == 1
    assert document["updated_at"] == original_recency
    assert document["conversation_situation"]["revision"] == 1
    assert document["conversation_situation"]["source_request_id"] == (
        "request-situation-1"
    )

    cleared = chat_history_service.clear_chat_history_conversation_situation(
        user_id="#V#user",
        session_id="session-1",
        expected_revision=1,
        source="user_reset",
        updated_by="#V#user",
    )

    assert cleared["updated"] is True
    assert cleared["current_revision"] == 2
    assert cleared["conversation_situation"]["text"] is None
    assert document["conversation_situation"]["revision"] == 2
    assert "text" not in document["conversation_situation"]
    assert document["updated_at"] == original_recency

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
    )
    assert state is not None
    assert state["conversation_situation"]["revision"] == 2
    assert state["conversation_situation"]["text"] is None


def test_situation_revision_conflict_preserves_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "text": "Current shared situation",
        "revision": 2,
        "source": "adaptive_turn",
        "updated_by": "#V#user",
        "updated_at": datetime(2026, 7, 29, tzinfo=UTC),
    }
    document = {
        "user_id": "#V#user",
        "session_id": "session-1",
        "history": [],
        "conversation_situation": current,
    }
    _install_collection(monkeypatch, [document])

    result = chat_history_service.set_chat_history_conversation_situation(
        user_id="#V#user",
        session_id="session-1",
        text="Stale replacement",
        expected_revision=1,
        source="adaptive_turn",
        updated_by="#V#user",
    )

    assert result["updated"] is False
    assert result["matched"] is True
    assert result["conflict"] is True
    assert result["expected_revision"] == 1
    assert result["current_revision"] == 2
    assert result["conversation_situation"]["text"] == "Current shared situation"
    assert document["conversation_situation"] is current


def test_situation_cas_reports_missing_session_without_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(monkeypatch, [])

    result = chat_history_service.set_chat_history_conversation_situation(
        user_id="#V#user",
        session_id="missing",
        text="Do not create a session implicitly.",
        expected_revision=0,
        source="adaptive_turn",
        updated_by="#V#user",
    )

    assert result == {
        "updated": False,
        "matched": False,
        "conflict": False,
        "expected_revision": 0,
        "current_revision": None,
        "session_id": "missing",
        "conversation_situation": None,
    }
    assert collection.documents == []


def test_malformed_optional_situation_does_not_hide_history_or_get_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = {
        "user_id": "#V#user",
        "session_id": "session-1",
        "history": [{"role": "user", "content": "Still available"}],
        "conversation_situation": "malformed",
    }
    _install_collection(monkeypatch, [document])

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
    )

    assert state == {
        "mode": None,
        "origin_kind": None,
        "created_by_actor_concept_id": None,
        "created_by_actor_type": None,
        "is_agent_created": False,
        "test_artifact_kind": None,
        "focal_concept_ids": [],
        "focal_concept_ids_source": None,
        "focal_concept_ids_updated_at": None,
        "session_id": "session-1",
        "history": [{"role": "user", "content": "Still available"}],
        "conversation_situation": None,
        "conversation_observations": [],
        "conversation_observation_state": {
            "schema_version": "conversation_observation_state.v1",
            "retained_count": 0,
            "total_count": 0,
            "omitted_count": 0,
            "retention_limit": (
                chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS
            ),
        },
    }
    assert "Ignoring malformed conversation situation" in caplog.text

    result = chat_history_service.set_chat_history_conversation_situation(
        user_id="#V#user",
        session_id="session-1",
        text="Replacement must not bypass CAS.",
        expected_revision=0,
        source="adaptive_turn",
        updated_by="#V#user",
    )
    assert result["updated"] is False
    assert result["conflict"] is True
    assert document["conversation_situation"] == "malformed"


def test_malformed_optional_observation_does_not_hide_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [{"role": "user", "content": "The transcript survives."}]
    _install_collection(
        monkeypatch,
        [
            {
                "user_id": "#V#user",
                "session_id": "session-1",
                "history": history,
                "conversation_observations": [
                    {
                        "observation_id": "x" * 1_000,
                        "kind": "durable_workflow_terminal",
                    },
                    {
                        "observation_id": "valid-observation",
                        "kind": "durable_workflow_terminal",
                    },
                ],
            }
        ],
    )

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
    )

    assert state is not None
    assert state["history"] == history
    assert [item["observation_id"] for item in state["conversation_observations"]] == [
        "valid-observation"
    ]
    assert state["conversation_observation_state"]["total_count"] == 2
    assert state["conversation_observation_state"]["omitted_count"] == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_revision": True}, "expected_revision must be"),
        ({"source": ""}, "source is required"),
        ({"updated_by": ""}, "updated_by is required"),
        (
            {
                "text": "x"
                * (chat_history_service.CONVERSATION_SITUATION_TEXT_MAX_CHARS + 1)
            },
            "characters or fewer",
        ),
    ],
)
def test_situation_writes_validate_bounded_cas_input(
    overrides: dict[str, Any],
    message: str,
) -> None:
    arguments: dict[str, Any] = {
        "user_id": "#V#user",
        "session_id": "session-1",
        "text": "A bounded situation",
        "expected_revision": 0,
        "source": "adaptive_turn",
        "updated_by": "#V#user",
    }
    arguments.update(overrides)

    with pytest.raises(chat_history_service.ChatHistoryServiceError, match=message):
        chat_history_service.set_chat_history_conversation_situation(**arguments)


def test_exact_conversation_observations_are_idempotent_and_do_not_touch_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mongomock

    original_recency = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    collection = mongomock.MongoClient().von_test.chat_history
    collection.insert_one(
        {
            "user_id": "#V#user",
            "session_id": "session-1",
            "namespace": "#V#user@org",
            "history": [],
            "updated_at": original_recency,
        }
    )
    stored_recency = collection.find_one({"session_id": "session-1"})["updated_at"]
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_mongo_operation_comment",
        lambda **_kwargs: None,
    )
    observation = {
        "schema_version": "conversation_observation.v1",
        "observation_id": "late-terminal-1",
        "kind": "durable_workflow_terminal",
        "request_id": "request-1",
        "effect_id": "effect-1",
        "workflow_id": "#V#paper_representation_workflow",
        "instance_id": "instance-1",
        "terminal_status": "completed",
        "effect_status": "succeeded",
        "outcome_finality": "canonical_durable_terminal",
        "changed": True,
    }

    first = chat_history_service.append_chat_history_conversation_observation(
        user_id="#V#user",
        session_id="session-1",
        namespace="#V#user@org",
        observation=observation,
    )
    duplicate = chat_history_service.append_chat_history_conversation_observation(
        user_id="#V#user",
        session_id="session-1",
        namespace="#V#user@org",
        observation=observation,
    )

    assert first["updated"] is True
    assert duplicate["updated"] is False
    assert duplicate["duplicate"] is True
    stored = collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["updated_at"] == stored_recency
    assert stored["conversation_observations"] == [observation]


def test_conversation_observations_are_bounded_and_resettable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mongomock

    collection = mongomock.MongoClient().von_test.chat_history
    collection.insert_one(
        {
            "user_id": "#V#user",
            "session_id": "session-1",
            "history": [],
        }
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_mongo_operation_comment",
        lambda **_kwargs: None,
    )

    for index in range(chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS + 3):
        observation = {
            "observation_id": f"observation-{index}",
            "kind": "durable_workflow_terminal",
            "terminal_status": "completed",
        }
        if index == chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS + 2:
            observation.update(
                {
                    "error": "actor-scoped private diagnostic",
                    "error_step": "private-step",
                    "failure_detail_available": True,
                }
            )
        outcome = chat_history_service.append_chat_history_conversation_observation(
            user_id="#V#user",
            session_id="session-1",
            observation=observation,
        )
        assert outcome["updated"] is True

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
    )
    assert state is not None
    observations = state["conversation_observations"]
    assert len(observations) == (
        chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS
    )
    assert observations[0]["observation_id"] == "observation-3"
    assert observations[-1]["observation_id"] == "observation-14"
    assert observations[-1]["failure_detail_available"] is True
    assert "error" not in observations[-1]
    assert "error_step" not in observations[-1]
    assert state["conversation_observation_state"] == {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": (chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS),
        "total_count": (chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS + 3),
        "omitted_count": 3,
        "retention_limit": (chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS),
    }

    cleared = chat_history_service.clear_chat_history_conversation_observations(
        user_id="#V#user",
        session_id="session-1",
    )
    assert cleared["updated"] is True
    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
    )
    assert state is not None
    assert state["conversation_observations"] == []
    assert state["conversation_observation_state"]["total_count"] == 0


@pytest.mark.parametrize("stored_total", [_MISSING, "malformed"])
def test_observation_append_self_heals_legacy_or_malformed_total(
    monkeypatch: pytest.MonkeyPatch,
    stored_total: Any,
) -> None:
    import mongomock

    collection = mongomock.MongoClient().von_test.chat_history
    document = {
        "user_id": "#V#user",
        "session_id": "session-legacy-observations",
        "history": [],
        "conversation_observations": [
            {
                "observation_id": f"legacy-{index}",
                "kind": "durable_workflow_terminal",
            }
            for index in range(chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS)
        ],
    }
    if stored_total is not _MISSING:
        document["conversation_observation_total"] = stored_total
    collection.insert_one(document)
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_mongo_operation_comment",
        lambda **_kwargs: None,
    )

    outcome = chat_history_service.append_chat_history_conversation_observation(
        user_id="#V#user",
        session_id="session-legacy-observations",
        observation={
            "observation_id": "new-terminal",
            "kind": "durable_workflow_terminal",
        },
    )
    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-legacy-observations",
    )

    assert outcome["updated"] is True
    assert state is not None
    assert state["conversation_observation_state"]["total_count"] == (
        chat_history_service.CONVERSATION_OBSERVATION_MAX_ITEMS + 1
    )
    assert state["conversation_observation_state"]["omitted_count"] == 1
    assert state["conversation_observations"][0]["observation_id"] == "legacy-1"
    assert state["conversation_observations"][-1]["observation_id"] == ("new-terminal")


def test_session_state_tail_projection_avoids_loading_unrequested_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mongomock

    collection = mongomock.MongoClient().von_test.chat_history
    collection.insert_one(
        {
            "user_id": "#V#user",
            "session_id": "session-1",
            "history": [
                {"role": "user", "content": f"message-{index}"} for index in range(20)
            ],
            "conversation_situation": {
                "text": "The conversation continues.",
                "revision": 1,
                "source": "adaptive_turn",
                "updated_by": "#V#user",
                "updated_at": datetime(2026, 7, 29, tzinfo=UTC),
            },
        }
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_mongo_operation_comment",
        lambda **_kwargs: None,
    )

    state = chat_history_service.get_chat_history_session_state(
        user_id="#V#user",
        session_id="session-1",
        history_tail_limit=5,
        include_debug=True,
    )

    assert state is not None
    assert [item["content"] for item in state["history"]] == [
        "message-15",
        "message-16",
        "message-17",
        "message-18",
        "message-19",
    ]
    assert state["history_offset"] == 15
    assert state["history_truncated"] is True
    assert state["conversation_situation"]["revision"] == 1


def test_reset_uses_one_atomic_revision_advancing_carrier_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    collection = MagicMock()
    collection.update_one.return_value = types.SimpleNamespace(
        matched_count=1,
        modified_count=1,
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_chat_history_query",
        lambda **_kwargs: {
            "user_id": "#V#user",
            "session_id": "session-1",
        },
    )

    outcome = chat_history_service.reset_chat_history_conversation_state(
        user_id="#V#user",
        session_id="session-1",
        updated_by="#V#user",
    )

    assert outcome["updated"] is True
    assert outcome["matched"] is True
    assert collection.update_one.call_count == 1
    query, pipeline = collection.update_one.call_args.args
    assert query == {"user_id": "#V#user", "session_id": "session-1"}
    assert isinstance(pipeline, list)
    assert pipeline[0]["$set"]["history"]["$concatArrays"][1][0]["content"] == (
        "__RESET__"
    )
    revision_expression = pipeline[0]["$set"]["conversation_situation"]["revision"]
    assert revision_expression["$add"][1] == 1
    assert pipeline[0]["$set"]["conversation_situation"]["source"] == (
        "conversation_reset"
    )
    assert pipeline[1] == {"$unset": "conversation_observations"}
