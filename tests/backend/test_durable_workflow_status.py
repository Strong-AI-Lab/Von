from __future__ import annotations

from typing import Any

from src.backend.workflows.durable.models import WorkflowInstanceStatus
from src.backend.workflows.durable.startup import get_system_status


def test_get_system_status_matches_recognised_statuses_before_grouping(
    monkeypatch,
) -> None:
    """Status counts should use the existing status-leading index as a covered scan."""

    captured: dict[str, Any] = {}
    status_rows = [
        {"_id": status.value, "count": index}
        for index, status in enumerate(WorkflowInstanceStatus, start=1)
    ]
    status_rows.append({"_id": "legacy_unknown", "count": 99})

    class _InstancesCollection:
        def aggregate(self, pipeline):
            captured["pipeline"] = pipeline
            return status_rows

    class _CountCollection:
        def count_documents(self, _query):
            return 0

    collections = {
        "workflow_instances": _InstancesCollection(),
        "workflow_schedules": _CountCollection(),
        "workflow_workers": _CountCollection(),
    }

    class _Database:
        def __getitem__(self, collection_name):
            return collections[collection_name]

    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_db",
        lambda: _Database(),
    )

    result = get_system_status()

    recognised_statuses = [status.value for status in WorkflowInstanceStatus]
    assert captured["pipeline"] == [
        {"$match": {"status": {"$in": recognised_statuses}}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    assert result["instances"] == {
        "pending": 1,
        "running": 2,
        "completed": 3,
        "failed": 4,
        "cancelled": 5,
        "paused": 6,
    }
