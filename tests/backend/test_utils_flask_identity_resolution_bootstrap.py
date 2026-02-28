"""Integration coverage for identity-resolution schedule bootstrap at startup."""

from __future__ import annotations

from unittest.mock import MagicMock


def test_start_durable_system_bootstraps_identity_schedule(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.workflows.durable import startup as durable_startup
    from src.backend.services import (
        identity_resolution_schedule_bootstrap_service as schedule_bootstrap,
    )

    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    # Reset globals to force fresh startup path.
    monkeypatch.setattr(utils_flask, "_durable_workflow_registry", None)
    monkeypatch.setattr(utils_flask, "_durable_action_registry", None)

    monkeypatch.setattr(utils_flask, "_build_durable_workflow_registry", lambda: object())
    monkeypatch.setattr(utils_flask, "_build_durable_action_registry", lambda: object())
    monkeypatch.setattr(utils_flask, "_get_durable_definition_loader", lambda: lambda _workflow_id: None)

    monkeypatch.setattr(durable_startup, "recover_orphaned_instances", lambda: 0)
    monkeypatch.setattr(
        durable_startup,
        "start_worker_and_scheduler",
        lambda **_kwargs: {"worker": "worker", "scheduler": "scheduler"},
    )
    monkeypatch.setattr(
        durable_startup,
        "get_system_status",
        lambda: {
            "worker_running": True,
            "scheduler_running": True,
            "instances": {"pending": 0},
        },
    )

    monkeypatch.setattr(
        schedule_bootstrap,
        "ensure_identity_resolution_background_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
    )

    app_logger = MagicMock()
    result = utils_flask._start_durable_workflow_system(app_logger)

    assert isinstance(result, dict)
    bootstrap_report = result.get("identity_resolution_schedule_bootstrap")
    assert isinstance(bootstrap_report, dict)
    assert bootstrap_report.get("success") is True
    assert bootstrap_report.get("created_count") == 1
