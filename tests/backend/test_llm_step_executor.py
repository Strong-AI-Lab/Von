from __future__ import annotations

from unittest.mock import MagicMock

from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.llm_step_executor import execute_llm_step


def _build_request(*, llm_response: str) -> WorkflowActionRequest:
    llm_client = MagicMock()
    llm_client.generate.return_value = llm_response
    return WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={"invitation_text": "Please meet on Monday at 10am."},
        prompt_contract={
            "prompt_text": "Return JSON only for the meeting invitation.",
        },
        validation_policy={"output_format": "json_value"},
    )


def test_execute_llm_step_parses_json_value_output() -> None:
    request = _build_request(
        llm_response='{"meeting_type":"project_meeting","title":"Roadmap sync"}'
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {
        "meeting_type": "project_meeting",
        "title": "Roadmap sync",
    }
    assert result.outputs["validated_json_parse_mode"] in {
        "strict_json",
        "direct_json",
    }
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "success"
    assert envelope["validation"]["output_format"] == "json_value"


def test_execute_llm_step_fails_closed_when_json_value_is_invalid() -> None:
    request = _build_request(llm_response="This is not valid JSON.")

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert "json_parse_failed" in str(result.error or "")
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "failed"
    assert "json_parse_failed" in str(envelope["validation"]["reason"] or "")
