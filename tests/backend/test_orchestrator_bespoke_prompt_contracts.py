from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services.prompt_template_service import _render_template
from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


class _StrictPromptTemplateService:
    def __init__(self, *, template: str, prompt_id: str) -> None:
        self._template = template
        self._prompt_id = prompt_id

    def render_prompt(
        self,
        concept_ids: Any,
        *,
        variables: Any = None,
        fallback: Any = None,
        max_chars: Any = None,
    ) -> Any:
        _ = (concept_ids, fallback, max_chars)
        rendered = _render_template(self._template, dict(variables or {}))
        return SimpleNamespace(
            prompt_id=self._prompt_id,
            text=rendered,
            variables=dict(variables or {}),
            truncated=False,
        )


def _build_orchestrator() -> InternalMCPChatOrchestrator:
    return InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))


def _build_request(*, data: dict[str, Any]) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id="test.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=SimpleNamespace(generate=lambda *args, **kwargs: "unused"),
            model="test-model",
        ),
        data=data,
        trace=None,
    )


def test_narration_prompt_selection_accepts_zero_variable_prompt_contract() -> None:
    orchestrator = _build_orchestrator()
    cast(Any, orchestrator)._prompt_templates = _StrictPromptTemplateService(
        template=(
            "Narration contract:\n"
            "Return a concise spoken-first prompt with no template variables."
        ),
        prompt_id="#V#prompt_turn_execution_narrate_completion_report",
    )

    result = orchestrator._action_narration_select_prompts(
        _build_request(
            data={
                "narration_prompt_ids": [
                    "#V#prompt_turn_execution_narrate_completion_report"
                ]
            }
        )
    )

    assert result.outputs["narration_prompts_resolved"] is True
    assert (
        result.outputs["narration_prompt_id"]
        == "#V#prompt_turn_execution_narrate_completion_report"
    )
    assert "Narration contract:" in result.outputs["narration_prompt_text"]


def test_buttonify_prompt_selection_supplies_expected_template_variables() -> None:
    orchestrator = _build_orchestrator()
    cast(Any, orchestrator)._prompt_templates = _StrictPromptTemplateService(
        template=(
            "Return ONLY a JSON array with up to 4 quick-reply strings.\n\n"
            "User message:\n{user_message}\n\n"
            "Assistant response:\n{assistant_response}"
        ),
        prompt_id="#V#buttonify_prompt_v1",
    )

    result = orchestrator._action_buttonify_select_prompt(
        _build_request(
            data={
                "buttonify_prompt_ids": ["#V#buttonify_prompt_v1"],
                "user_prompt": "Offer a couple of quick replies.",
                "screen_text": "Please reply with one of: \"Proceed\", \"Hold\".",
            }
        )
    )

    assert result.outputs["buttonify_prompt_available"] is True
    assert result.outputs["buttonify_prompt_error"] is None
    assert result.outputs["buttonify_prompt_id"] == "#V#buttonify_prompt_v1"
    assert "Offer a couple of quick replies." in result.outputs["buttonify_prompt_text"]
    assert 'Please reply with one of: "Proceed", "Hold".' in result.outputs[
        "buttonify_prompt_text"
    ]