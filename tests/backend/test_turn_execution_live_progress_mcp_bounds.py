from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _large_serialised_progress_payload() -> dict[str, Any]:
    return {
        "request_id": "req-large-live-progress",
        "status": "workflow_step_complete",
        "stage": "selected_workflow_execution",
        "goal_label": "Represent paper",
        "subtask": "workflow execution",
        "elapsed_ms": 760274,
        "liveness_state": "active",
        "liveness_reason": "recent_activity",
        "stall_detected": False,
        "activity_idle_ms": 250,
        "stage_idle_ms": 125,
        "diagnostic_summary": {"event_count": 500},
        "counters": {
            "tokens_streamed": 42,
            "tools_started": 3,
            "tools_completed": 2,
        },
        "diagnostic_events": [
            {
                "status": "llm_request_prepared",
                "stage": "workflow_routing",
                "llm_request": {"prompt": {"text": "x" * 4000}},
                "result_summary": f"event {index}",
            }
            for index in range(30)
        ],
        "stage_diagnostics": [
            {
                "stage_id": "workflow_routing",
                "stage_label": "Workflow routing",
                "event_count": 30,
                "latest_result_summary": "large stage detail",
                "llm_interactions": [{"prompt": "y" * 4000}],
            }
        ],
        "llm_request": {"prompt": {"text": "z" * 12000}},
        "selected_workflow_execution": {
            "schema_version": "selected_workflow_execution.v1",
            "selected_workflow_id": "#V#paper_representation_workflow",
            "selected_workflow_name": "Paper representation workflow",
            "events": [{"payload": "s" * 2000} for _ in range(5)],
        },
        "workflow_stage_path": {
            "schema_version": "conversation_turn_stage_path.v1",
            "path": [
                {"stage_id": "workflow_routing"},
                {"stage_id": "selected_workflow_execution"},
            ],
        },
        "progress_events": [
            {"stage": "workflow_routing", "status": "phase_transition"},
            {"stage": "selected_workflow_execution", "status": "workflow_step_complete"},
        ],
        "activity_history": [
            {"stage": "workflow_routing", "state": "complete"},
            {"stage": "selected_workflow_execution", "state": "active"},
        ],
        "turn_timing_trace": {
            "schema_version": "turn_timing_trace.v1",
            "span_count": 12,
            "stored_span_count": 12,
            "dropped_span_count": 0,
            "summary": {
                "elapsed_ms": 760274,
                "phase_elapsed_ms": 700000,
                "operation_elapsed_ms": 60274,
                "llm_elapsed_ms": 42000,
                "tool_elapsed_ms": 12000,
            },
            "slowest_spans": [
                {
                    "span_id": "slow-1",
                    "stage_id": "workflow_routing",
                    "operation_kind": "llm_call",
                    "operation_name": "gpt-5-mini",
                    "duration_ms": 42000,
                }
            ],
            "model_prompt_summary": [
                {
                    "stage_id": "workflow_routing",
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "prompt_id": "#V#workflow_selector_prompt",
                    "call_count": 1,
                    "duration_ms": 42000,
                }
            ],
            "operation_totals": [
                {
                    "operation_kind": "llm_call",
                    "operation_name": "gpt-5-mini",
                    "span_count": 1,
                    "duration_ms": 42000,
                }
            ],
            "spans": [
                {
                    "span_id": f"timing-span-{index}",
                    "stage_id": "workflow_routing",
                    "operation_kind": "llm_call",
                    "operation_name": "gpt-5-mini",
                    "duration_ms": 1000 + index,
                }
                for index in range(12)
            ],
        },
        "timing_spans": [
            {
                "span_id": f"timing-span-{index}",
                "stage_id": "workflow_routing",
                "operation_kind": "llm_call",
                "operation_name": "gpt-5-mini",
                "duration_ms": 1000 + index,
            }
            for index in range(12)
        ],
    }


def _install_live_progress_stubs(monkeypatch, payload: dict[str, Any]) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes,
        "_resolve_tool_progress_state_from_scope_candidates",
        lambda **kwargs: ({"request_id": kwargs["request_id"]}, "user:#V#tester"),
    )
    monkeypatch.setattr(
        von_routes,
        "_serialise_tool_progress_state",
        lambda state: dict(payload),
    )


def test_live_progress_default_projection_is_bounded(monkeypatch) -> None:
    from src.backend.services.turn_execution_live_progress_service import (
        get_turn_execution_live_progress_payload,
    )

    _install_live_progress_stubs(monkeypatch, _large_serialised_progress_payload())

    result = get_turn_execution_live_progress_payload(
        request_id="req-large-live-progress",
        namespace="#V#tester@org",
    )

    assert result is not None
    assert result["success"] is True
    assert result["projection"] == "bounded_snapshot"
    assert result["status"] == "workflow_step_complete"
    assert "diagnostic_events" not in result
    assert "stage_diagnostics" not in result
    assert "llm_request" not in result
    assert "turn_timing_trace" not in result
    assert "timing_spans" not in result
    assert result["timing_summary"]["span_count"] == 12
    assert result["timing_summary"]["slowest_spans"][0]["duration_ms"] == 42000
    assert result["section_counts"]["diagnostic_events"] == 30
    assert result["section_counts"]["timing_spans"] == 12
    assert result["detail_access"]["arguments"]["section"] == "<one of available_sections>"
    assert len(json.dumps(result, indent=2, default=str)) < 20_000


