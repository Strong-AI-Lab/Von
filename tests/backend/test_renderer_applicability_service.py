from src.backend.services.renderer_applicability_service import (
    OBJECT_KIND_TRANSIENT_MICROTHEORY,
    RendererProfile,
    RendererResolutionInput,
    resolve_renderer_applicability,
    resolve_renderer_applicability_from_metadata,
)


def test_resolver_selects_highest_priority_concept_renderer() -> None:
    profiles = (
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#timeline_renderer",
                "renderer_type": "timeline",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
                "applies_to_concept_type_ids": ["#V#task"],
                "required_predicates": ["#V#has_start_time"],
                "priority": 90,
            }
        ),
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#table_renderer",
                "renderer_type": "table",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
                "applies_to_concept_type_ids": ["#V#task"],
                "required_predicates": ["#V#has_due_time"],
                "priority": 60,
            }
        ),
    )
    request = RendererResolutionInput.from_mapping(
        {
            "object_kind": "concept",
            "concept_id": "#V#task_123",
            "concept_type_ids": ["#V#task"],
            "present_predicates": ["#V#has_start_time", "#V#has_due_time"],
        }
    )

    result = resolve_renderer_applicability(
        renderer_profiles=profiles,
        request=request,
    )

    assert result.interpreted_object_kind == "concept"
    assert not result.interpreted_as_transient_microtheory
    assert len(result.selected_renderers) == 1
    assert result.selected_renderers[0].renderer_id == "#V#timeline_renderer"
    assert all(item.applicable for item in result.candidate_evaluations)


def test_resolver_interprets_untyped_transient_payload_as_microtheory() -> None:
    result = resolve_renderer_applicability_from_metadata(
        renderer_definitions=[
            {
                "renderer_id": "#V#transient_table_renderer",
                "renderer_type": "table",
                "modalities": ["visual"],
                "applies_to_object_kinds": [OBJECT_KIND_TRANSIENT_MICROTHEORY],
                "required_context_tags": ["planning"],
                "priority": 70,
            }
        ],
        request_payload={
            "context_tags": ["planning"],
            "transient_microtheory": {
                "context_scope": "#V#turn_scope",
                "assertions": [{"predicate": "#V#has_due_time"}],
                "provenance": {"source": "agent_turn", "episode_id": "#V#episode_9"},
            },
        },
    )

    assert result.interpreted_object_kind == OBJECT_KIND_TRANSIENT_MICROTHEORY
    assert result.interpreted_as_transient_microtheory is True
    assert len(result.selected_renderers) == 1
    assert result.selected_renderers[0].renderer_id == "#V#transient_table_renderer"
    assert result.diagnostics["transient_interpretation"]["episode_id"] == "#V#episode_9"
    assert (
        result.diagnostics["transient_interpretation"]["provenance"]["source"]
        == "agent_turn"
    )


def test_resolver_supports_multimodal_selection_with_fallbacks() -> None:
    profiles = (
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#table_visual_renderer",
                "renderer_type": "table",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
                "priority": 100,
                "fallback_renderer_ids": ["#V#chart_visual_renderer"],
            }
        ),
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#narration_renderer",
                "renderer_type": "narration",
                "modalities": ["narrated_audio", "textual"],
                "applies_to_object_kinds": ["concept"],
                "priority": 90,
                "fallback_renderer_ids": ["#V#text_summary_renderer"],
            }
        ),
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#text_summary_renderer",
                "renderer_type": "text_summary",
                "modalities": ["textual"],
                "applies_to_object_kinds": ["concept"],
                "priority": 50,
            }
        ),
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#chart_visual_renderer",
                "renderer_type": "chart",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
                "priority": 40,
            }
        ),
    )
    request = RendererResolutionInput.from_mapping(
        {
            "object_kind": "concept",
            "preferred_modalities": ["visual", "narrated_audio"],
        }
    )

    result = resolve_renderer_applicability(
        renderer_profiles=profiles,
        request=request,
        allow_multimodal=True,
    )

    selected_ids = [item.renderer_id for item in result.selected_renderers]
    assert selected_ids == ["#V#table_visual_renderer", "#V#narration_renderer"]
    assert result.selected_renderers[0].fallback_renderer_ids == ("#V#chart_visual_renderer",)
    assert result.selected_renderers[1].fallback_renderer_ids == ("#V#text_summary_renderer",)


def test_resolver_exposes_rejection_reasons_when_no_renderer_applies() -> None:
    profiles = (
        RendererProfile.from_mapping(
            {
                "renderer_id": "#V#high_confidence_renderer",
                "renderer_type": "timeline",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
                "minimum_confidence": 0.95,
                "priority": 99,
            }
        ),
    )
    request = RendererResolutionInput.from_mapping(
        {
            "object_kind": "concept",
            "confidence": 0.2,
        }
    )

    result = resolve_renderer_applicability(
        renderer_profiles=profiles,
        request=request,
    )

    assert result.selected_renderers == ()
    assert len(result.candidate_evaluations) == 1
    assert result.candidate_evaluations[0].applicable is False
    assert "confidence_below_threshold" in set(
        result.candidate_evaluations[0].rejection_reasons
    )
    assert result.diagnostics["selection_rationale"] == "No applicable renderer profile found"

