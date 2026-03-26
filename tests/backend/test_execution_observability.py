from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pymongo.errors import PyMongoError

from src.backend.workflows.durable.execution_observability import (
    await_workflow_terminal_state,
)


def test_await_workflow_terminal_state_retries_transient_get_instance() -> None:
    manager = SimpleNamespace(
        get_instance=MagicMock(
            side_effect=[
                PyMongoError("server selection timeout while reading workflow_instances"),
                SimpleNamespace(status="completed"),
            ]
        )
    )

    with (
        patch(
            "src.backend.db.transient_errors.attempt_reconnect",
            return_value={"reconnected": True},
        ),
        patch(
            "src.backend.db.transient_errors.time.sleep",
            return_value=None,
        ),
    ):
        result = await_workflow_terminal_state(
            manager,
            "instance-1",
            timeout_seconds=30,
            poll_interval_seconds=0,
        )

    assert result.timed_out is False
    assert result.final_status == "completed"
    assert manager.get_instance.call_count == 2
