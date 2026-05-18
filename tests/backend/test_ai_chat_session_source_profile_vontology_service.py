from __future__ import annotations

import json
from typing import Any

import pytest

from src.backend.services import (
    ai_chat_session_source_profile_vontology_service as service,
)
from src.backend.services.concept_service import ConceptNotFoundError


def _fake_profile_definition() -> dict[str, Any]:
    return {
        "definition_schema_version": service.SOURCE_PROFILE_DEFINITION_SCHEMA_VERSION,
        "profile_concept_id": "#V#synthetic_chat_session_source_profile",
        "environment": "synthetic",
        "display_name": "Synthetic chat-session source profile",
        "source_system": "synthetic_chat_session",
        "adapter_kind": "generic_file_glob",
        "document_type": {
            "concept_id": "#V#synthetic_chat_session_document",
            "name": "Synthetic Chat Session Document",
            "parent_concept_id": "#V#ai_assisted_programming_chat_session_document",
            "description": "Synthetic document type for tests.",
        },
        "file_copy_type": {
            "concept_id": "#V#synthetic_chat_session_file_copy",
            "name": "Synthetic Chat Session File Copy",
            "parent_concept_id": "#V#ai_assisted_programming_chat_session_file_copy",
            "description": "Synthetic file-copy type for tests.",
        },
        "default_root_templates": ["~/synthetic-chat-sessions"],
        "file_patterns": ["*.jsonl"],
        "name_tokens": ["session"],
        "required_roots": False,
    }


def _fake_catalogue_definition() -> dict[str, Any]:
    catalogue, _profiles, _metadata = (
        service.load_ai_chat_session_source_profiles_from_seed_fixture()
    )
    catalogue = dict(catalogue)
    catalogue["profile_concept_ids"] = ["#V#synthetic_chat_session_source_profile"]
    return catalogue


def test_load_source_profile_authority_uses_represented_synthetic_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue = _fake_catalogue_definition()
    profile = _fake_profile_definition()
    concept_ids = {
        service.SOURCE_PROFILE_CATALOGUE_CONCEPT_ID,
        profile["profile_concept_id"],
    }

    def _get_concept(concept_id: str) -> dict[str, Any]:
        if concept_id not in concept_ids:
            raise ConceptNotFoundError("missing")
        return {"concept_id": concept_id}

    def _get_texts(
        concept_id: str,
        *,
        predicate: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        if (
            concept_id == service.SOURCE_PROFILE_CATALOGUE_CONCEPT_ID
            and predicate == service.HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON
        ):
            return [{"text": json.dumps(catalogue)}]
        if (
            concept_id == profile["profile_concept_id"]
            and predicate == service.HAS_SOURCE_PROFILE_DEFINITION_JSON
        ):
            return [{"text": json.dumps(profile)}]
        return []

    monkeypatch.setattr(service, "get_concept_by_concept_id", _get_concept)
    monkeypatch.setattr(service, "get_texts_for_concept", _get_texts)

    authority, diagnostics = service.load_ai_chat_session_source_profile_authority()

    loaded_profile = authority.profile_for_environment("synthetic")
    assert loaded_profile.source_system == "synthetic_chat_session"
    assert loaded_profile.file_copy_type.concept_id == (
        "#V#synthetic_chat_session_file_copy"
    )
    assert diagnostics["catalogue_source_predicate"] == (
        service.HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON
    )


def test_load_source_profile_authority_fails_closed_when_profile_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue = _fake_catalogue_definition()

    def _get_concept(concept_id: str) -> dict[str, Any]:
        if concept_id == service.SOURCE_PROFILE_CATALOGUE_CONCEPT_ID:
            return {"concept_id": concept_id}
        raise ConceptNotFoundError("missing")

    def _get_texts(
        concept_id: str,
        *,
        predicate: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        if (
            concept_id == service.SOURCE_PROFILE_CATALOGUE_CONCEPT_ID
            and predicate == service.HAS_SOURCE_PROFILE_CATALOGUE_DEFINITION_JSON
        ):
            return [{"text": json.dumps(catalogue)}]
        return []

    monkeypatch.setattr(service, "get_concept_by_concept_id", _get_concept)
    monkeypatch.setattr(service, "get_texts_for_concept", _get_texts)

    with pytest.raises(service.AIChatSessionSourceProfileAuthorityMissingError) as exc:
        service.load_ai_chat_session_source_profile_authority()

    assert str(exc.value) == "source_profile_authority_incomplete"
    assert exc.value.diagnostics["missing_concept_ids"] == [
        "#V#synthetic_chat_session_source_profile"
    ]


def test_ensure_canonical_source_profiles_imports_seed_as_represented_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: set[str] = set()
    persisted: list[dict[str, Any]] = []
    relationships: list[tuple[str, str, str]] = []

    def _get_concept(concept_id: str) -> dict[str, Any]:
        if concept_id in created:
            return {"concept_id": concept_id}
        raise ConceptNotFoundError("missing")

    def _create_concept(**kwargs: Any) -> dict[str, Any]:
        concept_id = str(kwargs["concept_id"])
        created.add(concept_id)
        return {"concept_id": concept_id}

    def _get_texts_for_concept(
        _concept_id: str,
        *,
        predicate: str,
        limit: int = 1,
    ) -> list[dict[str, str]]:
        return []

    def _upsert_singleton_text_relation(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        return {"success": True}

    def _add_relationship(
        source_id: str, predicate: str, target: str
    ) -> dict[str, Any]:
        relationships.append((source_id, predicate, target))
        return {"success": True}

    monkeypatch.setattr(service, "get_concept_by_concept_id", _get_concept)
    monkeypatch.setattr(service.concept_service, "create_concept", _create_concept)
    monkeypatch.setattr(service, "get_texts_for_concept", _get_texts_for_concept)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _upsert_singleton_text_relation,
    )
    monkeypatch.setattr(service, "add_relationship", _add_relationship)

    report = (
        service.ensure_canonical_ai_chat_session_source_profiles_from_seed_fixture()
    )

    assert report["success"] is True
    assert service.SOURCE_PROFILE_TYPE_ID in created
    assert service.SOURCE_PROFILE_CATALOGUE_CONCEPT_ID in created
    assert service.HAS_SOURCE_PROFILE_DEFINITION_JSON in created
    assert service.SOURCE_PROFILE_CATALOGUE_CONCEPT_ID in (
        item["subject_concept_id"] for item in persisted
    )
    assert any(
        item["predicate"] == service.HAS_SOURCE_PROFILE_DEFINITION_JSON
        for item in persisted
    )
    assert any(
        predicate == service.HAS_SOURCE_PROFILE
        for _source, predicate, _target in relationships
    )
