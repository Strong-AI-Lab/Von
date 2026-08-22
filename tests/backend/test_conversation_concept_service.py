"""Focused bounded-lookup tests for legacy conversation concepts."""

from __future__ import annotations

from typing import Any

from src.backend.services import conversation_concept_service as service
from src.backend.security.visibility_predicates import get_specific_to_user_values


def _conversation_doc(concept_id: str) -> dict[str, Any]:
    return {
        "concept_id": concept_id,
        "relationships": {"is_an_instance_of": [service.CONVERSATION_TYPE_ID]},
    }


def test_lookup_uses_deterministic_conversation_id_before_legacy_paths(
    monkeypatch,
) -> None:
    session_id = "session-deterministic"
    expected_id = service._generate_conversation_concept_id(session_id)
    concept_queries: list[dict[str, Any]] = []

    def _find_one(query: dict[str, Any], projection=None):  # noqa: ANN001
        concept_queries.append(dict(query))
        return _conversation_doc(expected_id)

    monkeypatch.setattr(service.ConceptsRepository, "find_one", _find_one)

    assert service.get_conversation_concept_by_session_id(session_id) == expected_id
    assert concept_queries == [{"concept_id": expected_id}]


def test_lookup_uses_actor_filtered_exact_metadata_before_text_fallback(
    monkeypatch,
) -> None:
    session_id = "legacy-metadata-session"
    expected_id = "#V#legacy_conversation"
    concept_queries: list[dict[str, Any]] = []

    def _find_one(query: dict[str, Any], projection=None):  # noqa: ANN001
        concept_queries.append(dict(query))
        if "metadata.session_id" in query:
            return _conversation_doc(expected_id)
        return None

    monkeypatch.setattr(service.ConceptsRepository, "find_one", _find_one)

    assert service.get_conversation_concept_by_session_id(session_id) == expected_id
    assert concept_queries == [
        {"concept_id": service._generate_conversation_concept_id(session_id)},
        {
            "metadata.session_id": session_id,
            "relationships.is_an_instance_of": service.CONVERSATION_TYPE_ID,
        },
    ]


def test_lookup_uses_one_exact_text_value_and_bounded_relation_lookup(
    monkeypatch,
) -> None:
    session_id = "legacy-text-session"
    text_value_queries: list[tuple[dict[str, Any], dict[str, Any] | None, int]] = []
    relation_queries: list[tuple[dict[str, Any], dict[str, Any] | None, int]] = []
    concept_find_queries: list[tuple[dict[str, Any], dict[str, Any] | None, int]] = []
    concept_find_one_calls = 0

    def _concept_find_one(query: dict[str, Any], projection=None):  # noqa: ANN001
        nonlocal concept_find_one_calls
        concept_find_one_calls += 1
        return None

    def _text_value_find(query, projection=None, **kwargs):  # noqa: ANN001
        text_value_queries.append((dict(query), projection, kwargs.get("limit", 0)))
        return [
            {"_id": "text-value-1", "text": session_id},
            {"_id": "unrelated", "text": "another-session"},
        ]

    def _text_relation_find(query, projection=None, **kwargs):  # noqa: ANN001
        relation_queries.append((dict(query), projection, kwargs.get("limit", 0)))
        return [
            {"subject_concept_id": "#V#not_a_conversation"},
            {"subject_concept_id": "#V#legacy_visible_conversation"},
        ]

    def _concept_find(query, projection=None, **kwargs):  # noqa: ANN001
        concept_find_queries.append((dict(query), projection, kwargs.get("limit", 0)))
        return [_conversation_doc("#V#legacy_visible_conversation")]

    monkeypatch.setattr(service.ConceptsRepository, "find_one", _concept_find_one)
    monkeypatch.setattr(service.ConceptsRepository, "find", _concept_find)
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextValuesRepository.find",
        _text_value_find,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextRelationsRepository.find",
        _text_relation_find,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextValuesRepository.find_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy lookup must not hydrate text values per relation")
        ),
    )

    assert (
        service.get_conversation_concept_by_session_id(session_id)
        == "#V#legacy_visible_conversation"
    )
    assert concept_find_one_calls == 2
    assert len(text_value_queries) == 1
    text_query, _, text_limit = text_value_queries[0]
    assert text_limit == service._LEGACY_SESSION_TEXT_VALUE_LIMIT
    assert text_query == {
        "fingerprint": service._session_id_text_fingerprint(session_id),
        "lang": "en",
    }
    assert len(relation_queries) == 1
    relation_query, _, relation_limit = relation_queries[0]
    assert relation_limit == service._LEGACY_SESSION_RELATION_LIMIT
    assert relation_query["predicate"]["$in"] == [
        service.PREDICATE_HAS_SESSION_ID,
        "hasSessionId",
    ]
    assert relation_query["object_text_id"]["$in"] == ["text-value-1"]
    assert len(concept_find_queries) == 1
    candidate_query, _, candidate_limit = concept_find_queries[0]
    assert candidate_limit == service._LEGACY_SESSION_RELATION_LIMIT
    assert candidate_query == {
        "concept_id": {
            "$in": [
                "#V#not_a_conversation",
                "#V#legacy_visible_conversation",
            ]
        },
        "relationships.is_an_instance_of": service.CONVERSATION_TYPE_ID,
    }


