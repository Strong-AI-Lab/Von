from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask, jsonify, request, session

from src.backend.services import chat_prompt_queue_dispatch_service as dispatch_service
from src.backend.services import task_execution_service


@pytest.fixture(autouse=True)
def isolate_dispatcher_queue_reconciliation(monkeypatch):
    """Keep dispatch-unit tests independent from a live queue database."""

    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "claim_task_execution_terminal_reconciliation",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "backfill_linked_terminal_reconciliation",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "claim_task_execution_launch_reconciliation",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reconcile_next_server_dispatch_handoff",
        lambda: None,
    )


def _reserved_record() -> dict[str, object]:
    return {
        "queue_id": "queue-1",
        "dispatch_reservation_token": "reservation-1",
        "user_concept_id": "#V#user",
        "organisation_concept_id": None,
        "namespace": "#V#user",
        "session_id": "session-1",
        "session_name": "Conversation one",
        "prompt_raw": "Continue this work",
        "client_request_id": "request-1",
        "attempt_id": "attempt-1",
        "execution_envelope": {"language": "en-NZ"},
    }


def test_dispatcher_leaves_accepted_reservation_for_admission(
    monkeypatch,
) -> None:
    reserved = _reserved_record()
    released: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: dict(reserved),
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "release_server_dispatch_reservation",
        lambda **kwargs: released.append(kwargs) or True,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_server_dispatch_reservation",
        lambda **kwargs: failed.append(kwargs) or True,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(
        app=object(),
        submit=lambda _app, record: dispatch_service.ChatPromptDispatchOutcome(
            accepted=record["queue_id"] == "queue-1"
        ),
    )

    assert dispatcher.run_once() is True
    assert released == []
    assert failed == []
    assert dispatcher.snapshot()["accepted_count"] == 1


def test_blocked_handoff_falls_through_to_unrelated_ready_dispatch(
    monkeypatch,
) -> None:
    reserved = _reserved_record()
    submissions: list[str] = []

    def defer_blocked_handoff():
        raise dispatch_service.chat_prompt_queue_service.ConversationTurnAlreadyActive(
            "legacy source is still executing",
            queue_id="source-active",
        )

    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reconcile_next_server_dispatch_handoff",
        defer_blocked_handoff,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: dict(reserved),
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(
        app=object(),
        submit=lambda _app, record: (
            submissions.append(str(record["queue_id"]))
            or dispatch_service.ChatPromptDispatchOutcome(accepted=True)
        ),
    )

    assert dispatcher.run_once() is True
    assert submissions == ["queue-1"]
    snapshot = dispatcher.snapshot()
    assert snapshot["handoff_reconciliation_failed_count"] == 1
    assert snapshot["accepted_count"] == 1


def test_dispatcher_releases_exact_retryable_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: _reserved_record(),
    )
    released: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "release_server_dispatch_reservation",
        lambda **kwargs: released.append(kwargs) or True,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_server_dispatch_reservation",
        lambda **_kwargs: False,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(
        app=object(),
        submit=lambda _app, _record: dispatch_service.ChatPromptDispatchOutcome(
            accepted=False,
            retryable=True,
            retry_after_seconds=3,
            error="capacity",
        ),
    )

    assert dispatcher.run_once() is True
    assert released == [
        {
            "queue_id": "queue-1",
            "dispatch_reservation_token": "reservation-1",
            "server_instance_id": dispatch_service.SERVER_INSTANCE_ID,
            "retry_after_seconds": 3,
            "error": "capacity",
        }
    ]


def test_dispatcher_terminalises_non_retryable_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: _reserved_record(),
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "release_server_dispatch_reservation",
        lambda **_kwargs: False,
    )
    failed: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_server_dispatch_reservation",
        lambda **kwargs: failed.append(kwargs) or True,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(
        app=object(),
        submit=lambda _app, _record: {
            "accepted": False,
            "retryable": False,
            "error": "authority revoked",
        },
    )

    assert dispatcher.run_once() is True
    assert failed == [
        {
            "queue_id": "queue-1",
            "dispatch_reservation_token": "reservation-1",
            "server_instance_id": dispatch_service.SERVER_INSTANCE_ID,
            "error": "authority revoked",
        }
    ]


