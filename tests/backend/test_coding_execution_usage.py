"""Replay the existing controller with fixture events, then read canonical tasks."""

import io
import json
from pathlib import Path

import pytest
from test_codex_von_retry import canonical_controller, config  # noqa: F401

from scripts import codex_von_worker as worker
from src.backend.services import coding_execution_usage as usage
from src.backend.services import task_execution_timing_service as timing

# ruff: noqa: F811

COUNTERS = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}


def events(thread="root", counters=None):
    return [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": counters or COUNTERS},
    ]


def parse(rows):
    return usage.from_exec_stream(
        io.BytesIO(b"".join(json.dumps(row).encode() + b"\n" for row in rows)),
        configured_model="gpt-6-astra",
    )


def record(api, task_id, receipt, attempt="first"):
    return timing.record_usage(
        task_id,
        attempt_id=attempt,
        actor_concept_id=api.config["agent_id"],
        receipt=receipt,
    )


def start(api, task_id, attempt="first"):
    return timing.start(
        task_id,
        attempt_id=attempt,
        actor_concept_id=api.config["agent_id"],
        started_at="2026-09-17T10:00:00Z",
        source="codex_dgx",
    )


def test_root_only_duplicate_terminal_and_identity():
    rows = events()
    # Nested child counters and tool result text are not independent receipts.
    rows.insert(
        2,
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "usage": COUNTERS,
                "result": events("child"),
            },
        },
    )
    rows.append(rows[-1])
    receipt = parse(rows)
    usage.validate(receipt)
    assert receipt["tokens"] == COUNTERS
    assert receipt["coverage"] == "partial"
    assert receipt["thread_id"] == "root"
    assert receipt["turn_id"] is None
    assert receipt["turn_ordinal"] == 1
    assert len(receipt["source_sha256"]) == 64
    assert receipt["configured_model"] == "gpt-6-astra"
    assert receipt["observed_model"] is None
    assert receipt["provider"] is None
    rows = events()
    rows[1]["turn_id"] = rows[2]["turn_id"] = "observed-turn"
    assert parse(rows)["turn_id"] == "observed-turn"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        events()[:-1],
        events()[1:],
        events() + events("child"),
        events() + events()[1:],
        events()
        + [{"type": "turn.completed", "usage": dict(COUNTERS, output_tokens=21)}],
        events(counters=dict(COUNTERS, input_tokens=True)),
        events(counters=dict(COUNTERS, cached_input_tokens=101)),
        events(counters=dict(COUNTERS, output_tokens=-1)),
        events(counters={"input_tokens": 10}),
        [dict(events()[0], parent_thread_id="parent"), *events()[1:]],
        [*events()[:-1], dict(events()[-1], thread_id="foreign")],
        [*events()[:-1], dict(events()[-1], parent_turn_id="parent")],
        [*events(), {"type": "turn.failed"}],
        [*events(), None],
    ],
)
def test_unknown_is_not_zero_or_a_guessed_delta(rows):
    receipt = parse(rows)
    usage.validate(receipt)
    assert receipt["coverage"] == "unknown"
    assert receipt["tokens"] is None


def test_truncated_stream_stays_unknown():
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in events())
    receipt = usage.from_exec_stream(io.BytesIO(raw + b'{"type":'))
    assert receipt["coverage"] == "unknown"


