import asyncio
import json
from pathlib import Path
from typing import Any, cast

WORKFLOW_TOOL_NAMES = {
    "workflow_list_definitions",
    "workflow_validate_candidate",
    "workflow_list_use_episodes",
    "workflow_bind_event",
    "workflow_list_event_bindings",
    "workflow_set_event_binding_enabled",
    "workflow_delete_event_binding",
    "workflow_mcp_health_check",
    "workflow_concept_parity_audit",
    "workflow_materialisation_diagnostics",
    "workflow_create_instance",
    "workflow_execute",
    "workflow_list_instances",
    "workflow_list_execution_traces",
    "workflow_build_prediction_envelope",
    "workflow_get_instance",
    "workflow_get_execution_trace",
    "workflow_cancel_instance",
    "workflow_resume_instance",
    "workflow_retry_instance",
    "workflow_create_schedule",
    "workflow_list_schedules",
    "workflow_get_schedule",
    "workflow_set_schedule_enabled",
    "workflow_delete_schedule",
    "workflow_trigger_schedule",
    "testing_theory_create_slice",
    "testing_theory_import_canonical_context",
    "testing_theory_assert_local_claims",
    "testing_theory_compute_diff",
    "testing_theory_rollback_local_writes",
    "testing_theory_promote_validated_claims",
    "testing_theory_gc_expired",
    "experiment_create_spec",
    "experiment_start_run",
    "experiment_record_observation",
    "experiment_compute_verdict",
    "experiment_emit_learning_signal",
    "experiment_execute_target_workflow",
    "experiment_execute_regression_suite",
    "experiment_run_list",
    "experiment_run_get",
    "episode_critique_build_benchmark",
    "episode_critique_memory_list",
    "episode_critique_memory_get",
    "context_bundle_resolve_effective_context",
    "context_bundle_assemble_context_dossier",
    "context_bundle_update_report_revision",
    "context_bundle_build_reconstructed_workspace",
    "context_bundle_build_benchmark",
    "repo_dossier_file_snapshot",
    "repo_dossier_search",
    "repo_dossier_workflow_definition_get",
    "repo_dossier_prompt_definition_get",
    "repo_dossier_git_metadata",
    "testing_prepare_experiment_spec",
    "testing_prepare_meeting_invitation_spec",
    "testing_prepare_arxiv_paper_ingestion_fixture",
    "testing_verify_arxiv_paper_ingestion_result",
    "testing_cleanup_arxiv_paper_ingestion_artifacts",
}


def test_mcp_stdio_server_has_workflow_tool_handlers():
    from src.backend.mcp_server import mcp_stdio_server

    for tool_name in WORKFLOW_TOOL_NAMES:
        assert tool_name in mcp_stdio_server._TOOL_HANDLERS


def test_vontology_mcp_manifest_includes_workflow_tools():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "backend"
        / "mcp_server"
        / "vontology_mcp.json"
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    tools = data.get("tools") or []
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    missing = WORKFLOW_TOOL_NAMES - names
    assert not missing, f"Manifest missing workflow tools: {sorted(missing)}"


def test_workflow_materialisation_diagnostics_stdio_call_path(monkeypatch):
    from src.backend.mcp_server import mcp_stdio_server
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "build_workflow_materialisation_diagnostics",
        lambda **kwargs: calls.append(dict(kwargs)) or {
            "success": True,
            "classification": {"state": "healthy"},
            "required_concept_ids": kwargs.get("required_concept_ids") or [],
        },
    )

    async def _runner():
        result = await mcp_stdio_server.call_tool(
            "workflow_materialisation_diagnostics",
            {"required_concept_ids": ["#V#arxiv_paper_representation_workflow"]},
        )
        items = cast(list[Any], result)
        first = items[0]
        return cast(dict[str, Any], json.loads(cast(str, getattr(first, "text"))))

    payload = asyncio.run(_runner())

    assert payload["success"] is False
    assert payload["error_code"] == "workflow_global_admin_authority_required"
    assert calls == []