def test_live_progress_surfaces_terminal_receipt_from_critic_stage(monkeypatch) -> None:
    from src.backend.services.turn_execution_live_progress_service import (
        get_turn_execution_live_progress_payload,
    )

    payload = _large_serialised_progress_payload()
    payload["stage_diagnostics"].append(
        {
            "stage_id": "postcondition_critic",
            "critic_verdict": {
                "terminal_outcome_receipt": {
                    "schema_version": "terminal_outcome_receipt.v1",
                    "profile_concept_id": "#V#terminal_outcome_receipt",
                    "outcome": "input_required",
                    "cause_code": "required_input_missing",
                    "causal_stage": "planning",
                    "summary": "A required represented input was not available.",
                    "evidence_refs": [{"source": "critic", "ref": "input-1"}],
                    "committed_effects": [],
                    "remaining_obligations": [{"effect_id": "effect-1"}],
                    "retryability": "after_input",
                    "recovery_affordances": [
                        {"action_type": "elicit_input"}
                    ],
                    "learning_candidate": None,
                    "redaction_status": "safe_projection",
                    "provenance": {"decision_source": "represented_llm"},
                }
            },
        }
    )
    _install_live_progress_stubs(monkeypatch, payload)

    result = get_turn_execution_live_progress_payload(
        request_id="req-large-live-progress",
        namespace="#V#tester@org",
    )

    assert result is not None
    projection = result["terminal_outcome_receipt_projection"]
    assert projection["available"] is True
    assert projection["outcome"] == "input_required"
    assert projection["retryability"] == "after_input"
    assert projection["source"] == (
        "live_progress.stage_diagnostics.critic_verdict"
    )


def test_live_progress_section_projection_paginates_large_details(monkeypatch) -> None:
    from src.backend.services.turn_execution_live_progress_service import (
        get_turn_execution_live_progress_payload,
    )

    _install_live_progress_stubs(monkeypatch, _large_serialised_progress_payload())

    result = get_turn_execution_live_progress_payload(
        request_id="req-large-live-progress",
        namespace="#V#tester@org",
        section="diagnostic_events",
        limit=3,
        offset=4,
    )

    assert result is not None
    assert result["projection"] == "section"
    assert result["section"] == "diagnostic_events"
    assert result["total"] == 30
    assert result["returned"] == 3
    assert result["next_offset"] == 7
    assert [item["result_summary"] for item in result["items"]] == [
        "event 4",
        "event 5",
        "event 6",
    ]


def test_live_progress_timing_span_section_projection_paginates(monkeypatch) -> None:
    from src.backend.services.turn_execution_live_progress_service import (
        get_turn_execution_live_progress_payload,
    )

    _install_live_progress_stubs(monkeypatch, _large_serialised_progress_payload())

    result = get_turn_execution_live_progress_payload(
        request_id="req-large-live-progress",
        namespace="#V#tester@org",
        section="timing_spans",
        limit=4,
        offset=8,
    )

    assert result is not None
    assert result["projection"] == "section"
    assert result["section"] == "timing_spans"
    assert result["total"] == 12
    assert result["returned"] == 4
    assert result["next_offset"] is None
    assert [item["span_id"] for item in result["items"]] == [
        "timing-span-8",
        "timing-span-9",
        "timing-span-10",
        "timing-span-11",
    ]


def test_stdio_json_text_returns_payload_too_large_guard(monkeypatch) -> None:
    from src.backend.mcp_server import mcp_stdio_server

    monkeypatch.setenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS", "10000")

    content = mcp_stdio_server._json_text({"items": ["x" * 1000 for _ in range(20)]})
    payload = json.loads(content.text)

    assert payload["success"] is False
    assert payload["error_code"] == "payload_too_large"
    assert payload["error_details"]["approximate_response_chars"] > 10_000
    assert payload["error_details"]["response_guard"] == "vontology_stdio_text_content"


def test_stdio_small_vontology_read_then_second_call_same_process() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    server_path = repo_root / "src" / "backend" / "mcp_server" / "mcp_stdio_server.py"
    python_path = repo_root / ".venv" / "bin" / "python"
    command = str(python_path if python_path.exists() else Path(sys.executable))
    env = dict(os.environ)
    env.setdefault("VON_MCP_ALLOW_WRITES", "0")
    env.pop("VON_MCP_HELPER_OWNER_TOKEN", None)
    env["VON_MCP_HELPER_HEARTBEAT_SEC"] = "0"

    async def _run_probe(registry_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
        probe_env = dict(env)
        probe_env["VON_MCP_HELPER_REGISTRY_DIR"] = registry_dir
        params = StdioServerParameters(
            command=command,
            args=[str(server_path)],
            cwd=str(repo_root),
            env=probe_env,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                first = await session.call_tool(
                    "get_text_relations",
                    {
                        "concept_id": "#V#small_stdio_transport_probe",
                        "limit": 1,
                    },
                )
                second = await session.call_tool(
                    "coding_agent_mcp_access_profile",
                    {},
                )
        return _decode_single_text(first), _decode_single_text(second)

    with tempfile.TemporaryDirectory() as registry_dir:
        first_payload, second_payload = asyncio.run(_run_probe(registry_dir))

    assert isinstance(first_payload, dict)
    assert first_payload.get("error_code") != "payload_too_large"
    assert second_payload["success"] is True
    assert second_payload["profile_id"] == "coding_agent_vontology_mcp_access"


def _decode_single_text(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", None)
    assert isinstance(content, list) and content
    text = getattr(content[0], "text", "")
    assert isinstance(text, str) and text
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload
