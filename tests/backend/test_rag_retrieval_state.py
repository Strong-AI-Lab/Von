"""Typed RAG retrieval-state regressions (JVNAUTOSCI-1982 / 2575)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from src.backend.integrations.internal_mcp import catalogue
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services.rag_backends.llamaindex_backend import (
    LlamaIndexRAGService,
)
from src.backend.services.rag_backends.haystack_backend import HaystackRAGService
from src.backend.services.rag_service import (
    RAGBackendUnavailable,
    RAGQueryResults,
    build_rag_retrieval_state,
)


def _bare_rag_service(monkeypatch: pytest.MonkeyPatch) -> LlamaIndexRAGService:
    """Build the query support surface without initialising a model backend."""

    rag = object.__new__(LlamaIndexRAGService)
    rag._indices = {}
    rag._namespace_retrieval_states = {}
    rag._retrieval_state_lock = threading.Lock()
    rag._last_query_info = None
    monkeypatch.setattr(
        rag,
        "_resolve_effective_namespace",
        lambda namespace: namespace or "test_namespace",
    )
    return rag


@pytest.mark.parametrize(
    ("namespace_status", "compatible", "expected_status"),
    [
        ("missing_index", True, "missing_index"),
        ("signature_missing", False, "signature_missing"),
        (
            "embedding_signature_mismatch",
            False,
            "embedding_signature_mismatch",
        ),
        ("rebuild_in_progress", False, "rebuild_in_progress"),
        ("embedder_unconfigured", False, "unavailable"),
    ],
)
def test_llamaindex_query_preserves_blocked_namespace_state(
    monkeypatch: pytest.MonkeyPatch,
    namespace_status: str,
    compatible: bool,
    expected_status: str,
) -> None:
    rag = _bare_rag_service(monkeypatch)
    monkeypatch.setattr(
        rag,
        "get_namespace_runtime_state",
        lambda _namespace: {
            "status": namespace_status,
            "compatible": compatible,
            "detail": f"diagnostic:{namespace_status}",
            "lock_contended": namespace_status == "rebuild_in_progress",
        },
    )

    results = rag.query("synthetic query", namespace="#V#test_user")

    assert results == []
    assert isinstance(results, RAGQueryResults)
    assert results.retrieval_state["status"] == expected_status
    assert results.retrieval_state["usable"] is False
    assert results.retrieval_state["authoritative_empty"] is False
    assert rag.get_last_retrieval_state("#V#test_user") == results.retrieval_state
    assert rag._last_query_info["retrieval_state"] == results.retrieval_state
    if namespace_status == "rebuild_in_progress":
        assert results.retrieval_state["cause"] == "namespace_lock_contended"


def test_llamaindex_bounded_empty_search_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Retriever:
        def retrieve(self, _query_text: str) -> list[Any]:
            return []

    class _Index:
        def as_retriever(self, *, similarity_top_k: int) -> _Retriever:
            assert similarity_top_k == 50
            return _Retriever()

    rag = _bare_rag_service(monkeypatch)
    monkeypatch.setattr(
        rag,
        "get_namespace_runtime_state",
        lambda _namespace: {"status": "compatible", "compatible": True},
    )
    monkeypatch.setattr(rag, "_maybe_load_index", lambda _namespace: _Index())
    monkeypatch.setattr(
        rag,
        "_temporarily_bound_query_embed_model",
        lambda: (None, {}),
    )

    results = rag.query("synthetic query", namespace="#V#test_user")

    assert results == []
    assert results.retrieval_state["status"] == "degraded"
    assert results.retrieval_state["usable"] is False
    assert results.retrieval_state["authoritative_empty"] is False
    assert results.retrieval_state["cause"] == (
        "bounded_visible_result_window_inconclusive"
    )
    assert results.retrieval_state["candidate_count"] == 0
    assert results.retrieval_state["candidate_limit"] == 5
    assert results.retrieval_state["candidate_limit_reached"] is False
    assert "filtered_candidate_count" not in results.retrieval_state


def test_filtered_candidate_window_is_not_reported_as_authoritative_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        SimpleNamespace(
            node=SimpleNamespace(
                metadata={"user_id": "#V#other_user"},
                ref_doc_id=f"doc-{index}",
                node_id=f"node-{index}",
                get_content=lambda: "filtered content",
            ),
            score=0.5,
        )
        for index in range(50)
    ]

    class _Retriever:
        def retrieve(self, _query_text: str) -> list[Any]:
            return nodes

    class _Index:
        def as_retriever(self, *, similarity_top_k: int) -> _Retriever:
            assert similarity_top_k == 50
            return _Retriever()

    rag = _bare_rag_service(monkeypatch)
    monkeypatch.setattr(
        rag,
        "get_namespace_runtime_state",
        lambda _namespace: {"status": "compatible", "compatible": True},
    )
    monkeypatch.setattr(rag, "_maybe_load_index", lambda _namespace: _Index())
    monkeypatch.setattr(
        rag,
        "_temporarily_bound_query_embed_model",
        lambda: (None, {}),
    )

    results = rag.query(
        "synthetic query",
        namespace="#V#test_user",
        permissions_context={"user_id": "#V#target_user"},
    )

    assert results == []
    assert results.retrieval_state["status"] == "degraded"
    assert results.retrieval_state["authoritative_empty"] is False
    assert results.retrieval_state["cause"] == (
        "bounded_visible_result_window_inconclusive"
    )
    assert results.retrieval_state["candidate_count"] == 0
    assert results.retrieval_state["candidate_limit"] == 5
    assert results.retrieval_state["candidate_limit_reached"] is False
    assert "filtered_candidate_count" not in results.retrieval_state
    assert {
        item["action_type"] for item in results.retrieval_state["recovery_affordances"]
    } == {
        "retry",
        "inspect_runtime",
    }


def test_filtered_saturated_window_preserves_rows_but_reports_partial_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        SimpleNamespace(
            node=SimpleNamespace(
                metadata={
                    "user_id": (
                        "#V#target_user" if index == 49 else "#V#other_user"
                    )
                },
                ref_doc_id=f"doc-{index}",
                node_id=f"node-{index}",
                get_content=lambda: "candidate content",
            ),
            score=0.5,
        )
        for index in range(50)
    ]

    class _Retriever:
        def retrieve(self, _query_text: str) -> list[Any]:
            return nodes

    class _Index:
        def as_retriever(self, *, similarity_top_k: int) -> _Retriever:
            assert similarity_top_k == 50
            return _Retriever()

    rag = _bare_rag_service(monkeypatch)
    monkeypatch.setattr(
        rag,
        "get_namespace_runtime_state",
        lambda _namespace: {"status": "compatible", "compatible": True},
    )
    monkeypatch.setattr(rag, "_maybe_load_index", lambda _namespace: _Index())
    monkeypatch.setattr(
        rag,
        "_temporarily_bound_query_embed_model",
        lambda: (None, {}),
    )

    results = rag.query(
        "synthetic query",
        namespace="#V#test_user",
        permissions_context={"user_id": "#V#target_user"},
    )

    assert len(results) == 1
    assert results.retrieval_state["status"] == "partial_results"
    assert results.retrieval_state["usable"] is True
    assert results.retrieval_state["authoritative_empty"] is False
    assert results.retrieval_state["cause"] == (
        "bounded_visible_result_window_underfilled"
    )
    assert results.retrieval_state["candidate_count"] == 1
    assert results.retrieval_state["candidate_limit"] == 5
    assert results.retrieval_state["candidate_limit_reached"] is False
    assert "filtered_candidate_count" not in results.retrieval_state
    assert {
        item["action_type"] for item in results.retrieval_state["recovery_affordances"]
    } == {"retry", "expand_candidate_window"}


def test_hidden_scoped_candidates_are_absent_from_retrieval_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        SimpleNamespace(
            node=SimpleNamespace(
                metadata={
                    "type": "scoped_knowledge_assertion",
                    "assertion_id": "ska_revoked",
                    "user_id": "#V#target_user",
                    "organisation_concept_id": "#V#org",
                },
                ref_doc_id="scoped_assertion:ska_revoked",
                node_id="node-scoped",
                get_content=lambda: "revoked private assertion",
            ),
            score=0.9,
        ),
        SimpleNamespace(
            node=SimpleNamespace(
                    metadata={
                        "type": "text_relation",
                        "relation_id": "visible",
                        "user_id": "#V#target_user",
                    "organisation_concept_id": "#V#org",
                },
                ref_doc_id="text_relation:visible",
                node_id="node-visible",
                get_content=lambda: "visible candidate",
            ),
            score=0.8,
        ),
        SimpleNamespace(
            node=SimpleNamespace(
                    metadata={
                        "type": "text_relation",
                        "relation_id": "filtered",
                        "user_id": "#V#other_user",
                    "organisation_concept_id": "#V#org",
                },
                ref_doc_id="text_relation:filtered",
                node_id="node-filtered",
                get_content=lambda: "ordinary filtered candidate",
            ),
            score=0.7,
        ),
    ]

    class _Retriever:
        def retrieve(self, _query_text: str) -> list[Any]:
            return nodes

    class _Index:
        def as_retriever(self, *, similarity_top_k: int) -> _Retriever:
            assert similarity_top_k == 50
            return _Retriever()

    rag = _bare_rag_service(monkeypatch)
    monkeypatch.setattr(
        rag,
        "get_namespace_runtime_state",
        lambda _namespace: {"status": "compatible", "compatible": True},
    )
    monkeypatch.setattr(rag, "_maybe_load_index", lambda _namespace: _Index())
    monkeypatch.setattr(
        rag,
        "_temporarily_bound_query_embed_model",
        lambda: (None, {}),
    )
    monkeypatch.setattr(
        "src.backend.services.scoped_rag_authority_service."
        "current_authorised_rag_candidate_keys",
        lambda *_args, **_kwargs: {
            ("text_relation", "visible"),
            ("text_relation", "filtered"),
        },
    )

    results = rag.query(
        "synthetic query",
        namespace="#V#target_user@org",
        permissions_context={
            "mode": "concepts",
            "user_id": "#V#target_user",
            "organisation_concept_id": "#V#org",
        },
    )

    assert [row["id"] for row in results] == ["text_relation:visible"]
    assert results.retrieval_state["status"] == "partial_results"
    assert results.retrieval_state["cause"] == (
        "bounded_visible_result_window_underfilled"
    )
    assert results.retrieval_state["candidate_count"] == 1
    assert results.retrieval_state["candidate_limit"] == 5
    assert results.retrieval_state["candidate_limit_reached"] is False
    assert "filtered_candidate_count" not in results.retrieval_state
    assert rag._last_query_info["retrieved"] == 1
    assert rag._last_query_info["returned"] == 1


def test_hidden_authority_rows_match_clean_empty_retrieval_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        SimpleNamespace(
            node=SimpleNamespace(
                metadata={
                    "type": "scoped_knowledge_assertion",
                    "assertion_id": f"ska_hidden_{index}",
                    "user_id": "#V#target_user",
                    "organisation_concept_id": "#V#org",
                },
                ref_doc_id=f"scoped_assertion:ska_hidden_{index}",
                node_id=f"node-{index}",
                get_content=lambda: "hidden",
            ),
            score=1.0,
        )
        for index in range(50)
    ]

    class _Retriever:
        def retrieve(self, query_text: str) -> list[Any]:
            return nodes if query_text == "hidden query" else []

    class _Index:
        def as_retriever(self, *, similarity_top_k: int) -> _Retriever:
            assert similarity_top_k == 50
            return _Retriever()

    rag = _bare_rag_service(monkeypatch)
    monkeypatch.setattr(
        rag,
        "get_namespace_runtime_state",
        lambda _namespace: {"status": "compatible", "compatible": True},
    )
    monkeypatch.setattr(rag, "_maybe_load_index", lambda _namespace: _Index())
    monkeypatch.setattr(
        rag,
        "_temporarily_bound_query_embed_model",
        lambda: (None, {}),
    )
    monkeypatch.setattr(
        "src.backend.services.scoped_rag_authority_service."
        "current_authorised_rag_candidate_keys",
        lambda *_args, **_kwargs: set(),
    )

    hidden_results = rag.query(
        "hidden query",
        namespace="#V#target_user@org",
        permissions_context={
            "mode": "concepts",
            "user_id": "#V#target_user",
            "organisation_concept_id": "#V#org",
        },
    )
    empty_results = rag.query(
        "clean empty query",
        namespace="#V#target_user@org",
        permissions_context={
            "mode": "concepts",
            "user_id": "#V#target_user",
            "organisation_concept_id": "#V#org",
        },
    )

    assert hidden_results == empty_results == []
    assert hidden_results.retrieval_state == empty_results.retrieval_state
    assert hidden_results.retrieval_state["status"] == "degraded"
    assert hidden_results.retrieval_state["authoritative_empty"] is False
    assert hidden_results.retrieval_state["cause"] == (
        "bounded_visible_result_window_inconclusive"
    )
    assert hidden_results.retrieval_state["candidate_count"] == 0
    assert hidden_results.retrieval_state["candidate_limit"] == 5
    assert hidden_results.retrieval_state["candidate_limit_reached"] is False
    assert "filtered_candidate_count" not in hidden_results.retrieval_state
    assert rag._last_query_info["retrieved"] == 0


def test_search_knowledge_base_does_not_report_index_mismatch_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StatefulRAG:
        def query(self, **_kwargs: Any) -> RAGQueryResults:
            return RAGQueryResults(
                [],
                retrieval_state=build_rag_retrieval_state(
                    "embedding_signature_mismatch",
                    cause="embedding_signature_mismatch",
                    detail="A namespace rebuild is required.",
                ),
            )

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _StatefulRAG(),
    )

    payload = catalogue._search_knowledge_base(
        query="synthetic query",
        namespace="#V#test_user@org",
    )

    assert payload["success"] is False
    assert payload["error"] == "embedding_signature_mismatch"
    assert payload["results"] == []
    assert payload["retrieval_state"]["status"] == "embedding_signature_mismatch"
    assert payload["retrieval_state"]["rebuild_required"] is True
    assert payload["retrieval_state"]["recovery_affordances"] == [
        {"action_type": "rebuild_namespace_index"}
    ]


def test_search_knowledge_base_preserves_valid_empty_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StatefulRAG:
        def query(self, **_kwargs: Any) -> RAGQueryResults:
            return RAGQueryResults(
                [],
                retrieval_state=build_rag_retrieval_state(
                    "valid_empty",
                    cause="query_completed",
                ),
            )

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _StatefulRAG(),
    )

    payload = catalogue._search_knowledge_base(
        query="synthetic query",
        namespace="#V#test_user@org",
    )

    assert payload["success"] is True
    assert "error" not in payload
    assert payload["retrieval_state"]["status"] == "valid_empty"
    assert payload["retrieval_state"]["authoritative_empty"] is True


def test_search_knowledge_base_reports_backend_unavailable_with_recovery_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RAGBackendUnavailable("backend omitted from this test")

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        _raise_unavailable,
    )

    payload = catalogue._search_knowledge_base(
        query="synthetic query",
        namespace="#V#test_user@org",
    )

    assert payload["success"] is False
    assert payload["retrieval_state"]["status"] == "unavailable"
    assert payload["retrieval_state"]["retryable"] is True
    assert payload["retrieval_state"]["recovery_affordances"] == [
        {"action_type": "retry"},
        {"action_type": "inspect_runtime"},
    ]


def test_unimplemented_haystack_placeholder_cannot_report_authoritative_empty() -> None:
    with pytest.raises(RAGBackendUnavailable, match="not implemented"):
        HaystackRAGService()


def test_untyped_empty_backend_result_is_degraded_not_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LegacyEmptyRAG:
        def query(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _LegacyEmptyRAG(),
    )

    payload = catalogue._search_knowledge_base(
        query="synthetic query",
        namespace="#V#test_user@org",
    )

    assert payload["success"] is False
    assert payload["retrieval_state"]["status"] == "degraded"
    assert payload["retrieval_state"]["authoritative_empty"] is False
    assert payload["retrieval_state"]["cause"] == (
        "empty_result_missing_typed_retrieval_state"
    )


def test_search_knowledge_base_attaches_typed_state_to_unexpected_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingRAG:
        def query(self, **_kwargs: Any) -> Any:
            raise ValueError("synthetic backend failure")

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _FailingRAG(),
    )

    payload = catalogue._search_knowledge_base(
        query="synthetic query",
        namespace="#V#test_user@org",
    )

    assert payload["success"] is False
    assert payload["error_code"] == "exception"
    assert payload["retrieval_state"]["status"] == "degraded"
    assert payload["retrieval_state"]["cause"] == "rag_query_exception:ValueError"


def test_llm_projection_keeps_typed_state_when_no_rows_are_returned() -> None:
    state = build_rag_retrieval_state(
        "rebuild_in_progress",
        cause="namespace_lock_contended",
    )

    payload = InternalMCPChatOrchestrator._shape_search_knowledge_base_payload_for_llm(
        {
            "query": "synthetic query",
            "count": 0,
            "results": [],
            "retrieval_state": state,
        }
    )

    assert payload["retrieval_state"] == state
    assert "authoritative" in payload["retrieval_diagnostics"]["note"]
