from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.utilities.reindex_text_relations import ReindexArgs, parse_args, run_reindex
from src.backend.services.rag_text_relation_sync_service import TextRelationRagDoc


def test_parse_args_accepts_namespace_directly():
    args = parse_args(["--namespace", "#V#u@#V#org", "--dry-run"])
    assert args.namespace == "#V#u@#V#org"
    assert args.dry_run is True


def test_parse_args_builds_namespace_from_user_and_org():
    args = parse_args(["--user", "#V#u", "--org", "org", "--dry-run"])
    assert args.namespace == "#V#u@org"


def test_parse_args_splits_repeatable_lists_and_csv():
    args = parse_args(
        [
            "--namespace",
            "#V#u@#V#org",
            "--predicate",
            "hasDescription,hasName",
            "--predicate",
            "hasContent",
            "--language",
            "en-NZ,fr",
            "--concept-id",
            "#V#c1,#V#c2",
            "--dry-run",
        ]
    )
    assert args.predicates == ["hasDescription", "hasName", "hasContent"]
    assert args.languages == ["en-NZ", "fr"]
    assert args.concept_ids == ["#V#c1", "#V#c2"]


def test_parse_args_parses_since_date_to_utc_midnight():
    args = parse_args(
        ["--namespace", "#V#u@#V#org", "--since", "2025-12-01", "--dry-run"]
    )
    assert isinstance(args.updated_since, datetime)
    assert args.updated_since.tzinfo is not None
    assert args.updated_since == datetime(2025, 12, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_run_reindex_dry_run_does_not_call_rag(monkeypatch: pytest.MonkeyPatch):
    from src.utilities import reindex_text_relations as mod

    def _boom():
        raise AssertionError("get_rag_service should not be called in dry-run")

    monkeypatch.setattr(mod, "get_rag_service", _boom)
    monkeypatch.setattr(mod.TextRelationsRepository, "count_documents", lambda _f: 0)
    monkeypatch.setattr(
        mod, "collect_text_relation_docs_for_namespace", lambda **_k: []
    )

    args = ReindexArgs(
        namespace="#V#u@#V#org",
        predicates=None,
        languages=None,
        concept_ids=None,
        updated_since=None,
        dry_run=True,
        page_size=10,
        batch_size=2,
        scan_limit=0,
    )

    assert run_reindex(args) == 0


def test_run_reindex_upserts_documents(monkeypatch: pytest.MonkeyPatch):
    from src.utilities import reindex_text_relations as mod

    calls = {"upserts": 0, "namespace": None, "count": 0}

    class _FakeRag:
        def upsert_documents(self, docs, namespace: str):
            calls["upserts"] += 1
            calls["namespace"] = namespace
            calls["count"] += len(docs)
            return len(docs), 0

    monkeypatch.setattr(mod, "get_rag_service", lambda: _FakeRag())
    monkeypatch.setattr(mod.TextRelationsRepository, "count_documents", lambda _f: 1)

    doc = TextRelationRagDoc(
        doc_id="text_relation:1", text="hello", metadata={"k": "v"}
    )
    monkeypatch.setattr(
        mod, "collect_text_relation_docs_for_namespace", lambda **_k: [doc]
    )

    args = ReindexArgs(
        namespace="#V#u@#V#org",
        predicates=None,
        languages=None,
        concept_ids=None,
        updated_since=None,
        dry_run=False,
        page_size=1,
        batch_size=10,
        scan_limit=1,
    )

    assert run_reindex(args) == 0
    assert calls["upserts"] == 1
    assert calls["namespace"] == "#V#u@#V#org"
    assert calls["count"] == 1