def test_lookup_falls_back_to_exact_text_only_after_fingerprint_miss(
    monkeypatch,
) -> None:
    session_id = "legacy-no-fingerprint-session"
    text_queries: list[dict[str, Any]] = []

    monkeypatch.setattr(
        service.ConceptsRepository, "find_one", lambda *_args, **_kwargs: None
    )

    def _text_value_find(query, **_kwargs):  # noqa: ANN001
        text_queries.append(dict(query))
        if "fingerprint" in query:
            return []
        return [{"_id": "legacy-text-value", "text": session_id}]

    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextValuesRepository.find",
        _text_value_find,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextRelationsRepository.find",
        lambda *_args, **_kwargs: [
            {"subject_concept_id": "#V#legacy_visible_conversation"}
        ],
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            _conversation_doc("#V#legacy_visible_conversation")
        ],
    )

    assert (
        service.get_conversation_concept_by_session_id(session_id)
        == "#V#legacy_visible_conversation"
    )
    assert text_queries == [
        {
            "fingerprint": service._session_id_text_fingerprint(session_id),
            "lang": "en",
        },
        {"text": session_id},
    ]


def test_lookup_rejects_text_relation_subject_that_is_not_visible_conversation(
    monkeypatch,
) -> None:
    session_id = "legacy-hidden-session"

    monkeypatch.setattr(
        service.ConceptsRepository, "find_one", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextValuesRepository.find",
        lambda *_args, **_kwargs: [{"_id": "text-value-1", "text": session_id}],
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextRelationsRepository.find",
        lambda *_args, **_kwargs: [{"subject_concept_id": "#V#foreign"}],
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "concept_id": "#V#foreign",
                "relationships": {"is_an_instance_of": ["#V#other_type"]},
            }
        ],
    )

    assert service.get_conversation_concept_by_session_id(session_id) is None


def test_created_conversation_projection_is_restricted_to_transcript_owner(
    monkeypatch,
) -> None:
    inserted: list[dict[str, Any]] = []
    text_writes: list[dict[str, Any]] = []

    monkeypatch.setattr(
        service, "get_conversation_concept_by_session_id", lambda _session_id: None
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "insert_one",
        lambda doc: inserted.append(dict(doc)),
    )
    monkeypatch.setattr(
        service,
        "upsert_text_for_concept",
        lambda **kwargs: text_writes.append(kwargs),
    )

    conversation_id = service.get_or_create_conversation_concept(
        "owner-scoped-session",
        owner_concept_id="#V#owner",
        namespace="#V#owner@org",
    )

    assert conversation_id == service._generate_conversation_concept_id(
        "owner-scoped-session"
    )
    assert len(inserted) == 1
    relationships = inserted[0]["relationships"]
    assert relationships[service.PREDICATE_HAS_OWNER] == ["#V#owner"]
    assert get_specific_to_user_values(relationships) == ["#V#owner"]
    assert service.PREDICATE_HAS_NAME not in {
        write["predicate"] for write in text_writes
    }


def test_source_backed_conversation_card_is_deterministic_and_minimal(
    monkeypatch,
) -> None:
    session_id = "source-backed-session"
    expected_conversation_id = service._generate_conversation_concept_id(session_id)
    materialise = []

    def _materialise(**kwargs):  # noqa: ANN003
        materialise.append(kwargs)
        return expected_conversation_id

    monkeypatch.setattr(service, "get_or_create_conversation_concept", _materialise)

    card = service.build_source_backed_conversation_projection(
        session_id=session_id,
        owner_concept_id="#V#owner",
        authorised_focal_concepts=[
            {"concept_id": "#V#task", "name": "Task", "type_ids": ["#V#task"]}
        ],
        access_mode="owner",
        session_name="Current session title",
        last_activity_at="2026-08-22T12:00:00+00:00",
        materialise_owner_projection=True,
    )

    assert materialise[0]["session_id"] == session_id
    assert card == {
        "schema_version": "conversation_projection.v1",
        "conversation_concept_id": expected_conversation_id,
        "title": "Current session title",
        "last_activity_at": "2026-08-22T12:00:00+00:00",
        "access_mode": "owner",
        "focal_concepts": [
            {
                "concept_id": "#V#task",
                "display_name": "Task",
                "type_ids": ["#V#task"],
            }
        ],
        "projection_status": "materialised",
        "open_action": {"kind": "open_conversation", "session_id": session_id},
    }
    assert "owner_concept_id" not in card
    assert "history" not in card


def test_shared_source_backed_card_does_not_materialise_owner_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_or_create_conversation_concept",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not materialise")),
    )

    card = service.build_source_backed_conversation_projection(
        session_id="shared-session",
        owner_concept_id="#V#owner",
        authorised_focal_concepts=[],
        access_mode="shared",
    )

    assert card["conversation_concept_id"] is None
    assert card["projection_status"] == "source_backed"
    assert card["title"] == "Conversation"


def test_focal_conversation_backlinks_are_bounded_and_source_scoped() -> None:
    card = {"schema_version": "conversation_projection.v1", "title": "One"}

    backlinks = service.build_focal_conversation_backlinks(
        source_concept_id="#V#task",
        items=[card] * 30,
        more_count=5,
    )

    assert backlinks["schema_version"] == "conversation_backlinks.v1"
    assert backlinks["source_concept_id"] == "#V#task"
    assert len(backlinks["items"]) == 25
    assert backlinks["more_count"] == 5
