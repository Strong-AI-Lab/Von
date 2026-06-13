"""Tests for the event-storm backlog damper (JVNAUTOSCI-2507).

The damper suppresses NEW event-triggered workflow-instance creation for a
``(workflow_id, source_event_type)`` pair once an unprocessed backlog already
exists, so a self-sustaining event cascade (e.g. paper_recommendation /
episode_evaluation workflows that rewrite graph relations and re-trigger
themselves) cannot keep minting instances that never drain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.backend.workflows.durable import workflow_instance_submission_service as svc


def _fake_manager(*, existing_key: str | None, backlog: int) -> MagicMock:
    manager = MagicMock()
    manager.find_instance_id_by_event_key.return_value = existing_key
    manager.count_recent_event_backlog.return_value = backlog
    return manager


def _throttle(manager: MagicMock):
    return svc._event_backlog_throttle_result(
        manager=manager,
        workflow_id="#V#paper_recommendation_evaluation_workflow",
        source_event_type="text_relation.upserted",
        event_idempotency_key="evt:text_relation.upserted:abc123",
        verification_payload={"runnable_verification_success": True},
    )


# ---------------------------------------------------------------------------
# Env readers
# ---------------------------------------------------------------------------


def test_backlog_limit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(svc._EVENT_BACKLOG_LIMIT_ENV, raising=False)
    assert svc._read_event_backlog_limit() == svc._DEFAULT_EVENT_BACKLOG_LIMIT


def test_backlog_limit_override_and_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "5")
    assert svc._read_event_backlog_limit() == 5
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "0")
    assert svc._read_event_backlog_limit() == 0  # disabled sentinel


def test_backlog_window_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_WINDOW_ENV, "5")  # below minimum 60
    assert svc._read_event_backlog_window_seconds() == 60.0


# ---------------------------------------------------------------------------
# Throttle decision
# ---------------------------------------------------------------------------


def test_disabled_when_limit_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "0")
    manager = _fake_manager(existing_key=None, backlog=10_000)
    assert _throttle(manager) is None
    manager.count_recent_event_backlog.assert_not_called()


def test_idempotent_redelivery_not_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "1")
    # An instance already exists for this exact event key: it must resolve to
    # that instance, not be suppressed, even with a huge backlog.
    manager = _fake_manager(existing_key="instance-existing", backlog=10_000)
    assert _throttle(manager) is None
    manager.count_recent_event_backlog.assert_not_called()


def test_below_limit_allows_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "50")
    manager = _fake_manager(existing_key=None, backlog=49)
    assert _throttle(manager) is None


def test_at_or_above_limit_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "50")
    manager = _fake_manager(existing_key=None, backlog=50)
    result = _throttle(manager)
    assert result is not None
    assert result.success is False
    assert result.status == "throttled_event_backlog"
    assert result.error_code == "event_backlog_throttled"
    assert result.instance_id is None
    assert result.created_new is False


def test_throttle_query_is_time_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(svc._EVENT_BACKLOG_LIMIT_ENV, "10")
    monkeypatch.setenv(svc._EVENT_BACKLOG_WINDOW_ENV, "3600")
    manager = _fake_manager(existing_key=None, backlog=0)
    before = datetime.now(timezone.utc)
    _throttle(manager)
    kwargs = manager.count_recent_event_backlog.call_args.kwargs
    assert kwargs["workflow_id"] == "#V#paper_recommendation_evaluation_workflow"
    assert kwargs["source_event_type"] == "text_relation.upserted"
    assert tuple(kwargs["statuses"]) == svc._EVENT_BACKLOG_PENDING_STATUSES
    # since_utc must be a bounded lower bound in the past, never unbounded.
    assert kwargs["since_utc"] < before
    assert (before - kwargs["since_utc"]).total_seconds() == pytest.approx(
        3600, abs=30
    )
