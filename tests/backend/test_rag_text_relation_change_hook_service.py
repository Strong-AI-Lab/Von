from __future__ import annotations

import queue
import threading

import pytest

_QUEUE_FIELDS = {
    "mechanism": "in_memory_bounded_queue",
    "durable": False,
}


class _DeferredThread:
    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


def _install_deferred_worker_state(monkeypatch, service, *, queue_size=4):
    work_queue = queue.Queue(maxsize=queue_size)
    pending_keys = set()
    threads = []

    def _thread_factory(**kwargs):
        thread = _DeferredThread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(service, "_RAG_SYNC_QUEUE", work_queue)
    monkeypatch.setattr(service, "_RAG_SYNC_PENDING_KEYS", pending_keys)
    monkeypatch.setattr(service, "_RAG_SYNC_RERUN_KEYS", set())
    monkeypatch.setattr(service, "_RAG_SYNC_LOCK", threading.Lock())
    monkeypatch.setattr(service, "_RAG_SYNC_WORKER_THREAD", None)
    monkeypatch.setattr(service.threading, "Thread", _thread_factory)
    return work_queue, pending_keys, threads


def test_post_write_hook_schedules_without_running_rag_inline(monkeypatch):
    from src.backend.services import rag_text_relation_change_hook_service as service

    work_queue, pending_keys, threads = _install_deferred_worker_state(
        monkeypatch,
        service,
    )

    def _must_not_run_inline(**_kwargs):
        raise AssertionError("derived RAG maintenance ran on the primary write path")

    monkeypatch.setattr(
        service,
        "_run_concept_text_relation_rag_sync",
        _must_not_run_inline,
    )

    result = service.maybe_sync_concept_text_relations_to_rag(
        namespace=" #V#user@org ",
        concept_id=" #V#subject ",
        predicate=" hasDescription ",
    )

    assert result == {**_QUEUE_FIELDS, "success": True, "scheduled": True}
    assert len(threads) == 1
    assert threads[0].started is True
    assert threads[0].daemon is True
    assert work_queue.get_nowait() == {
        "namespace": "#V#user@org",
        "concept_id": "#V#subject",
        "predicate": "hasDescription",
        "scoped_assertion_id": None,
    }
    assert pending_keys == {
        ("#V#user@org", "#V#subject", "hasDescription", None),
    }


def test_post_write_hook_coalesces_duplicate_pending_refresh(monkeypatch):
    from src.backend.services import rag_text_relation_change_hook_service as service

    work_queue, _pending_keys, threads = _install_deferred_worker_state(
        monkeypatch,
        service,
    )

    first = service.maybe_sync_concept_text_relations_to_rag(
        namespace="#V#user@org",
        concept_id="#V#subject",
        predicate="hasDescription",
    )
    second = service.maybe_sync_concept_text_relations_to_rag(
        namespace="#V#user@org",
        concept_id="#V#subject",
        predicate="hasDescription",
    )

    assert first == {**_QUEUE_FIELDS, "success": True, "scheduled": True}
    assert second == {
        **_QUEUE_FIELDS,
        "success": True,
        "scheduled": False,
        "reason": "already_scheduled",
        "rerun_requested": True,
    }
    assert len(threads) == 1
    assert work_queue.qsize() == 1
    assert service._RAG_SYNC_RERUN_KEYS == {
        ("#V#user@org", "#V#subject", "hasDescription", None),
    }


def test_post_write_hook_reports_queue_saturation_without_raising(monkeypatch):
    from src.backend.services import rag_text_relation_change_hook_service as service

    work_queue, pending_keys, _threads = _install_deferred_worker_state(
        monkeypatch,
        service,
        queue_size=1,
    )
    work_queue.put_nowait(
        {
            "namespace": "#V#other@org",
            "concept_id": "#V#other",
            "predicate": None,
            "scoped_assertion_id": None,
        }
    )

    result = service.maybe_sync_concept_text_relations_to_rag(
        namespace="#V#user@org",
        concept_id="#V#subject",
    )

    assert result == {
        **_QUEUE_FIELDS,
        "success": False,
        "scheduled": False,
        "skipped": False,
        "reason": "queue_full",
    }
    assert ("#V#user@org", "#V#subject", None, None) not in pending_keys


