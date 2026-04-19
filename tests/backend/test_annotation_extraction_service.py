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
