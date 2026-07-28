from __future__ import annotations

from typing import Any, cast


def test_llm_generate_spans_structural_list_fallback_is_language_neutral(
    monkeypatch,
) -> None:
    from src.backend.services import annotation_extraction_service as service

    monkeypatch.setattr(service, "_get_llm_prompt_instruction", lambda: "Extract spans.")

    class _LLM:
        def generate(self, prompt):
            return "Elementos:\n- Juan Perez\n- Universidad de Auckland"

    monkeypatch.setattr(service, "get_llm_client", lambda *_args, **_kwargs: _LLM())

    spans = service.llm_generate_spans(
        "Juan Perez estudia en la Universidad de Auckland.",
        max_spans=10,
    )

    assert [span["text"] for span in spans] == [
        "Juan Perez",
        "Universidad de Auckland",
    ]
    assert all(span.get("source") == ["llm", "fallback"] for span in spans)
    metric = service.fallback_nonjson_metric_stats()
    assert "last_preview" not in metric
    assert "last_response_preview" not in metric


def test_extract_annotations_does_not_infer_type_from_capitalisation(
    monkeypatch,
) -> None:
    from src.backend.services import annotation_extraction_service as service

    monkeypatch.setattr(service, "_get_llm_prompt_instruction", lambda: "Extract spans.")

    class _LLM:
        def generate(self, prompt):
            return (
                '{"spans":[{"text":"John Smith","start":0,"end":10,"type":null}]}'
            )

    monkeypatch.setattr(service, "get_llm_client", lambda *_args, **_kwargs: _LLM())
    monkeypatch.setattr(
        service.concept_service,
        "suggest_concepts_for_text",
        lambda *_args, **_kwargs: [],
    )

    annotations = service.extract_annotations(
        "John Smith wrote this paper.",
        use_llm=True,
        use_match=False,
    )

    assert annotations
    first = cast(dict[str, Any], annotations[0])
    first_span = cast(dict[str, Any], first["span"])
    assert first_span["text"] == "John Smith"
    assert "type" not in first_span
    assert "suggested_type_id" not in first


def test_phrase_cache_does_not_reuse_private_names_across_actor_scopes(
    monkeypatch,
) -> None:
    from src.backend.security.access_control import (
        get_effective_user_concept_id,
        override_current_actor,
    )
    from src.backend.services import annotation_extraction_service as service

    private_phrase = "Secret Quasar Project"

    def visible_concepts(*_args, **_kwargs):
        if get_effective_user_concept_id() == "#V#actor_b":
            return [{"name": private_phrase, "names": []}]
        return []

    monkeypatch.setattr(service.ConceptsRepository, "find", visible_concepts)
    monkeypatch.setattr(
        service.concept_service,
        "suggest_concepts_for_text",
        lambda *_args, **_kwargs: [],
    )
    service.invalidate_phrase_cache()

    with override_current_actor("#V#actor_b"):
        actor_b_annotations = service.extract_annotations(
            private_phrase,
            use_llm=False,
        )
    with override_current_actor("#V#actor_a"):
        actor_a_annotations = service.extract_annotations(
            private_phrase,
            use_llm=False,
        )

    assert [item["span"]["text"] for item in actor_b_annotations] == [
        private_phrase
    ]
    assert actor_a_annotations == []
    service.invalidate_phrase_cache()


def test_last_annotation_llm_io_is_scoped_to_authenticated_actor(
    monkeypatch,
) -> None:
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import annotation_extraction_service as service

    monkeypatch.setattr(service, "_get_llm_prompt_instruction", lambda: "Extract.")

    class _LLM:
        def generate(self, prompt):
            return '{"spans":[]}'

    monkeypatch.setattr(service, "get_llm_client", lambda *_args, **_kwargs: _LLM())
    service._LAST_LLM_IO_BY_ACTOR.clear()

    with override_current_actor("#V#actor_b"):
        service.llm_generate_spans("Secret project notes")
        actor_b_io = service.get_last_llm_io()
    with override_current_actor("#V#actor_a"):
        actor_a_io = service.get_last_llm_io()
    anonymous_io = service.get_last_llm_io()

    assert "Secret project notes" in actor_b_io["prompt"]
    assert actor_a_io == {}
    assert anonymous_io == {}
    service._LAST_LLM_IO_BY_ACTOR.clear()