def test_background_worker_executes_job_and_releases_pending_key(monkeypatch):
    from src.backend.services import rag_text_relation_change_hook_service as service

    job = {
        "namespace": "#V#user@org",
        "concept_id": "#V#subject",
        "predicate": "hasDescription",
        "scoped_assertion_id": None,
    }

    class _OneJobQueue:
        def __init__(self):
            self.returned = False
            self.task_done_calls = 0

        def get(self):
            if self.returned:
                raise StopIteration
            self.returned = True
            return job

        def task_done(self):
            self.task_done_calls += 1

    work_queue = _OneJobQueue()
    pending_key = ("#V#user@org", "#V#subject", "hasDescription", None)
    calls = []
    monkeypatch.setattr(service, "_RAG_SYNC_QUEUE", work_queue)
    monkeypatch.setattr(service, "_RAG_SYNC_PENDING_KEYS", {pending_key})
    monkeypatch.setattr(service, "_RAG_SYNC_RERUN_KEYS", set())
    monkeypatch.setattr(service, "_RAG_SYNC_LOCK", threading.Lock())
    monkeypatch.setattr(
        service,
        "_run_concept_text_relation_rag_sync",
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )

    with pytest.raises(StopIteration):
        service._concept_text_relation_rag_sync_worker()

    assert calls == [
        {
            "namespace": "#V#user@org",
            "concept_id": "#V#subject",
            "predicate": "hasDescription",
            "scoped_assertion_id": None,
        }
    ]
    assert service._RAG_SYNC_PENDING_KEYS == set()
    assert work_queue.task_done_calls == 1


def test_background_worker_requeues_once_when_write_arrives_during_refresh(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_change_hook_service as service

    job = {
        "namespace": "#V#user@org",
        "concept_id": "#V#subject",
        "predicate": "hasDescription",
        "scoped_assertion_id": None,
    }

    class _OneJobQueue:
        def __init__(self):
            self.returned = False
            self.requeued = []

        def get(self):
            if self.returned:
                raise StopIteration
            self.returned = True
            return job

        def put_nowait(self, value):
            self.requeued.append(value)

        def task_done(self):
            return None

    work_queue = _OneJobQueue()
    pending_key = ("#V#user@org", "#V#subject", "hasDescription", None)
    monkeypatch.setattr(service, "_RAG_SYNC_QUEUE", work_queue)
    monkeypatch.setattr(service, "_RAG_SYNC_PENDING_KEYS", {pending_key})
    monkeypatch.setattr(service, "_RAG_SYNC_RERUN_KEYS", {pending_key})
    monkeypatch.setattr(service, "_RAG_SYNC_LOCK", threading.Lock())
    monkeypatch.setattr(
        service,
        "_run_concept_text_relation_rag_sync",
        lambda **_kwargs: {"success": True},
    )

    with pytest.raises(StopIteration):
        service._concept_text_relation_rag_sync_worker()

    assert work_queue.requeued == [job]
    assert service._RAG_SYNC_PENDING_KEYS == {pending_key}
    assert service._RAG_SYNC_RERUN_KEYS == set()


def test_exact_scoped_refresh_does_not_spend_budget_on_base_rows(monkeypatch):
    from src.backend.services import rag_text_relation_change_hook_service as service
    from src.backend.services import rag_text_relation_sync_service as sync_service

    calls = []
    monkeypatch.setattr(
        sync_service,
        "sync_scoped_assertions_to_rag",
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )
    monkeypatch.setattr(
        sync_service,
        "sync_text_relations_to_rag",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact scoped refresh must not scan base rows")
        ),
    )

    result = service._run_concept_text_relation_rag_sync(
        namespace="#V#user@org",
        concept_id="#V#subject",
        predicate="hasDescription",
        scoped_assertion_id="ska_exact",
    )

    assert result == {"success": True}
    assert calls == [
        {
            "namespace": "#V#user@org",
            "assertion_ids": ["ska_exact"],
        }
    ]
