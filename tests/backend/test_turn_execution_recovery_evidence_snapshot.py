from __future__ import annotations

from src.backend.workflows.durable.turn_execution_runtime_support import (
    _bounded_snapshot,
)


def test_bounded_snapshot_retains_salient_scalar_mapping_beyond_list_window() -> None:
    payload = {
        "records": [
            {"name": f"noise-{index}", "value": f"ignored-{index}"}
            for index in range(8)
        ]
        + [
            {"name": "title", "value": "Quarterly evidence report"},
            {"name": "status", "value": "ready"},
        ]
    }

    snapshot = _bounded_snapshot(payload, max_depth=2, max_items=3)

    records = snapshot["records"]
    assert {row.get("value") for row in records if isinstance(row, dict)} >= {
        "Quarterly evidence report",
        "ready",
    }
    assert all(
        row != {"_truncated": "mapping"}
        for row in records
        if isinstance(row, dict)
    )
