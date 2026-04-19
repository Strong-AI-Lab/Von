from __future__ import annotations

from types import SimpleNamespace


def test_ensure_prompt_support_seeds_missing_prompts(monkeypatch) -> None:
    from src.backend.services import (
        workflow_authoring_request_interpretation_vontology_service as service,
    )

    seeded: list[dict[str, object]] = []
    has_content_state = {
        service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID: False,
        service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID: False,
    }
    monkeypatch.setattr(service, "_PROMPT_SUPPORT_READY", False)

    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        lambda **_: {
            "created_prompt_ids": [],
            "validated_prompt_ids": [],
            "missing_content_prompt_ids": [
                service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
                service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
            ],
            "linked_workflow_ids": [service.WORKFLOW_CREATION_WORKFLOW_ID],
            "errors_by_target": {
                service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID: (
                    "prompt_content_missing"
                ),
                service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID: (
                    "prompt_content_missing"
                ),
            },
        },
    )
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda concept_id, *_args, **_kwargs: bool(has_content_state.get(concept_id)),
    )

    def _fake_upsert_singleton_text_relation(**kwargs):
        seeded.append(dict(kwargs))
        has_content_state[str(kwargs["subject_concept_id"])] = True
        return {"success": True}

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )

    report = service.ensure_workflow_authoring_request_interpretation_prompt_support()

    assert report["success"] is True
    assert {
        str(item["subject_concept_id"]) for item in seeded
    } == {
        service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
        service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
    }


def test_infer_workflow_authoring_identity_parses_structured_response(
    monkeypatch,
) -> None:
    from src.backend.services import (
        workflow_authoring_request_interpretation_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_workflow_authoring_identity_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
            ),
            {
                "resolved_prompt_concept_id": (
                    service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID
                ),
                "loaded_prompt_concept_id": (
                    service.WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID
                ),
            },
        ),
    )

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return """{
              "schema_version": "workflow_authoring_identity_inference.v1",
              "target_workflow_name": "Scholarly Paper Representation Workflow",
              "target_workflow_id": null,
              "workflow_description": "Represent scholarly papers from uploaded files."
            }"""

    payload, diagnostics = service.infer_workflow_authoring_identity(
        request_text="Create a workflow to represent scholarly papers from uploaded files.",
        llm_client=_LLM(),
        model=None,
    )

    assert diagnostics["status"] == "ok"
    assert payload["target_workflow_name"] == (
        "Scholarly Paper Representation Workflow"
    )
    assert payload["target_workflow_id"] is None


def test_infer_workflow_authoring_profile_parses_structured_response(
    monkeypatch,
) -> None:
    from src.backend.services import (
        workflow_authoring_request_interpretation_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_workflow_authoring_profile_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
            ),
            {
                "resolved_prompt_concept_id": (
                    service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID
                ),
                "loaded_prompt_concept_id": (
                    service.WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID
                ),
            },
        ),
    )

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return """{
              "schema_version": "workflow_authoring_profile_interpretation.v1",
              "student_name": "Alex Example",
              "supervisor_names": ["Grace Hopper"],
              "research_topic": "Neuro-Symbolic Systems",
              "institution": "University of Auckland"
            }"""

    payload, diagnostics = service.infer_workflow_authoring_profile(
        source_text=(
            "Doctorando: Alex Example; Supervisora: Grace Hopper; "
            "Tema de investigación: Neuro-Symbolic Systems."
        ),
        llm_client=_LLM(),
        model=None,
    )

    assert diagnostics["status"] == "ok"
    assert payload["student_name"] == "Alex Example"
    assert payload["supervisor_names"] == ["Grace Hopper"]
    assert payload["research_topic"] == "Neuro-Symbolic Systems"
