from __future__ import annotations

def test_render_prompt_fails_closed_when_authority_missing(monkeypatch) -> None:
    from src.backend.services import multilingual_concept_enrichment_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_multilingual_concept_translation_prompt_concept_id",
        lambda **_kwargs: "#V#multilingual_concept_translation_prompt",
    )
    monkeypatch.setattr(
        mod,
        "render_authoritative_prompt",
        lambda **_kwargs: (
            None,
            {"error": "multilingual_concept_enrichment_prompt_missing_or_empty"},
        ),
    )

    rendered, diagnostics = mod.render_multilingual_concept_translation_prompt(
        workflow_id=mod.MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        variables={"translation_payload_json": "{}"},
    )

    assert rendered is None
    assert diagnostics["error"] == (
        "multilingual_concept_enrichment_prompt_missing_or_empty"
    )
    assert diagnostics["requested_workflow_id"] == (
        mod.MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID
    )


def test_ensure_prompt_support_seeds_only_when_missing(monkeypatch) -> None:
    from src.backend.services import multilingual_concept_enrichment_vontology_service as mod

    has_content = {"value": False}
    writes: list[dict] = []
    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **_kwargs: {"success": False, "errors_by_target": {}},
    )
    monkeypatch.setattr(
        mod,
        "prompt_concept_has_content",
        lambda _prompt_id: has_content["value"],
    )
    monkeypatch.setattr(
        mod,
        "_load_multilingual_concept_translation_prompt_seed_text",
        lambda: "Prompt body",
    )

    def _fake_upsert(**kwargs):
        writes.append(kwargs)
        has_content["value"] = True
        return {"success": True}

    monkeypatch.setattr(mod, "upsert_singleton_text_relation", _fake_upsert)

    report = mod._ensure_multilingual_concept_translation_prompt_support()

    assert report["success"] is True
    assert report["seeded_prompt_ids"] == [
        mod.MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID
    ]
    assert writes[0]["predicate"] == "hasContent"
    assert writes[0]["text"] == "Prompt body"
