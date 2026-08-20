from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock


class _StubRag:
    def __init__(self, *, fail_upserts: int = 0, deleted: int = 1) -> None:
        self.fail_upserts = fail_upserts
        self.deleted = deleted
        self.upserts: list[tuple[list[dict], str | None]] = []
        self.deletes: list[tuple[list[str], str | None]] = []

    def upsert_documents(self, docs, *, namespace=None, **_kwargs):
        payload = list(docs)
        self.upserts.append((payload, namespace))
        if self.fail_upserts:
            self.fail_upserts -= 1
            raise RuntimeError("temporary vector failure")
        return len(payload), 0

    def delete_documents(self, ids, *, namespace=None):
        payload = list(ids)
        self.deletes.append((payload, namespace))
        return self.deleted


def _collection():
    return mongomock.MongoClient()["von_test"]["scoped_knowledge_assertions"]


def _store(monkeypatch, collection, *, text="  Exact raw assertion.\n"):
    from src.backend.services import scoped_assertion_service

    monkeypatch.setattr(
        scoped_assertion_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    return scoped_assertion_service.store_text_assertion(
        text=text,
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
        turn_id="turn-rag",
    )


def test_pending_raw_assertion_indexes_exact_body_and_completes(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as service

    collection = _collection()
    stored = _store(monkeypatch, collection)
    rag = _StubRag()
    now = datetime(2030, 8, 20, 12, tzinfo=timezone.utc)

    result = service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="worker-one",
        rag_service=rag,
        now=now,
        collection=collection,
    )

    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert len(rag.upserts) == 1
    payload, namespace = rag.upserts[0]
    assert namespace == "#V#member@trusted_org"
    assert payload[0]["id"] == f"scoped_assertion:{stored['assertion_id']}"
    assert payload[0]["text"] == "  Exact raw assertion.\n"
    assert payload[0]["metadata"]["row_kind"] == "text_assertion"
    assert payload[0]["metadata"]["subject_concept_id"] is None
    assert payload[0]["metadata"]["assertion_revision"] == 1
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert persisted["rag_index"]["status"] == "indexed"
    assert persisted["rag_index"]["indexed_revision"] == 1


def test_rag_failure_does_not_undo_assertion_and_is_retryable(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as service

    collection = _collection()
    stored = _store(monkeypatch, collection)
    rag = _StubRag(fail_upserts=1)
    first_time = datetime(2030, 8, 20, 12, tzinfo=timezone.utc)

    failed = service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="worker-one",
        rag_service=rag,
        now=first_time,
        collection=collection,
    )
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert failed["failed"] == 1
    assert persisted["status"] == "asserted"
    assert persisted["object_text"]["text"] == "  Exact raw assertion.\n"
    assert persisted["rag_index"]["status"] == "failed"
    assert persisted["rag_index"]["last_error_code"] == "RuntimeError"

    retried = service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="worker-two",
        rag_service=rag,
        now=first_time + timedelta(seconds=10),
        collection=collection,
    )
    assert retried["succeeded"] == 1
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert persisted["rag_index"]["status"] == "indexed"
    assert persisted["rag_index"]["attempt_count"] == 2