def test_workflow_concept_parity_audit_stdio_call_path(monkeypatch):
    from src.backend.mcp_server import mcp_stdio_server
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "build_workflow_concept_parity_audit",
        lambda **kwargs: calls.append(dict(kwargs)) or {
            "success": True,
            "summary": {"audited_count": 1},
            "concepts": [
                {
                    "concept_id": (kwargs.get("concept_ids") or ["#V#alpha_workflow"])[0],
                    "diagnostic_state": "present",
                }
            ],
        },
    )

    async def _runner():
        result = await mcp_stdio_server.call_tool(
            "workflow_concept_parity_audit",
            {"concept_ids": ["#V#alpha_workflow"]},
        )
        items = cast(list[Any], result)
        first = items[0]
        return cast(dict[str, Any], json.loads(cast(str, getattr(first, "text"))))

    payload = asyncio.run(_runner())

    assert payload["success"] is False
    assert payload["error_code"] == "workflow_global_admin_authority_required"
    assert calls == []


def test_raw_stdio_workflow_launch_rejects_payload_only_actor_claims():
    from src.backend.mcp_server import mcp_stdio_server

    async def _runner():
        actor_claims = {
            "workflow_id": "#V#restricted_workflow",
            "user_id": "#V#trusted_member",
            "org_id": "#V#trusted_org",
            "namespace": "#V#trusted_member@trusted_org",
        }
        calls = [
            ("workflow_execute", actor_claims),
            (
                "workflow_create_schedule",
                {
                    **actor_claims,
                    "schedule_type": "interval",
                    "interval_seconds": 60,
                },
            ),
            (
                "experiment_execute_target_workflow",
                {**actor_claims, "workflow_inputs": {}},
            ),
            (
                "workflow_list_instances",
                {
                    "user_id": actor_claims["user_id"],
                    "org_id": actor_claims["org_id"],
                    "namespace": actor_claims["namespace"],
                },
            ),
            (
                "workflow_list_execution_traces",
                {
                    "user_id": actor_claims["user_id"],
                    "org_id": actor_claims["org_id"],
                    "namespace": actor_claims["namespace"],
                },
            ),
            (
                "workflow_list_use_episodes",
                {
                    **actor_claims,
                    "workflow_id": actor_claims["workflow_id"],
                },
            ),
            (
                "workflow_build_prediction_envelope",
                actor_claims,
            ),
        ]
        payloads = []
        for tool_name, arguments in calls:
            result = await mcp_stdio_server.call_tool(tool_name, arguments)
            items = cast(list[Any], result)
            first = items[0]
            payloads.append(
                cast(dict[str, Any], json.loads(cast(str, getattr(first, "text"))))
            )
        return payloads

    payloads = asyncio.run(_runner())

    for index, payload in enumerate(payloads):
        assert payload["success"] is False
        assert payload["error_code"] == (
            "workflow_global_admin_authority_required"
            if index == 2
            else "workflow_actor_authority_required"
        )


