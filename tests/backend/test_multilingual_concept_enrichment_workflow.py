from __future__ import annotations

from types import SimpleNamespace


def test_workflow_definition_structure() -> None:
    from src.backend.workflows.durable.multilingual_concept_enrichment_workflow import (
        MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        build_multilingual_concept_enrichment_workflow_test_definition,
    )

    workflow = build_multilingual_concept_enrichment_workflow_test_definition()

    assert workflow.workflow_id == MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID
    assert workflow.initial_state == "assess"
    assert set(workflow.states) == {
        "assess",
        "prepare_candidate",
        "generate_translations",
        "apply_translations",
        "complete",
        "failed",
    }
    assert workflow.states["complete"].terminal is True


def test_assess_candidates_uses_usage_threshold_and_missing_slots(monkeypatch) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import (
        multilingual_concept_enrichment_workflow as mod,
    )

    concept_doc = {
        "concept_id": "#V#concept",
        "name": "Concept",
        "relationships": {"is_a_type_of": ["#V#entity"]},
        "updated_at": "2026-04-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        mod.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [concept_doc],
    )
    monkeypatch.setattr(
        mod,
        "get_concept_display_name_with_names_fallback",
        lambda _doc: "Concept",
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [
            {"predicate": "#V#hasName", "lang": "en-NZ", "text": "Concept"},
            {
                "predicate": "#V#hasDescription",
                "lang": "en-NZ",
                "text": "A high quality English description with enough detail "
                "to support faithful multilingual concept descriptions.",
            },
            {"predicate": "#V#hasName", "lang": "es", "text": "Concepto"},
        ],
    )
    monkeypatch.setattr(
        mod,
        "build_concept_usage_profile",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#concept",
            "meets_minimum_total_usage": True,
            "usage_metrics": {"total_usage_count": 9},
        },
    )

    request = WorkflowActionRequest(
        action_id="multilingual_concept_enrichment.assess_candidates",
        inputs={},
        environment=WorkflowEnvironment(llm_client=None),
        data={"minimum_total_usage": 4, "min_description_chars": 20},
    )

    result = mod._handle_assess_candidates(request)

    assert result.ok
    assert result.outputs["multilingual_candidate_found"] is True
    assert result.outputs["multilingual_candidate_ids"] == ["#V#concept"]
    summary = result.outputs["multilingual_candidate_summaries"][0]
    assert summary["missing_target_slots"]["es"] == ["description"]
    assert summary["missing_target_slots"]["zh-Hans"] == ["name", "description"]


def test_generate_translations_uses_vontology_prompt(monkeypatch) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import (
        multilingual_concept_enrichment_workflow as mod,
    )

    class _FakeLlm:
        def generate(self, _prompt: str, llm_params=None):
            return (
                '{"concept_id":"#V#concept","confidence":0.93,'
                '"translations":{"fr":{"name":"Concept",'
                '"description":"Une description francaise fidele du concept."}}}'
            )

    monkeypatch.setattr(
        mod,
        "render_multilingual_concept_translation_prompt",
        lambda **_kwargs: (SimpleNamespace(text="prompt"), {"loaded": True}),
    )
    request = WorkflowActionRequest(
        action_id="multilingual_concept_enrichment.generate_translations",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_FakeLlm()),
        data={
            "current_multilingual_candidate_id": "#V#concept",
            "current_multilingual_candidate_summary": {
                "concept_id": "#V#concept",
                "missing_target_slots": {"fr": ["name", "description"]},
                "source_name": "Concept",
                "source_description": "An English source description.",
            },
            "multilingual_target_languages": ["fr"],
            "min_confidence": 0.86,
        },
    )

    result = mod._handle_generate_translations(request)

    assert result.ok
    assert result.outputs["multilingual_translation_status"] == "actionable"
    assert result.outputs["multilingual_translation_actionable"] is True
    assert result.outputs["multilingual_translation_result"]["translations"]["fr"][
            "description"
    ].startswith("Une description")


def test_apply_translations_fills_only_missing_slots(monkeypatch) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import (
        multilingual_concept_enrichment_workflow as mod,
    )

    rows = [{"predicate": "#V#hasName", "lang": "es", "text": "Concepto"}]
    monkeypatch.setattr(mod, "_text_rows_for_concept", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(mod, "_record_audit", lambda *_args, **_kwargs: None)
    writes: list[tuple[str, str, str, str]] = []

    def _fake_upsert(**kwargs):
        writes.append(
            (
                kwargs["subject_concept_id"],
                kwargs["predicate"],
                kwargs["lang"],
                kwargs["text"],
            )
        )
        return {"success": True}

    monkeypatch.setattr(mod, "upsert_singleton_text_relation", _fake_upsert)
    request = WorkflowActionRequest(
        action_id="multilingual_concept_enrichment.apply_translations",
        inputs={},
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "current_multilingual_candidate_id": "#V#concept",
            "multilingual_translation_status": "actionable",
            "multilingual_translation_actionable": True,
            "multilingual_translation_result": {
                "confidence": 0.93,
                "translations": {
                    "es": {
                        "name": "Concepto",
                        "description": "Una descripcion espanola fiel.",
                    },
                    "fr": {"name": "Concept"},
                },
            },
            "translation_records": [],
            "max_mutations_per_run": 6,
        },
    )

    result = mod._handle_apply_translations(request)

    assert result.ok
    assert writes == [
        (
            "#V#concept",
            "#V#hasDescription",
            "es",
            "Una descripcion espanola fiel.",
        ),
        ("#V#concept", "#V#hasName", "fr", "Concept"),
    ]
    assert result.outputs["applied_count"] == 2
    latest = result.outputs["latest_multilingual_enrichment_action"]
    assert latest["status"] == "applied"
    assert latest["skipped_slots"] == [
        {"lang": "es", "slot": "name", "reason": "slot_already_exists"}
    ]
