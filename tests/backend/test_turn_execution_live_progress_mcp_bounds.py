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
    assert result["section_counts"]["diagnostic_events"] == 30
    assert result["detail_access"]["arguments"]["section"] == "<one of available_sections>"
    assert len(json.dumps(result, indent=2, default=str)) < 20_000


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
