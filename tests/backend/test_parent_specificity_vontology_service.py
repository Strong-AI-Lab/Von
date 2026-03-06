"""Tests for parent-specificity Vontology prompt support."""

from __future__ import annotations


def test_resolve_prompt_concept_prefers_workflow_link(monkeypatch) -> None:
    from src.backend.services import parent_specificity_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda concept_id, predicate=None, limit=1: (
            [{"text": "#V#linked_prompt"}]
            if concept_id == mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID
            and predicate == mod.PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE
            else []
        ),
    )

    resolved = mod.resolve_parent_specificity_prompt_concept_id(
        workflow_id=mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID
    )

    assert resolved == "#V#linked_prompt"


def test_ensure_prompt_support_creates_prompt_and_links_workflow(monkeypatch) -> None:
    from src.backend.services import parent_specificity_vontology_service as mod

    created: list[dict[str, object]] = []
    upserts: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "_safe_get_concept", lambda _concept_id: None)
    monkeypatch.setattr(
        mod.concept_service,
        "create_concept",
        lambda **kwargs: created.append(kwargs) or {"concept_id": kwargs["concept_id"]},
    )
    monkeypatch.setattr(
        mod,
        "upsert_singleton_text_relation",
        lambda **kwargs: upserts.append(kwargs) or {"success": True},
    )

    report = mod.ensure_parent_specificity_prompt_support()

    assert report["success"] is True
    assert created
    assert created[0]["concept_id"] == mod.PARENT_SPECIFICITY_PROMPT_CONCEPT_ID
    linked_workflow_upsert = next(
        item
        for item in upserts
        if item["subject_concept_id"] == mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID
    )
    assert linked_workflow_upsert["predicate"] == mod.PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE


def test_render_prompt_reports_missing_prompt(monkeypatch) -> None:
    from src.backend.services import parent_specificity_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_parent_specificity_prompt_concept_id",
        lambda **_kwargs: mod.PARENT_SPECIFICITY_PROMPT_CONCEPT_ID,
    )

    class _FakePromptService:
        def __init__(self, *, default_max_chars: int = 0) -> None:
            self.default_max_chars = default_max_chars

        def render_prompt(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mod, "PromptTemplateService", _FakePromptService)

    rendered, diagnostics = mod.render_parent_specificity_prompt(
        workflow_id=mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        variables={"analysis_payload_json": "{}"},
    )

    assert rendered is None
    assert diagnostics["error"] == "parent_specificity_prompt_missing_or_empty"
