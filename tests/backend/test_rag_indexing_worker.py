from __future__ import annotations

import logging

from src.backend.utilities import rag_indexing_worker as worker


class _FakeCollection:
    def count_documents(self, _query):
        return 0


def test_startup_diagnostics_logs_only_sanitized_mongo_location(
    monkeypatch,
    caplog,
) -> None:
    raw_uri = (
        "mongodb+srv://appuser:s3cr3t-pass@cluster0.example.mongodb.net/"
        "von_db?authSource=admin&appName=VonSecret&tls=true&retryWrites=true"
    )
    safe_uri = "mongodb+srv://cluster0.example.mongodb.net"
    monkeypatch.setattr(
        worker,
        "health_summary",
        lambda: {
            "connected": True,
            "using_fallback": False,
            "effective_uri": raw_uri,
            "effective_uri_sanitized": safe_uri,
        },
    )
    monkeypatch.setattr(
        worker,
        "get_interaction_sessions_collection",
        lambda: _FakeCollection(),
    )

    caplog.set_level(logging.INFO, logger="rag_worker")
    worker.startup_diagnostics()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert safe_uri in log_text
    for fragment in (
        "appuser",
        "s3cr3t-pass",
        "admin",
        "authSource",
        "appName",
        "VonSecret",
        "tls=true",
        "retryWrites",
        "von_db",
        raw_uri,
    ):
        assert fragment not in log_text


def test_worker_iteration_runs_bounded_text_assertion_queue(monkeypatch) -> None:
    from src.backend.services import knowledge_assertion_rag_service as service

    calls = []
    monkeypatch.setattr(
        service,
        "reconcile_assertion_rag_state",
        lambda *, limit: calls.append(("reconcile", limit)) or {"repaired": 0},
    )
    monkeypatch.setattr(
        service,
        "process_pending_assertion_rag_jobs",
        lambda *, limit, worker_id: calls.append(
            ("process", limit, worker_id)
        )
        or {
            "claimed": 2,
            "succeeded": 1,
            "failed": 1,
            "superseded": 0,
        },
    )

    assert worker.process_pending_text_assertions() == 1
    assert calls[0] == ("reconcile", worker.BATCH_SIZE)
    assert calls[1][0:2] == ("process", worker.BATCH_SIZE)
    assert calls[1][2].startswith("rag-index-worker:")