def test_expired_claim_is_reclaimed_with_a_new_lease(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as service

    collection = _collection()
    _store(monkeypatch, collection)
    now = datetime(2030, 8, 20, 12, tzinfo=timezone.utc)
    first = service.claim_next_assertion_rag_job(
        worker_id="worker-one",
        now=now,
        lease_seconds=5,
        collection=collection,
    )
    assert first is not None
    assert (
        service.claim_next_assertion_rag_job(
            worker_id="worker-two",
            now=now + timedelta(seconds=4),
            lease_seconds=5,
            collection=collection,
        )
        is None
    )
    reclaimed = service.claim_next_assertion_rag_job(
        worker_id="worker-two",
        now=now + timedelta(seconds=6),
        lease_seconds=5,
        collection=collection,
    )
    assert reclaimed is not None
    assert reclaimed["rag_index"]["lease_token"] != first["rag_index"]["lease_token"]
    assert reclaimed["rag_index"]["attempt_count"] == 2


def test_old_upsert_completion_cannot_overwrite_retraction(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as rag_service
    from src.backend.services import scoped_assertion_service

    collection = _collection()
    stored = _store(monkeypatch, collection)
    now = datetime(2030, 8, 20, 12, tzinfo=timezone.utc)
    claimed = rag_service.claim_next_assertion_rag_job(
        worker_id="old-worker",
        now=now,
        collection=collection,
    )
    assert claimed is not None

    scoped_assertion_service.retract_scoped_assertion(
        assertion_id=stored["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    rag = _StubRag()
    old_result = rag_service.process_claimed_assertion_rag_job(
        claimed,
        rag_service=rag,
        now=now + timedelta(seconds=1),
        collection=collection,
    )
    assert old_result["superseded"] is True
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert persisted["status"] == "retracted"
    assert persisted["assertion_revision"] == 2
    assert persisted["rag_index"]["status"] == "pending"
    assert persisted["rag_index"]["desired_operation"] == "delete"

    deleted = rag_service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="delete-worker",
        rag_service=rag,
        now=now + timedelta(seconds=2),
        collection=collection,
    )
    assert deleted["succeeded"] == 1
    assert rag.deletes == [
        ([f"scoped_assertion:{stored['assertion_id']}"], "#V#member@trusted_org")
    ]
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert persisted["rag_index"]["status"] == "absent"
    assert persisted["rag_index"]["indexed_revision"] == 2


def test_stale_upsert_after_completed_delete_requeues_current_delete(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as rag_service
    from src.backend.services import scoped_assertion_service

    collection = _collection()
    stored = _store(monkeypatch, collection)
    now = datetime(2030, 8, 20, 12, tzinfo=timezone.utc)
    stale_upsert = rag_service.claim_next_assertion_rag_job(
        worker_id="stale-upsert",
        now=now,
        collection=collection,
    )
    scoped_assertion_service.retract_scoped_assertion(
        assertion_id=stored["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    rag = _StubRag()
    first_delete = rag_service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="delete-first",
        rag_service=rag,
        now=now + timedelta(seconds=1),
        collection=collection,
    )
    assert first_delete["succeeded"] == 1
    assert (
        collection.find_one({"assertion_id": stored["assertion_id"]})["rag_index"][
            "status"
        ]
        == "absent"
    )

    stale_result = rag_service.process_claimed_assertion_rag_job(
        stale_upsert,
        rag_service=rag,
        now=now + timedelta(seconds=2),
        collection=collection,
    )
    assert stale_result["superseded"] is True
    assert stale_result["current_revision_requeued"] is True
    pending = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert pending["rag_index"]["status"] == "pending"
    assert pending["rag_index"]["desired_operation"] == "delete"
    assert pending["rag_index"]["desired_revision"] == 2

    second_delete = rag_service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="delete-again",
        rag_service=rag,
        now=now + timedelta(seconds=3),
        collection=collection,
    )
    assert second_delete["succeeded"] == 1
    assert len(rag.deletes) == 2
    assert (
        collection.find_one({"assertion_id": stored["assertion_id"]})["rag_index"][
            "status"
        ]
        == "absent"
    )


def test_delete_zero_is_idempotent_desired_absence(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as rag_service
    from src.backend.services import scoped_assertion_service

    collection = _collection()
    stored = _store(monkeypatch, collection)
    scoped_assertion_service.retract_scoped_assertion(
        assertion_id=stored["assertion_id"],
        acting_user_concept_id="#V#member",
        organisation_concept_id="#V#trusted_org",
    )
    result = rag_service.process_pending_assertion_rag_jobs(
        limit=1,
        worker_id="delete-worker",
        rag_service=_StubRag(deleted=0),
        collection=collection,
    )
    assert result["succeeded"] == 1
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert persisted["rag_index"]["status"] == "absent"


def test_reconciler_repairs_missing_materialised_job(monkeypatch):
    from src.backend.services import knowledge_assertion_rag_service as service

    collection = _collection()
    stored = _store(monkeypatch, collection)
    collection.update_one(
        {"assertion_id": stored["assertion_id"]},
        {"$unset": {"rag_index": ""}},
    )
    result = service.reconcile_assertion_rag_state(
        collection=collection,
        now=datetime(2030, 8, 20, 12, tzinfo=timezone.utc),
    )
    assert result["repaired"] == 1
    persisted = collection.find_one({"assertion_id": stored["assertion_id"]})
    assert persisted["rag_index"]["status"] == "pending"
    assert persisted["rag_index"]["desired_revision"] == 1


def test_reconciler_queries_anomalies_instead_of_starving_behind_healthy_rows():
    from src.backend.services import knowledge_assertion_rag_service as service

    collection = _collection()
    old = datetime(2030, 8, 19, 12, tzinfo=timezone.utc)
    healthy = [
        {
            "assertion_id": f"ska_healthy_{index}",
            "assertion_form": "standalone_text",
            "assertion_revision": 1,
            "object_kind": "text",
            "status": "asserted",
            "updated_at": old,
            "rag_index": {
                "status": "indexed",
                "desired_operation": "upsert",
                "desired_revision": 1,
                "indexed_revision": 1,
                "namespace": "#V#member",
            },
        }
        for index in range(100)
    ]
    collection.insert_many(healthy)
    collection.insert_one(
        {
            "assertion_id": "ska_later_missing",
            "assertion_form": "standalone_text",
            "assertion_revision": 4,
            "object_kind": "text",
            "status": "asserted",
            "updated_at": old + timedelta(days=1),
            "scope": {"namespace": "#V#member"},
        }
    )

    result = service.reconcile_assertion_rag_state(
        collection=collection,
        now=old + timedelta(days=2),
        limit=10,
    )

    assert result["scanned"] == 1
    assert result["repaired"] == 1
    repaired = collection.find_one({"assertion_id": "ska_later_missing"})
    assert repaired["rag_index"]["status"] == "pending"
    assert repaired["rag_index"]["desired_revision"] == 4


def test_reconciler_repairs_unclaimable_and_stale_terminal_states():
    from src.backend.services import knowledge_assertion_rag_service as service

    collection = _collection()
    now = datetime(2030, 8, 20, 12, tzinfo=timezone.utc)
    base = {
        "assertion_form": "standalone_text",
        "assertion_revision": 3,
        "object_kind": "text",
        "status": "asserted",
        "updated_at": now,
    }
    collection.insert_many(
        [
            {
                **base,
                "assertion_id": "ska_bad_lease",
                "rag_index": {
                    "status": "processing",
                    "desired_operation": "upsert",
                    "desired_revision": 3,
                    "namespace": "#V#member",
                    "lease_expires_at": "not-a-date",
                },
            },
            {
                **base,
                "assertion_id": "ska_stale_terminal",
                "rag_index": {
                    "status": "indexed",
                    "desired_operation": "upsert",
                    "desired_revision": 3,
                    "indexed_revision": 2,
                    "namespace": "#V#member",
                },
            },
            {
                **base,
                "assertion_id": "ska_unknown_status",
                "rag_index": {
                    "status": "bogus",
                    "desired_operation": "upsert",
                    "desired_revision": 3,
                    "namespace": "#V#member",
                },
            },
        ]
    )

    result = service.reconcile_assertion_rag_state(
        collection=collection,
        now=now,
    )

    assert result["scanned"] == 3
    assert result["repaired"] == 3
    assert {
        row["rag_index"]["status"]
        for row in collection.find(
            {
                "assertion_id": {
                    "$in": [
                        "ska_bad_lease",
                        "ska_stale_terminal",
                        "ska_unknown_status",
                    ]
                }
            }
        )
    } == {"pending"}
