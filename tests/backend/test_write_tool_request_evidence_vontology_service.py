from __future__ import annotations

from types import SimpleNamespace


def test_ensure_prompt_support_seeds_missing_prompt(monkeypatch):
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
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
                service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
            ],
            "linked_workflow_ids": [service.WRITE_TOOL_POLICY_WORKFLOW_ID],
            "errors_by_target": {
                service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID: (
                    "prompt_content_missing"
                )
            },
        },
    )
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda *_a, **_kw: has_content_state["ready"],
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

    report = service.ensure_write_tool_request_evidence_prompt_support()

    assert report["success"] is True
    assert seeded
    assert seeded[0]["subject_concept_id"] == (
        service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
    )
    assert "external-system side effects" in seeded[0]["text"]
    assert "gmail_send_message" in seeded[0]["text"]
    assert "workflow_execute" in seeded[0]["text"]
    assert "represent, materialise" in seeded[0]["text"]


def test_infer_write_tool_request_evidence_parses_structured_response(monkeypatch):
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_write_tool_request_evidence_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
            ),
            {
                "resolved_prompt_concept_id": (
                    service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
                ),
                "loaded_prompt_concept_id": (
                    service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
                ),
            },
        ),
    )
    monkeypatch.setattr(service, "_PROMPT_SUPPORT_READY", False)

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return """{
              "schema_version": "write_tool_request_evidence.v1",
              "tool_evidence": [
                {
                  "tool_name": "update_concept",
                  "request_state": "explicit_request",
                  "confirmation_state": "low_confidence",
                                    "denial_state": "low_confidence",
                  "rationale": "The current prompt explicitly asks for an update."
                }
              ]
            }"""

    evidence, diagnostics = service.infer_write_tool_request_evidence(
        llm_client=_LLM(),
        model=None,
        prompt="Update the concept description.",
        recent_user_prompts=[],
        requested_tools=["update_concept"],
        requested_tool_payloads={},
    )

    assert diagnostics["status"] == "ok"
    assert evidence["update_concept"]["request_state"] == "explicit_request"
    assert evidence["update_concept"]["confirmation_state"] == "low_confidence"
    assert evidence["update_concept"]["denial_state"] == "low_confidence"


def test_infer_write_tool_request_evidence_parses_denial_state(monkeypatch):
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_write_tool_request_evidence_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
            ),
            {},
        ),
    )
    monkeypatch.setattr(service, "_PROMPT_SUPPORT_READY", False)

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return """{
              \"schema_version\": \"write_tool_request_evidence.v1\",
              \"tool_evidence\": [
                {
                  \"tool_name\": \"download_paper\",
                  \"request_state\": \"low_confidence\",
                  \"confirmation_state\": \"low_confidence\",
                  \"denial_state\": \"explicit_denial\",
                  \"rationale\": \"The prompt explicitly says not to download or store the paper.\"
                }
              ]
            }"""

    evidence, diagnostics = service.infer_write_tool_request_evidence(
        llm_client=_LLM(),
        model=None,
        prompt="Do not download or store this paper.",
        recent_user_prompts=[],
        requested_tools=["download_paper"],
        requested_tool_payloads={},
    )

    assert diagnostics["status"] == "ok"
    assert evidence["download_paper"]["denial_state"] == "explicit_denial"


def test_ensure_prompt_support_reseeds_legacy_prompt_contract(monkeypatch):
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    seeded: list[dict[str, object]] = []
    monkeypatch.setattr(service, "_PROMPT_SUPPORT_READY", False)
    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        lambda **_: {
            "created_prompt_ids": [],
            "validated_prompt_ids": [
                service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
            ],
            "missing_content_prompt_ids": [],
            "linked_workflow_ids": [service.WRITE_TOOL_POLICY_WORKFLOW_ID],
            "errors_by_target": {},
        },
    )
    monkeypatch.setattr(service, "prompt_concept_has_content", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        service,
        "_write_tool_request_evidence_prompt_seed_needs_refresh",
        lambda: True,
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: seeded.append(dict(kwargs)) or {"success": True},
    )

    report = service.ensure_write_tool_request_evidence_prompt_support()

    assert report["success"] is True
    assert seeded
    assert seeded[0]["subject_concept_id"] == (
        service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
    )


def test_prompt_refresh_detects_missing_workflow_execute_guidance(monkeypatch):
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda **_: [
            {
                "predicate": "hasContent",
                "text": (
                    "denial_state\nexternal-system side effects\n"
                    "gmail_send_message"
                ),
            }
        ],
    )

    assert service._write_tool_request_evidence_prompt_seed_needs_refresh() is True


