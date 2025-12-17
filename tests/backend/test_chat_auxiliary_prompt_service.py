import pytest


def test_build_user_specific_system_prompt_returns_none_for_blank_user_id(monkeypatch):
    from src.backend.services.chat_auxiliary_prompt_service import build_user_specific_system_prompt

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

    def _fake_get(concept_id):
        if concept_id == "#V#prompt_a":
            return {"concept_id": concept_id, "content": "First"}
        if concept_id == "#V#prompt_b":
            return {"concept_id": concept_id, "content": "Second\n"}
        raise AssertionError(f"Unexpected concept_id {concept_id}")

    monkeypatch.setattr(service, "get_concept_by_concept_id", _fake_get)

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

    def _fake_get(concept_id):
        if concept_id == "#V#prompt_a":
            return {"concept_id": concept_id, "content": "   "}
        if concept_id == "#V#prompt_b":
            return {"concept_id": concept_id, "content": None}
        raise AssertionError(f"Unexpected concept_id {concept_id}")

    monkeypatch.setattr(service, "get_concept_by_concept_id", _fake_get)

    assert service.build_user_specific_system_prompt("#V#michael_witbrock") is None
