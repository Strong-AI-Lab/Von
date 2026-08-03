from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Any

from src.backend.workflows.durable.durable_executor import (
    _ActivityProjectionCoalescer,
    _durable_activity_event_requires_checkpoint,
)


def _projection(completed_count: int, total: int = 42) -> dict[str, Any]:
    return {
        "schema_version": "workflow_activity_projection.v1",
        "instance_id": "parallel-for-each-42",
        "status": "running",
        "progress": {
            "current": completed_count,
            "total": total,
            "message": f"Completed {completed_count} of {total} items",
        },
        "work_items": [
            {
                "index": index,
                "status": "completed" if index < completed_count else "running",
            }
            for index in range(total)
        ],
    }


def test_42_for_each_completions_have_logarithmic_durable_checkpoints() -> None:
    checkpoints = [
        completed_count
        for completed_count in range(1, 43)
        if _durable_activity_event_requires_checkpoint(
            event_status="workflow_for_each_item_completed",
            progress_current=completed_count,
            progress_total=42,
        )
    ]

    assert checkpoints == [1, 2, 4, 8, 16, 32, 42]
    assert not _durable_activity_event_requires_checkpoint(
        event_status="workflow_for_each_item_start",
        progress_current=32,
        progress_total=42,
    )

    writes: list[dict[str, Any]] = []
    coalescer = _ActivityProjectionCoalescer(
        lambda snapshot: writes.append(dict(snapshot)),
        flush_interval_seconds=60.0,
    )
    coalescer.observe(_projection(0), sequence=0)
    for completed_count in range(1, 43):
        coalescer.observe(
            _projection(completed_count - 1),
            sequence=(completed_count * 2) - 1,
        )
        coalescer.observe(
            _projection(completed_count),
            force_checkpoint=(completed_count in checkpoints),
            sequence=completed_count * 2,
        )
    coalescer.flush()

    assert [write["progress"]["current"] for write in writes] == [
        0,
        1,
        2,
        4,
        8,
        16,
        32,
        42,
    ]


def test_parallel_for_each_callbacks_coalesce_while_mongo_write_is_blocked() -> None:
    writer_started = Event()
    release_writer = Event()
    writes_lock = Lock()
    writes: list[dict[str, Any]] = []

    def blocked_writer(snapshot: Mapping[str, Any]) -> None:
        with writes_lock:
            writes.append(dict(snapshot))
            write_number = len(writes)
        if write_number == 1:
            writer_started.set()
            assert release_writer.wait(timeout=5.0)

    coalescer = _ActivityProjectionCoalescer(
        blocked_writer,
        flush_interval_seconds=60.0,
    )

    with ThreadPoolExecutor(max_workers=12) as executor:
        first_write = executor.submit(coalescer.observe, _projection(0), sequence=0)
        assert writer_started.wait(timeout=2.0)

        callbacks = []
        for completed_count in range(1, 43):
            callbacks.append(
                executor.submit(
                    coalescer.observe,
                    _projection(completed_count - 1),
                    force_checkpoint=False,
                    sequence=(completed_count * 2) - 1,
                )
            )
            callbacks.append(
                executor.submit(
                    coalescer.observe,
                    _projection(completed_count),
                    force_checkpoint=_durable_activity_event_requires_checkpoint(
                        event_status="workflow_for_each_item_completed",
                        progress_current=completed_count,
                        progress_total=42,
                    ),
                    sequence=completed_count * 2,
                )
            )

        # Every callback can finish while the first simulated Mongo write is
        # blocked; none waits behind the coalescer's state lock.
        for callback in callbacks:
            callback.result(timeout=2.0)

        release_writer.set()
        first_write.result(timeout=2.0)

    coalescer.flush()

    assert len(writes) <= 2
    final_projection = writes[-1]
    assert final_projection["progress"]["current"] == 42
    assert len(final_projection["work_items"]) == 42
    assert all(item["status"] == "completed" for item in final_projection["work_items"])