def test_turn_contract_evidence_allows_exact_workflow_execute_target():
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    evidence, diagnostics = (
        service.augment_write_tool_request_evidence_with_turn_contract(
            request_evidence={
                "workflow_execute": {
                    "request_state": "low_confidence",
                    "confirmation_state": "low_confidence",
                    "denial_state": "low_confidence",
                    "rationale": "LLM was unsure.",
                }
            },
            requested_tools=["workflow_execute"],
            requested_tool_payloads={
                "workflow_execute": {
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                }
            },
            turn_expected_outcome_contract={
                "required_tools": ["gmail_get_message"],
                "workflow_concept_ids": [
                    "#V#arxiv_paper_representation_workflow"
                ],
            },
            activated_conditional_required_tools=["workflow_execute"],
        )
    )

    assert diagnostics["status"] == "contract_evidence_applied"
    assert diagnostics["applied_tools"] == ["workflow_execute"]
    assert evidence["workflow_execute"]["request_state"] == "explicit_request"
    assert evidence["workflow_execute"]["confirmation_state"] == "low_confidence"
    assert evidence["workflow_execute"]["denial_state"] == "low_confidence"
    assert (
        "#V#arxiv_paper_representation_workflow"
        in evidence["workflow_execute"]["rationale"]
    )


def test_turn_contract_evidence_requires_exact_workflow_execute_target():
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    evidence, diagnostics = (
        service.augment_write_tool_request_evidence_with_turn_contract(
            request_evidence={
                "workflow_execute": {
                    "request_state": "low_confidence",
                    "confirmation_state": "low_confidence",
                    "denial_state": "low_confidence",
                    "rationale": "LLM was unsure.",
                }
            },
            requested_tools=["workflow_execute"],
            requested_tool_payloads={
                "workflow_execute": {
                    "workflow_id": "#V#unrelated_workflow",
                }
            },
            turn_expected_outcome_contract={
                "required_tools": ["workflow_execute"],
                "workflow_concept_ids": [
                    "#V#arxiv_paper_representation_workflow"
                ],
            },
            activated_conditional_required_tools=[],
        )
    )

    assert diagnostics["status"] == "no_contract_evidence_applied"
    assert evidence["workflow_execute"]["request_state"] == "low_confidence"


def test_turn_contract_evidence_does_not_override_explicit_denial():
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    evidence, diagnostics = (
        service.augment_write_tool_request_evidence_with_turn_contract(
            request_evidence={
                "workflow_execute": {
                    "request_state": "low_confidence",
                    "confirmation_state": "low_confidence",
                    "denial_state": "explicit_denial",
                    "rationale": "The user explicitly denied the write.",
                }
            },
            requested_tools=["workflow_execute"],
            requested_tool_payloads={
                "workflow_execute": {
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                }
            },
            turn_expected_outcome_contract={
                "required_tools": ["workflow_execute"],
                "workflow_concept_ids": [
                    "#V#arxiv_paper_representation_workflow"
                ],
            },
            activated_conditional_required_tools=[],
        )
    )

    assert diagnostics["status"] == "no_contract_evidence_applied"
    assert evidence["workflow_execute"]["request_state"] == "low_confidence"
    assert evidence["workflow_execute"]["denial_state"] == "explicit_denial"


def test_infer_write_tool_request_evidence_fails_closed_on_parse_error(monkeypatch):
    from src.backend.services import (
        write_tool_request_evidence_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "render_write_tool_request_evidence_prompt",
        lambda **_: (
            SimpleNamespace(
                text="rendered prompt",
                prompt_id=service.WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
            ),
            {},
        ),
    )
    monkeypatch.setattr(service, "_PROMPT_SUPPORT_READY", False)

    class _LLM:
        def generate(self, prompt, context=None, model=None):
            return "not valid json"

    evidence, diagnostics = service.infer_write_tool_request_evidence(
        llm_client=_LLM(),
        model=None,
        prompt="Summarise the Jira issue state.",
        recent_user_prompts=[],
        requested_tools=["jira_update_issue"],
        requested_tool_payloads={},
    )

    assert diagnostics["status"] == "parse_failed"
    assert evidence["jira_update_issue"]["request_state"] == "low_confidence"
    assert evidence["jira_update_issue"]["confirmation_state"] == "low_confidence"
    assert evidence["jira_update_issue"]["denial_state"] == "low_confidence"