def test_raw_stdio_rejects_global_workflow_control_plane_tools():
    from src.backend.mcp_server import mcp_stdio_server

    async def _runner():
        calls = [
            (
                "workflow_bind_event",
                {"event_type": "event.test", "workflow_id": "#V#workflow"},
            ),
            ("workflow_list_event_bindings", {}),
            (
                "workflow_set_event_binding_enabled",
                {"binding_id": "binding-1", "enabled": False},
            ),
            ("workflow_delete_event_binding", {"binding_id": "binding-1"}),
            ("workflow_mcp_health_check", {"include_introspection": False}),
            ("workflow_materialisation_diagnostics", {}),
            ("workflow_concept_parity_audit", {}),
            ("testing_theory_create_slice", {"name": "fixture"}),
            (
                "testing_theory_import_canonical_context",
                {"theory_id": "foreign-theory"},
            ),
            (
                "testing_theory_assert_local_claims",
                {"theory_id": "foreign-theory", "claims": []},
            ),
            ("testing_theory_compute_diff", {"theory_id": "foreign-theory"}),
            (
                "testing_theory_rollback_local_writes",
                {"theory_id": "foreign-theory"},
            ),
            (
                "testing_theory_promote_validated_claims",
                {"theory_id": "foreign-theory"},
            ),
            ("testing_theory_gc_expired", {}),
            ("experiment_run_list", {}),
            ("experiment_run_get", {"run_id": "foreign-run"}),
            ("experiment_create_spec", {}),
            ("experiment_start_run", {"experiment_spec_id": "foreign-spec"}),
            ("experiment_record_observation", {"run_id": "foreign-run"}),
            ("experiment_compute_verdict", {"run_id": "foreign-run"}),
            ("experiment_emit_learning_signal", {"run_id": "foreign-run"}),
            (
                "experiment_execute_target_workflow",
                {"workflow_id": "#V#foreign_workflow"},
            ),
            ("experiment_execute_regression_suite", {}),
            ("testing_prepare_experiment_spec", {}),
            ("testing_prepare_meeting_invitation_spec", {}),
            ("testing_prepare_arxiv_paper_ingestion_fixture", {}),
            ("testing_verify_arxiv_paper_ingestion_result", {}),
            (
                "testing_cleanup_arxiv_paper_ingestion_artifacts",
                {"paper_concept_id": "#V#foreign_paper"},
            ),
            ("chat_history_get_segments", {"session_id": "foreign-session"}),
            (
                "chat_history_get_debug_entry",
                {"session_id": "foreign-session", "history_index": 0},
            ),
            (
                "conversation_telemetry_get_locator",
                {"session_id": "foreign-session"},
            ),
            ("turn_execution_list", {}),
            ("turn_execution_get", {"request_id": "foreign-turn"}),
            (
                "turn_execution_get_diagnostics",
                {"request_id": "foreign-turn"},
            ),
            (
                "turn_execution_get_live_progress",
                {"request_id": "foreign-turn"},
            ),
            (
                "turn_execution_get_critic_bundle",
                {"request_id": "foreign-turn"},
            ),
            ("turn_execution_search_failures", {}),
        ]
        payloads = []
        for tool_name, arguments in calls:
            result = await mcp_stdio_server.call_tool(tool_name, arguments)
            first = cast(list[Any], result)[0]
            payloads.append(
                cast(dict[str, Any], json.loads(cast(str, getattr(first, "text"))))
            )
        return payloads

    payloads = asyncio.run(_runner())
    assert all(payload["success"] is False for payload in payloads)
    assert {
        payload["error_code"] for payload in payloads
    } == {"workflow_global_admin_authority_required"}


