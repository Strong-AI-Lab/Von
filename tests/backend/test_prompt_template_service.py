import pytest

from src.backend.services import prompt_template_service as pts


def test_render_prompt_with_variables(monkeypatch):
    monkeypatch.setattr(
        pts,
        "get_texts_for_concept",
        lambda concept_id: [
            {"predicate": "hasContent", "text": "Kia ora {name}", "lang": "en-NZ"}
        ],
    )
    service = pts.PromptTemplateService()
    rendered = service.render_prompt(["#V#demo_prompt"], variables={"name": "Alex"})
    assert rendered is not None
    assert rendered.text == "Kia ora Alex"
    assert rendered.prompt_id == "#V#demo_prompt"


def test_render_prompt_missing_variable_raises(monkeypatch):
    monkeypatch.setattr(
        pts,
        "get_texts_for_concept",
        lambda concept_id: [
            {"predicate": "hasContent", "text": "Hello {name}", "lang": "en"}
        ],
    )
    service = pts.PromptTemplateService()
    with pytest.raises(ValueError):
        service.render_prompt(["#V#demo_prompt"], variables={})


def test_resolve_prompt_text_supports_v_prefixed_text_relations(monkeypatch):
    monkeypatch.setattr(
        pts,
        "get_texts_for_concept",
        lambda concept_id: [
            {"predicate": "#V#hasContent", "text": "Canonical prompt body"}
        ],
    )
    service = pts.PromptTemplateService()

    prompt_id, prompt_text = service.resolve_prompt_text(["#V#demo_prompt"])

    assert prompt_id == "#V#demo_prompt"
    assert prompt_text == "Canonical prompt body"