def test_dispatcher_terminalises_exact_linked_execution_only_after_queue_cas(
    monkeypatch,
) -> None:
    record = _reserved_record()
    record.update(
        {
            "organisation_concept_id": "#V#org",
            "enqueue_submission_id": "enqueue-1",
            "task_concept_id": "#V#task_1",
            "task_execution_concept_id": "#V#task_execution_1",
        }
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: dict(record),
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_server_dispatch_reservation",
        lambda **_kwargs: True,
    )
    claimed = {
        **record,
        "status": "failed",
        "task_execution_reconciliation_status": "claimed",
        "task_execution_reconciliation_queue_status": "failed",
        "task_execution_reconciliation_token": "terminal-claim-1",
        "task_execution_reconciliation_error": "authority revoked",
    }
    claim_calls = 0

    def _claim(**_kwargs):
        nonlocal claim_calls
        claim_calls += 1
        return dict(claimed) if claim_calls == 2 else None

    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "claim_task_execution_terminal_reconciliation",
        _claim,
    )
    acknowledgements: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "acknowledge_task_execution_terminal_reconciliation",
        lambda **kwargs: acknowledgements.append(kwargs) or True,
    )
    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "get_task_execution",
        lambda *_args, **_kwargs: {
            "task_concept_id": "#V#task_1",
            "enqueue_submission_id": "enqueue-1",
            "queue_id": "queue-1",
            "status": "pending",
        },
    )
    transitions: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "transition_task_execution",
        lambda execution_id, **kwargs: (
            transitions.append((execution_id, kwargs)) or {"status": kwargs["status"]}
        ),
    )
    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "reconcile_task_execution_queue_record",
        lambda _record: {
            "task_execution_concept_id": "#V#task_execution_1",
            "task_concept_id": "#V#task_1",
            "enqueue_submission_id": "enqueue-1",
            "queue_id": "queue-1",
            "status": "pending",
        },
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(
        app=object(),
        submit=lambda _app, _record: dispatch_service.ChatPromptDispatchOutcome(
            accepted=False,
            error="authority revoked",
        ),
    )

    assert dispatcher.run_once() is True
    assert transitions == [
        (
            "#V#task_execution_1",
            {
                "actor_concept_id": "#V#user",
                "organisation_concept_id": "#V#org",
                "enqueue_submission_id": "enqueue-1",
                "queue_id": "queue-1",
                "status": task_execution_service.TASK_EXECUTION_STATUS_FAILED,
                "result": "authority revoked",
                "source": "queue_dispatcher",
            },
        )
    ]
    assert acknowledgements == [
        {
            "queue_id": "queue-1",
            "reconciliation_token": "terminal-claim-1",
            "server_instance_id": dispatch_service.SERVER_INSTANCE_ID,
            "task_execution_concept_id": "#V#task_execution_1",
            "enqueue_submission_id": "enqueue-1",
            "queue_status": "failed",
        }
    ]


def test_dispatcher_does_not_terminalise_linked_execution_after_lost_queue_cas(
    monkeypatch,
) -> None:
    record = _reserved_record()
    record.update(
        {
            "organisation_concept_id": "#V#org",
            "enqueue_submission_id": "enqueue-1",
            "task_concept_id": "#V#task_1",
            "task_execution_concept_id": "#V#task_execution_1",
        }
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: dict(record),
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_server_dispatch_reservation",
        lambda **_kwargs: False,
    )
    transition = MagicMock()
    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "transition_task_execution",
        transition,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(
        app=object(),
        submit=lambda _app, _record: dispatch_service.ChatPromptDispatchOutcome(
            accepted=False,
            error="authority revoked",
        ),
    )

    assert dispatcher.run_once() is True
    transition.assert_not_called()