def test_canonical_replay_retry_unknown_and_conflicting_thread(canonical_controller):
    _, api, create, *_ = canonical_controller
    task_id = create()
    start(api, task_id)
    receipt = parse(events())
    first = record(api, task_id, receipt)["execution_timing"]
    assert record(api, task_id, receipt)["execution_timing"] == first
    assert api.task(task_id)["execution_timing"] == first
    assert (
        first["usage"]["observed_total_tokens"] == 120
    )  # cached input is not added again
    assert first["cost"]["amount"] is None
    assert first["cost"]["currency"] is None
    with pytest.raises(ValueError, match="different usage"):
        record(api, task_id, parse(events(counters=dict(COUNTERS, output_tokens=21))))
    # A new attempt replaying the same root must not double count it.
    start(api, task_id, "retry")
    record(api, task_id, receipt, "retry")
    start(api, task_id, "unknown")
    record(api, task_id, usage.unknown("No receipt"), "unknown")
    readback = api.task(task_id)["execution_timing"]["usage"]
    assert readback["observed_total_tokens"] == 120
    assert readback["distinct_root_threads"] == 1
    assert readback["unknown_attempts"] == 1
    assert readback["coverage"] == "partial"
    # New root execution is real extra work, not repeated result delivery.
    record(api, task_id, parse(events("new-root")), "unknown")
    assert (
        api.task(task_id)["execution_timing"]["usage"]["observed_total_tokens"] == 240
    )
    # Conflicting cumulative counters on reused thread cannot safely be summed.
    start(api, task_id, "conflict")
    record(
        api,
        task_id,
        parse(events(counters=dict(COUNTERS, output_tokens=21))),
        "conflict",
    )
    readback = api.task(task_id)["execution_timing"]["usage"]
    assert readback["observed_total_tokens"] == 120
    assert readback["conflicting_threads"] == ["root"]


def test_receipts_cannot_assert_charges_or_unbound_attempts(canonical_controller):
    _, api, create, *_ = canonical_controller
    task_id = create()
    with pytest.raises(ValueError, match="existing execution"):
        record(api, task_id, parse(events()))
    start(api, task_id)
    with pytest.raises(ValueError, match="receipt fields"):
        record(
            api, task_id, dict(parse(events()), cost={"status": "actual", "amount": 1})
        )
    with pytest.raises(ValueError, match="identity"):
        record(api, task_id, dict(parse(events()), observed_model="gpt-6-astra"))
    with pytest.raises(PermissionError):
        timing.record_usage(
            task_id,
            attempt_id="first",
            actor_concept_id="#V#other",
            receipt=parse(events()),
        )


@pytest.mark.parametrize("accounting_unavailable", [False, True])
def test_dgx_supported_delivery_and_timing_independence(
    canonical_controller, monkeypatch, accounting_unavailable
):
    cfg, api, create, read_state, launches, fd = canonical_controller
    runner = Path(cfg["codex_command"])
    # Exercise stdout capture, process exit, result acceptance and messaging.
    script = runner.read_text().replace("'status': 'blocked'", "'status': 'completed'")
    runner.write_text(
        script
        + "\n"
        + "\n".join("print(" + repr(json.dumps(row)) + ")" for row in events())
    )
    if accounting_unavailable:

        def unavailable(*args, **kwargs):
            raise OSError("Fixture usage persistence unavailable")

        monkeypatch.setattr(timing, "record_usage", unavailable)
    task_id = create()
    worker.tick(cfg, api, fd)
    _, state = read_state(task_id)
    task = api.task(task_id)
    assert task["status"] == "completed"
    assert task["execution_timing"]["completed_at"]
    receipt = task["execution_timing"]
    if accounting_unavailable:
        assert receipt["usage"]["observed_tokens"] is None
        assert state["usage_recording_error_type"] == "OSError"
    else:
        attempt = receipt["attempts"][0]
        assert attempt["attempt_id"] == state["attempt"]
        assert attempt["usage"]["tokens"] == COUNTERS
        assert attempt["usage"]["thread_id"] == "root"
        assert receipt["usage"]["observed_total_tokens"] == 120
        # Replayed result acceptance (including lost acknowledgements) is idempotent.
        assert api.finish_execution(state)["execution_timing"] == receipt
    message = api.messages.get_message_for_user(
        state["message_id"], cfg["delegator_id"]
    )
    text = api.messages.project_direct_message(message)["content"]
    assert "subscription usage is not a per-task invoice" in text
    assert (
        "partial coverage" if not accounting_unavailable else "could not be recorded"
    ) in text
    worker.tick(cfg, api, fd)
    assert len(launches()) == 1
    assert api.task(task_id)["execution_timing"] == receipt
