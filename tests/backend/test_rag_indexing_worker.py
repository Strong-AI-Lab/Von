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
