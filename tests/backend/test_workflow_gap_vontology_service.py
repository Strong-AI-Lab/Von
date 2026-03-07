"""Tests for workflow-gap Vontology prompt support."""

from __future__ import annotations


def test_resolve_analysis_prompt_prefers_workflow_link(monkeypatch) -> None:
    from src.backend.services import workflow_gap_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda concept_id, predicate=None, limit=1: (
            [{"text": "#V#linked_gap_analysis_prompt"}]
            if concept_id == mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
            and predicate == mod.WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE
            else []
        ),
    )

    resolved = mod.resolve_workflow_gap_analysis_prompt_concept_id(
        workflow_id=mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    )

    assert resolved == "#V#linked_gap_analysis_prompt"


def test_ensure_prompt_support_creates_prompts_and_links_workflows(monkeypatch) -> None:
    from src.backend.services import workflow_gap_vontology_service as mod

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

    report = mod.ensure_workflow_gap_prompt_support()

    assert report["success"] is True
    assert {
        item["concept_id"] for item in created if isinstance(item.get("concept_id"), str)
    } == {
        mod.WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
        mod.WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
    }
    assert report["counts"]["linked_workflows"] == 2
    analysis_link = next(
        item
        for item in upserts
        if item["subject_concept_id"] == mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    )
    assert analysis_link["predicate"] == mod.WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE
    test_link = next(
        item
        for item in upserts
        if item["subject_concept_id"] == mod.WORKFLOW_GAP_TEST_WORKFLOW_ID
    )
    assert test_link["predicate"] == mod.WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE


def test_render_analysis_prompt_reports_missing_prompt(monkeypatch) -> None:
    from src.backend.services import workflow_gap_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_workflow_gap_analysis_prompt_concept_id",
        lambda **_kwargs: mod.WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    )

    class _FakePromptService:
        def __init__(self, *, default_max_chars: int = 0) -> None:
            self.default_max_chars = default_max_chars

        def render_prompt(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mod, "PromptTemplateService", _FakePromptService)

    rendered, diagnostics = mod.render_workflow_gap_analysis_prompt(
        workflow_id=mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        variables={"analysis_payload_json": "{}"},
    )

    assert rendered is None
    assert diagnostics["error"] == "workflow_gap_analysis_prompt_missing_or_empty"
