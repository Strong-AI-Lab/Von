from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import registered_tool_capability_retrieval_service as service


class _FakeCapabilityRAG:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.documents: dict[str, dict[str, Any]] = {}
        self.reset_count = 0
        self.query_calls: list[dict[str, Any]] = []

    def _namespace_persist_dir(self, namespace: str) -> str:
        return str(self.root / namespace)

    def get_namespace_runtime_state(self, namespace: str) -> dict[str, Any]:
        return {
            "namespace": namespace,
            "compatible": bool(self.documents),
            "status": "compatible" if self.documents else "missing_index",
        }

    def reset_namespace(self, namespace: str) -> None:
        self.reset_count += 1
        self.documents = {}

    def upsert_documents(
        self,
        docs,
        *,
        namespace: str,
        allow_partial_failures: bool,
    ) -> tuple[int, int]:
        rows = list(docs)
        self.documents = {str(row["id"]): dict(row) for row in rows}
        return len(rows), 0

    def query(
        self,
        query_text: str,
        *,
        top_k: int,
        namespace: str,
        hybrid: bool,
        permissions_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self.query_calls.append(
            {
                "query_text": query_text,
                "top_k": top_k,
                "namespace": namespace,
                "hybrid": hybrid,
                "permissions_context": dict(permissions_context),
            }
        )
        query_tokens = set(re.findall(r"[a-z0-9]+", query_text.lower()))
        results: list[dict[str, Any]] = []
        for row in self.documents.values():
            text_tokens = set(
                re.findall(r"[a-z0-9]+", str(row.get("text") or "").lower())
            )
            overlap = len(query_tokens & text_tokens)
            if overlap <= 0:
                continue
            results.append(
                {
                    "id": row["id"],
                    "metadata": dict(row.get("metadata") or {}),
                    "score": overlap / max(1, len(query_tokens)),
                }
            )
        results.sort(
            key=lambda row: (
                -float(row["score"]),
                str(row["metadata"].get("capability_name") or ""),
            )
        )
        return results[:top_k]


@pytest.fixture(autouse=True)
def _reset_process_state() -> None:
    service.reset_registered_tool_capability_index_state()
    yield
    service.reset_registered_tool_capability_index_state()


def _candidates() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_concepts",
            "description": "Discover represented predicates and types.",
            "planner_hint": "Use for schema discovery before a relation lookup.",
            "evidence_surface_family": "knowledge_base",
        },
        {
            "name": "find_relations_with_argument",
            "description": "Find relationships for an anchor entity.",
            "planner_hint": "Use for entity-relative relation-bearing evidence.",
            "evidence_surface_family": "knowledge_base",
        },
        {
            "name": "unrelated_admin_read",
            "description": "Inspect server configuration.",
            "evidence_surface_family": "operations",
        },
    ]


def test_retrieval_builds_once_and_returns_only_supplied_candidate_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rag = _FakeCapabilityRAG(tmp_path)
    monkeypatch.setattr(service, "_get_capability_rag_service", lambda: rag)

    scores, diagnostics = service.retrieve_registered_tool_capability_scores(
        "discover schema predicates for a relationship",
        _candidates()[:2],
    )
    second_scores, second_diagnostics = (
        service.retrieve_registered_tool_capability_scores(
            "discover schema predicates for a relationship",
            _candidates()[:2],
        )
    )

    assert rag.reset_count == 1
    assert set(scores).issubset({"search_concepts", "find_relations_with_argument"})
    assert scores["search_concepts"] > scores.get(
        "find_relations_with_argument",
        0.0,
    )
    assert second_scores == scores
    assert diagnostics["status"] == "results_available"
    assert second_diagnostics["index"]["ready"] is True
    assert rag.query_calls[0]["permissions_context"]["type"] == (
        "registered_tool_capability"
    )


def test_metadata_digest_change_rebuilds_the_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rag = _FakeCapabilityRAG(tmp_path)
    monkeypatch.setattr(service, "_get_capability_rag_service", lambda: rag)

    first = _candidates()
    service.ensure_registered_tool_capability_index(first)
    changed = [dict(candidate) for candidate in first]
    changed[0]["planner_hint"] = "Changed represented schema guidance."
    service.ensure_registered_tool_capability_index(changed)

    assert rag.reset_count == 2
    state = service.get_registered_tool_capability_index_state()
    assert state["ready"] is True
    assert state["source"] == "rebuilt"


def test_retrieval_unavailability_is_typed_and_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_backend() -> Any:
        raise RuntimeError("embedding backend unavailable")

    monkeypatch.setattr(service, "_get_capability_rag_service", fail_backend)

    scores, diagnostics = service.retrieve_registered_tool_capability_scores(
        "represented relationship lookup",
        _candidates(),
    )

    assert scores == {}
    assert diagnostics["status"] == "unavailable"
    assert diagnostics["retrieval_state"]["authoritative_empty"] is False
    assert diagnostics["retrieval_state"]["cause"] == (
        "registered_tool_capability_index_unavailable"
    )


def test_matching_background_build_returns_without_starting_a_second_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = service._normalised_documents(_candidates())
    digest = service._document_digest(documents)
    service._set_runtime_state(
        status="building",
        ready=False,
        digest=digest,
        document_count=len(documents),
    )
    monkeypatch.setattr(
        service,
        "_get_capability_rag_service",
        lambda: pytest.fail("request path must not wait for or duplicate prewarm"),
    )

    state = service.ensure_registered_tool_capability_index(_candidates())

    assert state["status"] == "building"
    assert state["ready"] is False
    assert state["digest"] == digest