def test_raw_stdio_sensitive_workflow_handlers_bind_untrusted_actor_source(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import (
        get_internal_mcp_actor_context_source,
    )
    from src.backend.mcp_server import mcp_stdio_server

    tool_target_pairs = [
        ("testing_theory_create_slice", "_testing_theory_create_slice"),
        (
            "testing_theory_import_canonical_context",
            "_testing_theory_import_canonical_context",
        ),
        (
            "testing_theory_assert_local_claims",
            "_testing_theory_assert_local_claims",
        ),
        ("testing_theory_compute_diff", "_testing_theory_compute_diff"),
        (
            "testing_theory_rollback_local_writes",
            "_testing_theory_rollback_local_writes",
        ),
        (
            "testing_theory_promote_validated_claims",
            "_testing_theory_promote_validated_claims",
        ),
        ("testing_theory_gc_expired", "_testing_theory_gc_expired"),
        ("workflow_list_use_episodes", "_workflow_list_use_episodes"),
        ("workflow_bind_event", "_workflow_bind_event"),
        ("workflow_list_event_bindings", "_workflow_list_event_bindings"),
        (
            "workflow_set_event_binding_enabled",
            "_workflow_set_event_binding_enabled",
        ),
        ("workflow_delete_event_binding", "_workflow_delete_event_binding"),
        ("workflow_mcp_health_check", "_workflow_mcp_health_check"),
        (
            "workflow_materialisation_diagnostics",
            "_workflow_materialisation_diagnostics",
        ),
        ("workflow_concept_parity_audit", "_workflow_concept_parity_audit"),
        ("workflow_list_instances", "_workflow_list_instances"),
        ("workflow_list_execution_traces", "_workflow_list_execution_traces"),
        (
            "workflow_build_prediction_envelope",
            "_workflow_build_prediction_envelope",
        ),
        ("workflow_get_instance", "_workflow_get_instance"),
        ("workflow_get_execution_trace", "_workflow_get_execution_trace"),
        ("workflow_cancel_instance", "_workflow_cancel_instance"),
        ("workflow_resume_instance", "_workflow_resume_instance"),
        ("workflow_retry_instance", "_workflow_retry_instance"),
        ("workflow_create_schedule", "_workflow_create_schedule"),
        ("workflow_list_schedules", "_workflow_list_schedules"),
        ("workflow_get_schedule", "_workflow_get_schedule"),
        ("workflow_set_schedule_enabled", "_workflow_set_schedule_enabled"),
        ("workflow_delete_schedule", "_workflow_delete_schedule"),
        ("workflow_trigger_schedule", "_workflow_trigger_schedule"),
        (
            "experiment_execute_target_workflow",
            "_experiment_execute_target_workflow",
        ),
        ("experiment_run_list", "_experiment_run_list"),
        ("experiment_run_get", "_experiment_run_get"),
        ("experiment_create_spec", "_experiment_create_spec"),
        ("experiment_start_run", "_experiment_start_run"),
        ("experiment_record_observation", "_experiment_record_observation"),
        ("experiment_compute_verdict", "_experiment_compute_verdict"),
        ("experiment_emit_learning_signal", "_experiment_emit_learning_signal"),
        (
            "experiment_execute_regression_suite",
            "_experiment_execute_regression_suite",
        ),
        ("testing_prepare_experiment_spec", "_testing_prepare_experiment_spec"),
        (
            "testing_prepare_meeting_invitation_spec",
            "_testing_prepare_meeting_invitation_spec",
        ),
        (
            "testing_prepare_arxiv_paper_ingestion_fixture",
            "_testing_prepare_arxiv_paper_ingestion_fixture",
        ),
        (
            "testing_verify_arxiv_paper_ingestion_result",
            "_testing_verify_arxiv_paper_ingestion_result",
        ),
        (
            "testing_cleanup_arxiv_paper_ingestion_artifacts",
            "_testing_cleanup_arxiv_paper_ingestion_artifacts",
        ),
        ("chat_history_get_segments", "_chat_history_get_segments"),
        ("chat_history_get_debug_entry", "_chat_history_get_debug_entry"),
        (
            "conversation_telemetry_get_locator",
            "_conversation_telemetry_get_locator",
        ),
        ("turn_execution_list", "_turn_execution_list"),
        ("turn_execution_get", "_turn_execution_get"),
        ("turn_execution_get_diagnostics", "_turn_execution_get_diagnostics"),
        ("turn_execution_get_live_progress", "_turn_execution_get_live_progress"),
        ("turn_execution_get_critic_bundle", "_turn_execution_get_critic_bundle"),
        ("turn_execution_search_failures", "_turn_execution_search_failures"),
    ]

    def _probe(**_arguments):
        return {
            "success": True,
            "actor_context_source": get_internal_mcp_actor_context_source(),
        }

    for _tool_name, target_name in tool_target_pairs:
        monkeypatch.setattr(mcp_stdio_server, target_name, _probe)

    async def _runner():
        payloads = []
        for tool_name, _target_name in tool_target_pairs:
            result = await mcp_stdio_server.call_tool(tool_name, {})
            items = cast(list[Any], result)
            first = items[0]
            payloads.append(
                cast(dict[str, Any], json.loads(cast(str, getattr(first, "text"))))
            )
        return payloads

    payloads = asyncio.run(_runner())

    assert all(payload["success"] is True for payload in payloads)
    assert {
        payload["actor_context_source"] for payload in payloads
    } == {"tool_payload_fallback"}
