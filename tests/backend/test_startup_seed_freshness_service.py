from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import startup_seed_freshness_service as freshness


class _FakeChangeStream:
    def __init__(
        self,
        *,
        events: list[Mapping[str, Any]] | None = None,
        resume_token: Mapping[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._events = list(events or [])
        self.resume_token = dict(resume_token or {"_data": "current"})
        self._error = error
        self.closed = False

    def try_next(self):
        if self._error is not None:
            raise self._error
        if self._events:
            return self._events.pop(0)
        return None

    def close(self) -> None:
        self.closed = True


class _FakeDatabase:
    name = "von_db"

    def __init__(self, streams: list[_FakeChangeStream]) -> None:
        self._streams = list(streams)
        self.watch_calls: list[dict[str, Any]] = []

    def command(self, name: str) -> dict[str, Any]:
        assert name == "hello"
        return {"operationTime": "operation-time"}

    def watch(self, pipeline, **kwargs):
        self.watch_calls.append({"pipeline": pipeline, "kwargs": kwargs})
        if not self._streams:
            raise AssertionError("unexpected change-stream call")
        return self._streams.pop(0)


@pytest.fixture
def receipt_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    receipt_path = tmp_path / "freshness_receipts.json"
    monkeypatch.setenv("VON_STARTUP_SEED_FRESHNESS_RECEIPT_PATH", str(receipt_path))
    monkeypatch.setenv("VON_STARTUP_SEED_FRESHNESS_RECEIPTS_ENABLED", "1")
    monkeypatch.setattr(
        freshness,
        "_current_authority_fingerprint",
        lambda _db: "authority-fingerprint",
    )
    return receipt_path


def _record(
    monkeypatch: pytest.MonkeyPatch,
    db: _FakeDatabase,
) -> dict[str, Any]:
    monkeypatch.setattr(freshness, "_get_db", lambda: db)
    observation = freshness.begin_startup_seed_freshness_observation()
    assert observation["success"] is True
    return freshness.record_startup_seed_freshness(
        family_id="test_family",
        source_digest="source-digest",
        producer_schema_version="producer.v1",
        concept_ids=["#V#subject"],
        text_relation_subject_ids=["#V#subject"],
        text_relation_predicates=["#V#has_config"],
        tracked_concept_document_ids=["concept-document"],
        tracked_text_relation_document_ids=["relation-document"],
        tracked_text_value_document_ids=["text-document"],
        observation=observation,
        metadata={"counts": {"items": 1}},
    )


def _check(monkeypatch: pytest.MonkeyPatch, db: _FakeDatabase) -> dict[str, Any]:
    monkeypatch.setattr(freshness, "_get_db", lambda: db)
    return freshness.check_startup_seed_freshness(
        family_id="test_family",
        source_digest="source-digest",
        producer_schema_version="producer.v1",
        concept_ids=["#V#subject"],
        text_relation_subject_ids=["#V#subject"],
        text_relation_predicates=["#V#has_config"],
    )


def test_receipt_round_trip_skips_unrelated_changes(
    receipt_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDatabase(
        [
            _FakeChangeStream(resume_token={"_data": "recorded"}),
            _FakeChangeStream(
                events=[
                    {
                        "operationType": "update",
                        "ns": {"coll": "concepts"},
                        "documentKey": {"_id": "unrelated-document"},
                        "fullDocument": {"concept_id": "#V#unrelated"},
                    }
                ],
                resume_token={"_data": "advanced"},
            ),
        ]
    )

    recorded = _record(monkeypatch, db)
    checked = _check(monkeypatch, db)

    assert recorded["persisted"] is True
    assert checked["fresh"] is True
    assert checked["reason"] == "dependency_receipt_current"
    assert checked["events_examined"] == 1
    assert checked["metadata"] == {"counts": {"items": 1}}
    assert receipt_environment.exists()
    assert "resume_token" not in str(checked)
    assert db.watch_calls[0]["kwargs"]["start_at_operation_time"] == ("operation-time")
    assert "resume_after" in db.watch_calls[1]["kwargs"]


@pytest.mark.parametrize(
    "event",
    [
        {
            "operationType": "update",
            "ns": {"coll": "concepts"},
            "documentKey": {"_id": "other"},
            "fullDocument": {"concept_id": "#V#subject"},
        },
        {
            "operationType": "delete",
            "ns": {"coll": "concepts"},
            "documentKey": {"_id": "concept-document"},
        },
        {
            "operationType": "insert",
            "ns": {"coll": "text_relations"},
            "documentKey": {"_id": "new-relation"},
            "fullDocument": {
                "subject_concept_id": "#V#subject",
                "predicate": "#V#has_config",
            },
        },
        {
            "operationType": "delete",
            "ns": {"coll": "text_relations"},
            "documentKey": {"_id": "relation-document"},
        },
        {
            "operationType": "delete",
            "ns": {"coll": "text_values"},
            "documentKey": {"_id": "text-document"},
        },
        {
            "operationType": "drop",
            "ns": {"coll": "concepts"},
        },
    ],
)
def test_relevant_change_or_deletion_invalidates_receipt(
    receipt_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: Mapping[str, Any],
) -> None:
    db = _FakeDatabase(
        [
            _FakeChangeStream(resume_token={"_data": "recorded"}),
            _FakeChangeStream(
                events=[event],
                resume_token={"_data": "changed"},
            ),
        ]
    )

    assert _record(monkeypatch, db)["persisted"] is True
    checked = _check(monkeypatch, db)

    assert checked["fresh"] is False
    assert checked["reason"] == "relevant_dependency_change_observed"
    assert checked["relevant_events"] == 1


def test_indeterminate_event_falls_back_instead_of_claiming_fresh(
    receipt_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDatabase(
        [
            _FakeChangeStream(resume_token={"_data": "recorded"}),
            _FakeChangeStream(
                events=[
                    {
                        "operationType": "update",
                        "ns": {"coll": "text_relations"},
                        "documentKey": {"_id": "unknown"},
                        "fullDocument": None,
                    }
                ],
                resume_token={"_data": "indeterminate"},
            ),
        ]
    )

    assert _record(monkeypatch, db)["persisted"] is True
    checked = _check(monkeypatch, db)

    assert checked["fresh"] is False
    assert checked["reason"] == "change_stream_event_indeterminate"
    assert checked["indeterminate_events"] == 1


def test_unresumable_stream_falls_back(
    receipt_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDatabase(
        [
            _FakeChangeStream(resume_token={"_data": "recorded"}),
            _FakeChangeStream(error=RuntimeError("expired token")),
        ]
    )

    assert _record(monkeypatch, db)["persisted"] is True
    checked = _check(monkeypatch, db)

    assert checked["fresh"] is False
    assert checked["reason"] == "change_stream_unavailable_or_unresumable"
    assert checked["error_type"] == "RuntimeError"


def test_receipt_is_not_published_when_dependency_changes_during_verification(
    receipt_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDatabase(
        [
            _FakeChangeStream(
                events=[
                    {
                        "operationType": "delete",
                        "ns": {"coll": "concepts"},
                        "documentKey": {"_id": "concept-document"},
                    }
                ],
                resume_token={"_data": "changed-during-scan"},
            )
        ]
    )

    recorded = _record(monkeypatch, db)

    assert recorded["persisted"] is False
    assert recorded["reason"] == "relevant_dependency_change_observed"
    assert not receipt_environment.exists()


def test_source_change_invalidates_without_consuming_old_checkpoint(
    receipt_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDatabase([_FakeChangeStream(resume_token={"_data": "recorded"})])
    assert _record(monkeypatch, db)["persisted"] is True

    changed = freshness.check_startup_seed_freshness(
        family_id="test_family",
        source_digest="changed-source-digest",
        producer_schema_version="producer.v1",
        concept_ids=["#V#subject"],
        text_relation_subject_ids=["#V#subject"],
        text_relation_predicates=["#V#has_config"],
    )

    assert changed["fresh"] is False
    assert changed["reason"] == "source_digest_mismatch"
    assert len(db.watch_calls) == 1


def test_public_result_never_exposes_opaque_checkpoint_values() -> None:
    public = freshness.public_startup_seed_freshness_result(
        {
            "fresh": True,
            "reason": "current",
            "_resume_token": {"_data": "opaque"},
            "_start_at_operation_time": "opaque-time",
        }
    )

    assert public == {"fresh": True, "reason": "current"}