def test_dispatcher_retries_durable_terminal_marker_after_projection_failure(
    monkeypatch,
) -> None:
    from src.backend.security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id_with_source,
    )

    base_claim = {
        "queue_id": "queue-terminal-retry",
        "status": "failed",
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "enqueue_submission_id": "enqueue-terminal-retry",
        "task_concept_id": "#V#task_retry",
        "task_execution_concept_id": "#V#task_execution_retry",
        "task_execution_reconciliation_status": "claimed",
        "task_execution_reconciliation_queue_status": "failed",
        "task_execution_reconciliation_error": "turn failed",
    }
    claims = iter(
        [
            {**base_claim, "task_execution_reconciliation_token": "claim-1"},
            {**base_claim, "task_execution_reconciliation_token": "claim-2"},
        ]
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "claim_task_execution_terminal_reconciliation",
        lambda **_kwargs: next(claims),
    )
    released: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "release_task_execution_terminal_reconciliation",
        lambda **kwargs: released.append(kwargs) or True,
    )
    acknowledged: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "acknowledge_task_execution_terminal_reconciliation",
        lambda **kwargs: acknowledged.append(kwargs) or True,
    )

    def _get_execution(*_args, **_kwargs):
        assert get_effective_user_concept_id_with_source()[0] == "#V#user"
        assert get_effective_organisation_concept_id() == "#V#org"
        return {
            "task_concept_id": "#V#task_retry",
            "enqueue_submission_id": "enqueue-terminal-retry",
            "queue_id": "queue-terminal-retry",
            "status": "in_progress",
        }

    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "get_task_execution",
        _get_execution,
    )
    transition_attempts = 0

    def _transition(_execution_id, **kwargs):
        nonlocal transition_attempts
        assert get_effective_user_concept_id_with_source()[0] == "#V#user"
        assert get_effective_organisation_concept_id() == "#V#org"
        transition_attempts += 1
        if transition_attempts == 1:
            raise RuntimeError("task execution store unavailable")
        return {"status": kwargs["status"]}

    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "transition_task_execution",
        _transition,
    )
    reserve = MagicMock(return_value=None)
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        reserve,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(app=object(), submit=lambda *_args: {"accepted": True})

    assert dispatcher.run_once() is True
    assert dispatcher.run_once() is True
    assert transition_attempts == 2
    assert released[0]["reconciliation_token"] == "claim-1"
    assert acknowledged[0]["reconciliation_token"] == "claim-2"
    assert dispatcher.snapshot()["task_execution_reconciliation_retry_count"] == 1
    assert dispatcher.snapshot()["task_execution_reconciled_count"] == 1
    reserve.assert_not_called()


def test_dispatcher_projects_and_activates_durable_task_launch_before_dispatch(
    monkeypatch,
) -> None:
    claim = {
        "queue_id": "queue-launch",
        "status": "queued",
        "dispatch_ready": False,
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
        "session_id": "session-launch",
        "enqueue_submission_id": "enqueue-launch",
        "task_concept_id": "#V#task_launch",
        "task_execution_concept_id": "#V#task_execution_launch",
        "task_launch_reconciliation_token": "launch-claim-1",
        "execution_envelope_version": 1,
        "execution_envelope": {"initiation_id": "launch-1"},
    }
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "claim_task_execution_launch_reconciliation",
        lambda **_kwargs: dict(claim),
    )
    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "reconcile_task_execution_queue_record",
        lambda record: {
            "task_execution_concept_id": record["task_execution_concept_id"],
            "queue_id": record["queue_id"],
            "enqueue_submission_id": record["enqueue_submission_id"],
            "status": "pending",
        },
    )
    acknowledgements: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "acknowledge_task_execution_launch_reconciliation",
        lambda **kwargs: acknowledgements.append(kwargs) or True,
    )
    reserve = MagicMock(return_value=None)
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        reserve,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(app=object(), submit=lambda *_args: {"accepted": True})

    assert dispatcher.run_once() is True
    assert acknowledgements == [
        {
            "queue_id": "queue-launch",
            "reconciliation_token": "launch-claim-1",
            "server_instance_id": dispatch_service.SERVER_INSTANCE_ID,
            "task_execution_concept_id": "#V#task_execution_launch",
            "enqueue_submission_id": "enqueue-launch",
        }
    ]
    assert dispatcher.snapshot()["task_launch_reconciled_count"] == 1
    reserve.assert_not_called()


