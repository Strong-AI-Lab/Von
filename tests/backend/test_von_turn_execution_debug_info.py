from __future__ import annotations

from typing import Any


def test_finalise_llm_debug_info_passes_supervised_structured_outputs_into_turn_record(
    monkeypatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    captured: dict[str, Any] = {}

    def _fake_build_turn_execution_record(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "workflow_routing_diagnostics": {"selected_workflow_id": "#V#chat_assistant_workflow"},
            "execution": {
                "selected_workflow_trace": kwargs.get("selected_workflow_trace"),
            },
            "critic": {"verdict": kwargs.get("critic_verdict")},
            "completion_gate_verdict": kwargs.get("completion_gate_verdict"),
            "completion_report": kwargs.get("completion_report"),
        }

    monkeypatch.setattr(von_routes, "build_turn_execution_record", _fake_build_turn_execution_record)
    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )

    llm_debug_info = {
        "request_id": "req-structured-debug",
        "interaction_timestamp_utc": "2026-04-08T00:00:00+00:00",
        "response": "Workflow monitor response",
        "tool_invocations": [],
        "search_evidence": [],
        "turn_execution_diagnostics": {},
        "aux_llm_calls": [],
        "selected_workflow_trace": {
            "workflow_id": "#V#chat_assistant_workflow",
            "execution_mode": "direct_response",
        },
        "critic_verdict": {"verdict": "pass"},
        "completion_gate_verdict": {"decision": "completed"},
        "completion_report": {
            "schema_version": "conversation_turn_selected_workflow_result.v1",
            "workflow_id": "#V#chat_assistant_workflow",
        },
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text="Show the workflow monitor row for this turn.",
        response_text="Workflow monitor response",
        session_id="session-structured-debug",
        namespace="#V#michael_witbrock@sail_lab",
        user_id="#V#michael_witbrock",
        org_id="#V#sail_lab",
        workflow_discovery={"selected_workflow_id": "#V#chat_assistant_workflow"},
        workflow_routing={"workflow_id": "#V#chat_assistant_workflow"},
    )

    assert captured["selected_workflow_trace"] == {
        "workflow_id": "#V#chat_assistant_workflow",
        "execution_mode": "direct_response",
    }
    assert captured["critic_verdict"] == {"verdict": "pass"}
    assert captured["completion_gate_verdict"] == {"decision": "completed"}
    assert captured["completion_report"] == {
        "schema_version": "conversation_turn_selected_workflow_result.v1",
        "workflow_id": "#V#chat_assistant_workflow",
    }
    turn_execution_record = result["turn_execution_record"]
    assert turn_execution_record["execution"]["selected_workflow_trace"] == captured[
        "selected_workflow_trace"
    ]
    assert turn_execution_record["critic"]["verdict"] == {"verdict": "pass"}
    assert turn_execution_record["completion_gate_verdict"] == {
        "decision": "completed"
    }
