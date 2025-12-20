import pytest


def test_build_user_specific_system_prompt_returns_none_for_blank_user_id(monkeypatch):
    from src.backend.services.chat_auxiliary_prompt_service import (
        build_user_specific_system_prompt,
    )

    assert build_user_specific_system_prompt("") is None
    assert build_user_specific_system_prompt("   ") is None


def test_build_user_specific_system_prompt_concatenates_multiple_prompts(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
            {"concept_id": "#V#prompt_b"},
        ],
    )

    def _fake_get_content(concept_id):
        if concept_id == "#V#prompt_a":
            return "First"
        if concept_id == "#V#prompt_b":
            return "Second\n"
        raise AssertionError(f"Unexpected concept_id {concept_id}")

    monkeypatch.setattr(service, "_get_prompt_content_for_concept", _fake_get_content)

    result = service.build_user_specific_system_prompt("#V#michael_witbrock")
    assert result == "First\n\nSecond"


def test_build_user_specific_system_prompt_skips_missing_content(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
            {"concept_id": "#V#prompt_b"},
        ],
    )

    def _fake_get_content(concept_id):
        if concept_id == "#V#prompt_a":
            return "   "
        if concept_id == "#V#prompt_b":
            return None
        raise AssertionError(f"Unexpected concept_id {concept_id}")

    monkeypatch.setattr(service, "_get_prompt_content_for_concept", _fake_get_content)

    assert service.build_user_specific_system_prompt("#V#michael_witbrock") is None


def test_get_user_specific_prompt_fragments_returns_ids_and_content(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
            {"concept_id": "#V#prompt_b"},
        ],
    )

    def _fake_get_content(concept_id):
        return f"Content for {concept_id}"

    monkeypatch.setattr(service, "_get_prompt_content_for_concept", _fake_get_content)

    fragments = service.get_user_specific_prompt_fragments("#V#michael_witbrock")
    assert fragments == [
        {"concept_id": "#V#prompt_a", "content": "Content for #V#prompt_a"},
        {"concept_id": "#V#prompt_b", "content": "Content for #V#prompt_b"},
    ]


def test_get_user_specific_prompt_fragments_falls_back_to_text_relations(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
        ],
    )

    monkeypatch.setattr(
        service, "get_concept_by_concept_id", lambda _cid: {"concept_id": _cid}
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda _cid: [
            {"predicate": "hasName", "text": "Ignored"},
            {"predicate": "hasContent", "lang": "en-NZ", "text": "Primary"},
        ],
    )

    fragments = service.get_user_specific_prompt_fragments("#V#michael_witbrock")
    assert fragments == [{"concept_id": "#V#prompt_a", "content": "Primary"}]