def test_dispatcher_retries_unknown_launch_projection_and_fails_revoked_launch(
    monkeypatch,
) -> None:
    base_claim = {
        "queue_id": "queue-launch-retry",
        "status": "queued",
        "dispatch_ready": False,
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
        "session_id": "session-launch",
        "enqueue_submission_id": "enqueue-launch-retry",
        "task_concept_id": "#V#task_launch_retry",
        "task_execution_concept_id": "#V#task_execution_launch_retry",
        "execution_envelope_version": 1,
        "execution_envelope": {"initiation_id": "launch-retry"},
    }
    claims = iter(
        [
            {**base_claim, "task_launch_reconciliation_token": "claim-1"},
            {**base_claim, "task_launch_reconciliation_token": "claim-2"},
        ]
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "claim_task_execution_launch_reconciliation",
        lambda **_kwargs: next(claims),
    )
    attempts = 0

    def _reconcile(_record):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("task store temporarily unavailable")
        raise task_execution_service.TaskExecutionAccessError(
            "task_not_executable",
            "The task was cancelled",
        )

    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "reconcile_task_execution_queue_record",
        _reconcile,
    )
    released: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "release_task_execution_launch_reconciliation",
        lambda **kwargs: released.append(kwargs) or True,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_task_execution_launch_reconciliation",
        lambda **kwargs: failed.append(kwargs) or True,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(app=object(), submit=lambda *_args: {"accepted": True})

    assert dispatcher.run_once() is True
    assert dispatcher.run_once() is True
    assert released[0]["reconciliation_token"] == "claim-1"
    assert "temporarily unavailable" in str(released[0]["error"])
    assert failed[0]["reconciliation_token"] == "claim-2"
    assert "The task was cancelled" in str(failed[0]["error"])
    snapshot = dispatcher.snapshot()
    assert snapshot["task_launch_reconciliation_retry_count"] == 1
    assert snapshot["task_launch_reconciliation_failed_count"] == 1


def test_dispatcher_revalidates_task_authority_before_model_submission(
    monkeypatch,
) -> None:
    record = {
        **_reserved_record(),
        "organisation_concept_id": "#V#org",
        "enqueue_submission_id": "enqueue-revoked",
        "task_concept_id": "#V#task_revoked",
        "task_execution_concept_id": "#V#task_execution_revoked",
    }
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: dict(record),
    )
    monkeypatch.setattr(
        dispatch_service.task_execution_service,
        "reconcile_task_execution_queue_record",
        MagicMock(
            side_effect=task_execution_service.TaskExecutionAccessError(
                "task_not_executable",
                "The task was completed while queued",
            )
        ),
    )
    failed: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "fail_server_dispatch_reservation",
        lambda **kwargs: failed.append(kwargs) or True,
    )
    monkeypatch.setattr(
        dispatch_service,
        "reconcile_linked_task_execution_terminal",
        MagicMock(),
    )
    submit = MagicMock()
    dispatcher = dispatch_service.ChatPromptQueueDispatcher()
    dispatcher.configure(app=object(), submit=submit)

    assert dispatcher.run_once() is True
    submit.assert_not_called()
    assert failed[0]["queue_id"] == "queue-1"
    assert "no longer executable" in str(failed[0]["error"])


def test_worker_backfills_terminal_retention_in_bounded_batches(monkeypatch) -> None:
    batches = iter([500, 3])
    observed_batch_sizes: list[int] = []

    def _backfill(*, batch_size: int) -> int:
        observed_batch_sizes.append(batch_size)
        return next(batches)

    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "backfill_legacy_terminal_purge_after",
        _backfill,
    )
    monkeypatch.setattr(
        dispatch_service.chat_prompt_queue_service,
        "reserve_next_server_dispatch",
        lambda **_kwargs: None,
    )
    dispatcher = dispatch_service.ChatPromptQueueDispatcher(poll_interval_seconds=0.01)
    dispatcher.configure(
        app=object(),
        submit=lambda _app, _record: dispatch_service.ChatPromptDispatchOutcome(
            accepted=True
        ),
    )
    dispatcher.start()
    for _ in range(100):
        if dispatcher.snapshot()["terminal_retention_backfill_complete"]:
            break
        dispatcher._wake.wait(0.01)
    dispatcher.stop(timeout=1)

    assert observed_batch_sizes == [500, 500]
    assert dispatcher.snapshot()["terminal_retention_backfilled_count"] == 503
    assert dispatcher.snapshot()["terminal_retention_backfill_complete"] is True


