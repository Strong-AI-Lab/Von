"""Focused bounded-lookup tests for legacy conversation concepts."""

from __future__ import annotations

from typing import Any

from src.backend.services import conversation_concept_service as service


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
