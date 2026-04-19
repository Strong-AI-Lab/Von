from __future__ import annotations

from types import SimpleNamespace


def test_ensure_prompt_support_seeds_missing_prompt(monkeypatch) -> None:
    from src.backend.services import (
        file_copy_diagram_interpretation_vontology_service as service,
    )

    seeded: list[dict[str, object]] = []
    has_content_state = {"ready": False}
    monkeypatch.setattr(service, "_PROMPT_SUPPORT_READY", False)

    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        lambda **_: {
            "created_prompt_ids": [],
            "validated_prompt_ids": [],
            "missing_content_prompt_ids": [
                service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
            ],
            "linked_workflow_ids": [service.FILE_COPY_INTERPRETATION_WORKFLOW_ID],
            "errors_by_target": {
                service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID: (
                    "prompt_content_missing"
                )
            },
        },
    )
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda *_args, **_kwargs: has_content_state["ready"],
    )

    def _fake_upsert_singleton_text_relation(**kwargs):
        seeded.append(dict(kwargs))
        has_content_state["ready"] = True
        return {"success": True}

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )

    report = service.ensure_file_copy_diagram_interpretation_prompt_support()

    assert report["success"] is True
    assert seeded
    assert seeded[0]["subject_concept_id"] == (
        service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
    )


def test_infer_file_copy_diagram_semantics_parses_structured_response(
    monkeypatch,
) -> None:
    from src.backend.services import (
        file_copy_diagram_interpretation_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_file_copy_diagram_interpretation_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID,
            ),
            {
                "resolved_prompt_concept_id": (
                    service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
                ),
                "loaded_prompt_concept_id": (
                    service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
                ),
            },
        ),
    )

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return """{
              "schema_version": "file_copy_diagram_interpretation.v1",
              "organisation_candidates": [
                {
                  "name": "Te Whatu Ora",
                  "segment_refs": ["diagram_segment_1"],
                  "evidence_excerpt": "te whatu ora"
                }
              ],
              "relationship_candidates": [
                {
                  "source_name": "Te Whatu Ora",
                  "target_name": "University of Auckland",
                  "relation_hint": "association",
                  "segment_refs": ["diagram_segment_1"],
                  "evidence_excerpt": "te whatu ora -- university of auckland"
                }
              ]
            }"""

    payload, diagnostics = service.infer_file_copy_diagram_semantics(
        segments=[
            {
                "segment_id": "diagram_segment_1",
                "source_type": "diagram_segment",
                "text": "te whatu ora -- university of auckland",
            }
        ],
        llm_client=_LLM(),
        model=None,
    )

    assert diagnostics["status"] == "ok"
    assert payload["organisation_candidates"][0]["name"] == "Te Whatu Ora"
    assert payload["relationship_candidates"][0]["relation_hint"] == "association"


def test_infer_file_copy_diagram_semantics_fails_closed_on_parse_error(
    monkeypatch,
) -> None:
    from src.backend.services import (
        file_copy_diagram_interpretation_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_file_copy_diagram_interpretation_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID,
            ),
            {},
        ),
    )

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return "not valid json"

    payload, diagnostics = service.infer_file_copy_diagram_semantics(
        segments=[
            {
                "segment_id": "diagram_segment_1",
                "source_type": "diagram_segment",
                "text": "te whatu ora -- university of auckland",
            }
        ],
        llm_client=_LLM(),
        model=None,
    )

    assert diagnostics["status"] == "parse_failed"
    assert payload["organisation_candidates"] == []
    assert payload["relationship_candidates"] == []