def test_generate_submission_uses_frozen_actor_scope_and_exact_reservation(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    app = Flask(__name__)
    app.secret_key = "test"
    captured: dict[str, object] = {}

    def _fake_generate():
        captured["payload"] = request.get_json()
        captured["session"] = dict(session)
        return jsonify(
            {
                "background": True,
                "task_id": "request-1",
                "status": "pending",
            }
        ), 202

    monkeypatch.setattr(von_routes, "generate", _fake_generate)
    record = _reserved_record()
    record.update(
        {
            "enqueue_submission_id": "enqueue-1",
            "task_concept_id": "#V#task_1",
            "task_execution_concept_id": "#V#task_execution_1",
        }
    )
    record["execution_envelope"] = {
        "language": "en-NZ",
        "user_id": "#V#attacker",
        "prompt_queue_id": "wrong-queue",
        "enqueue_submission_id": "attacker-enqueue",
        "task_concept_id": "#V#attacker_task",
        "task_execution_concept_id": "#V#attacker_execution",
    }

    outcome = von_routes.submit_server_dispatched_chat_prompt(app, record)

    assert outcome.accepted is True
    assert captured["payload"] == {
        "language": "en-NZ",
        "prompt": "Continue this work",
        "conversation_session_id": "session-1",
        "conversation_session_name": "Conversation one",
        "background": True,
        "prompt_queue_id": "queue-1",
        "client_request_id": "request-1",
        "attempt_id": "attempt-1",
        "dispatch_reservation_token": "reservation-1",
        "enqueue_submission_id": "enqueue-1",
        "task_concept_id": "#V#task_1",
        "task_execution_concept_id": "#V#task_execution_1",
    }
    assert captured["session"] == {
        "user_concept_id": "#V#user",
        "namespace": "#V#user",
        "session_id": "session-1",
    }


def test_generate_submission_rejects_scope_namespace_mismatch() -> None:
    from src.backend.server.routes import von_routes

    app = Flask(__name__)
    record = _reserved_record()
    record["namespace"] = "#V#another_user"

    outcome = von_routes.submit_server_dispatched_chat_prompt(app, record)

    assert outcome.accepted is False
    assert outcome.retryable is False
    assert "namespace" in str(outcome.error)


def _task_admission_token():
    return SimpleNamespace(
        task_concept_id="#V#task_1",
        task_execution_concept_id="#V#task_execution_1",
        enqueue_submission_id="enqueue-1",
        queue_id="queue-1",
        client_request_id="request-1",
        session_id="session-1",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )


def test_turn_projection_task_correlation_is_stamped_from_admission_token() -> None:
    from src.backend.server.routes import von_routes

    debug = {
        "turn_execution_record": {
            "request_id": "untrusted-request",
            "session_id": "untrusted-session",
            "queue_id": "untrusted-queue",
            "task_concept_id": "#V#untrusted_task",
        },
    }

    von_routes._stamp_bound_task_execution_turn_projection(
        debug,
        _task_admission_token(),
        completion_gate_source={
            "completion_gate_safe_to_claim_completion": True,
            "completion_gate_requires_follow_up": False,
        },
    )

    assert debug["turn_execution_record"] == {
        "request_id": "request-1",
        "session_id": "session-1",
        "conversation_session_id": "session-1",
        "queue_id": "queue-1",
        "enqueue_submission_id": "enqueue-1",
        "task_concept_id": "#V#task_1",
        "task_execution_concept_id": "#V#task_execution_1",
        "completion_gate": {
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
    }


def test_queue_turn_completion_does_not_infer_task_specification_completion() -> None:
    from src.backend.server.routes import von_routes

    assert not hasattr(
        von_routes,
        "_terminalise_task_specification_from_verified_turn",
    )
    assert not hasattr(
        task_execution_service,
        "terminalise_task_from_execution_evidence",
    )


def test_queue_cancellation_terminalises_linked_execution(monkeypatch) -> None:
    from src.backend.server.routes import von_routes

    app = Flask(__name__)
    app.secret_key = "test"
    scope = {
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
    }
    cancelled = {
        "queue_id": "queue-1",
        "status": "cancelled",
        "enqueue_submission_id": "enqueue-1",
        "task_concept_id": "#V#task_1",
        "task_execution_concept_id": "#V#task_execution_1",
    }
    monkeypatch.setattr(
        von_routes,
        "_get_current_chat_prompt_queue_scope",
        lambda: dict(scope),
    )
    monkeypatch.setattr(
        von_routes.chat_prompt_queue_service,
        "cancel_prompt_record",
        lambda **_kwargs: dict(cancelled),
    )
    reconcile = MagicMock()
    monkeypatch.setattr(
        von_routes,
        "reconcile_linked_task_execution_terminal",
        reconcile,
    )

    with app.test_request_context("/von/api/chat_prompt_queue/queue-1"):
        response = von_routes.cancel_chat_prompt_queue_route("queue-1")

    assert response.get_json()["item"] == cancelled
    reconcile.assert_called_once_with(
        {**cancelled, **scope},
        status="cancelled",
        source="queue_cancellation",
    )
