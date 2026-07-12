"""
RAG Service Abstraction (Apache-2.0 compatible)

Purpose:
- Provide a pluggable interface for Retrieval Augmented Generation (RAG)
  that supports rapid incremental reindexing and high quality semantic proximity search.
- Keep dependencies optional to allow selection between Apache-2.0 friendly libraries
  like LlamaIndex or Haystack; LangChain (MIT) can be used purely as orchestration.

Design:
- `RAGService` defines the minimal interface used by Von.
- Concrete backends can implement this interface without leaking library types.
- Index operations are incremental by default; bulk rebuild remains available.

Note:
- This module is dependency-free. Backend-specific modules should live under
  `src/backend/services/rag_backends/` and be imported lazily.
"""

from __future__ import annotations
from typing import Protocol, Iterable, Dict, Any, Optional, List, Mapping, Tuple


_RAG_SERVICE_SINGLETONS: dict[str, "RAGService"] = {}

RAG_RETRIEVAL_STATE_SCHEMA_VERSION = "rag_retrieval_state.v1"
RAG_RETRIEVAL_STATUSES = frozenset(
    {
        "results_available",
        "partial_results",
        "valid_empty",
        "missing_index",
        "signature_missing",
        "embedding_signature_mismatch",
        "rebuild_in_progress",
        "candidate_window_exhausted",
        "unavailable",
        "degraded",
    }
)
_RAG_USABLE_RETRIEVAL_STATUSES = frozenset(
    {"results_available", "partial_results", "valid_empty"}
)
_RAG_REBUILD_REQUIRED_STATUSES = frozenset(
    {"missing_index", "signature_missing", "embedding_signature_mismatch"}
)
_RAG_RETRYABLE_RETRIEVAL_STATUSES = frozenset(
    {
        "rebuild_in_progress",
        "candidate_window_exhausted",
        "partial_results",
        "unavailable",
        "degraded",
    }
)


def build_rag_retrieval_state(
    status: str,
    *,
    result_count: int = 0,
    cause: str | None = None,
    detail: str | None = None,
    candidate_count: int | None = None,
    filtered_candidate_count: int | None = None,
    candidate_limit: int | None = None,
    candidate_limit_reached: bool | None = None,
) -> dict[str, Any]:
    """Build the bounded, backend-neutral state for one retrieval attempt.

    The state deliberately reports support-layer facts rather than deciding how
    a workflow should respond. In particular, ``valid_empty`` means a usable
    index was queried and yielded no accessible matches; all other empty-result
    states remain distinguishable so callers do not mistake incompatibility or
    unavailability for evidence that the corpus contains nothing relevant.
    """

    clean_status = str(status or "").strip().lower()
    clean_cause = str(cause or "").strip() or None
    if clean_status not in RAG_RETRIEVAL_STATUSES:
        clean_cause = clean_cause or clean_status or "unknown_retrieval_state"
        clean_status = "degraded"

    try:
        bounded_result_count = max(0, int(result_count))
    except (TypeError, ValueError):
        bounded_result_count = 0

    usable = clean_status in _RAG_USABLE_RETRIEVAL_STATUSES
    rebuild_required = clean_status in _RAG_REBUILD_REQUIRED_STATUSES
    retryable = clean_status in _RAG_RETRYABLE_RETRIEVAL_STATUSES
    recovery_affordances: list[dict[str, str]] = []
    if retryable:
        recovery_affordances.append({"action_type": "retry"})
    if clean_status in {"candidate_window_exhausted", "partial_results"}:
        recovery_affordances.append({"action_type": "expand_candidate_window"})
    if rebuild_required:
        recovery_affordances.append({"action_type": "rebuild_namespace_index"})
    if clean_status in {"unavailable", "degraded"}:
        recovery_affordances.append({"action_type": "inspect_runtime"})

    state: dict[str, Any] = {
        "schema_version": RAG_RETRIEVAL_STATE_SCHEMA_VERSION,
        "status": clean_status,
        "usable": usable,
        "authoritative_empty": clean_status == "valid_empty",
        "result_count": bounded_result_count,
        "retryable": retryable,
        "rebuild_required": rebuild_required,
        "recovery_affordances": recovery_affordances,
    }
    if clean_cause:
        state["cause"] = clean_cause
    clean_detail = str(detail or "").strip()
    if clean_detail:
        # Keep diagnostic payloads bounded; exception bodies and secrets must not
        # become an unbounded tool-result surface.
        state["detail"] = clean_detail[:500]
    for key, value in (
        ("candidate_count", candidate_count),
        ("filtered_candidate_count", filtered_candidate_count),
        ("candidate_limit", candidate_limit),
    ):
        if value is not None:
            try:
                state[key] = max(0, int(value))
            except (TypeError, ValueError):
                pass
    if isinstance(candidate_limit_reached, bool):
        state["candidate_limit_reached"] = candidate_limit_reached
    return state


class RAGQueryResults(list[Dict[str, Any]]):
    """List-compatible query result carrying attempt-specific retrieval state.

    A list subclass avoids breaking existing RAG consumers while keeping the
    state attached to the exact result object, rather than relying solely on a
    mutable process-global "last query" diagnostic that can race across turns.
    """

    def __init__(
        self,
        values: Iterable[Dict[str, Any]] = (),
        *,
        retrieval_state: Mapping[str, Any],
    ) -> None:
        super().__init__(values)
        self.retrieval_state = dict(retrieval_state)


class RAGService(Protocol):
    """Protocol for pluggable RAG backends."""

    def upsert_documents(
        self,
        docs: Iterable[Dict[str, Any]],
        *,
        namespace: Optional[str] = None,
        allow_partial_failures: bool = True,
    ) -> Tuple[int, int]:
        """
        Incrementally index or reindex documents.

        Returns (success_count, failure_count).
        Each `doc` must include at minimum:
          - `id`: stable identifier
          - `text`: content
          - optional metadata fields (e.g., `source`, `permissions`)
        """
        ...

    def delete_documents(
        self,
        ids: Iterable[str],
        *,
        namespace: Optional[str] = None,
    ) -> int:
        """Delete documents by id. Returns count removed."""
        ...

    def reset_namespace(
        self,
        namespace: Optional[str] = None,
    ) -> None:
        """Remove all indexed data for a namespace."""
        ...

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        namespace: Optional[str] = None,
        hybrid: bool = True,
        permissions_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform semantic proximity search (optionally hybrid dense+sparse).

        Returns a list of dicts with keys like:
          - `id`, `score`, `text`, `metadata`
        Implementations may return `RAGQueryResults`, which remains list-compatible
        while attaching the typed state for the exact retrieval attempt.
        Implementations must honour `permissions_context` to filter results.
        """
        ...

    def embed(
        self,
        texts: Iterable[str],
    ) -> List[List[float]]:
        """Return embeddings for provided texts (backend model-dependent)."""
        ...


class RAGBackendUnavailable(RuntimeError):
    pass


def peek_rag_service(backend: Optional[str] = None) -> Optional[RAGService]:
    """Return a cached RAG service instance without initialising anything.

    This is useful for lightweight diagnostics endpoints (e.g., UI runtime hints)
    where we must not trigger slow imports/model initialisation.
    """

    name = (backend or "llamaindex").lower()
    return _RAG_SERVICE_SINGLETONS.get(name)


def list_initialised_rag_backends() -> List[str]:
    """Return backend names that already have an initialised singleton."""

    return sorted(_RAG_SERVICE_SINGLETONS.keys())


def get_rag_service(backend: Optional[str] = None) -> RAGService:
    """
    Factory to obtain a RAGService implementation.

    Selection rules (subject to settings integration):
    - `llamaindex` → use LlamaIndex backend (Apache-2.0)
    - `haystack`   → use Haystack backend (Apache-2.0)
    - default      → prefer LlamaIndex, fallback to Haystack
    """
    name = (backend or "llamaindex").lower()

    cached = _RAG_SERVICE_SINGLETONS.get(name)
    if cached is not None:
        return cached

    if name == "llamaindex":
        try:
            from .rag_backends.llamaindex_backend import LlamaIndexRAGService  # type: ignore

            service = LlamaIndexRAGService()
            _RAG_SERVICE_SINGLETONS[name] = service
            return service
        except Exception as e:
            # Fallback to Haystack if LlamaIndex not available
            try:
                from .rag_backends.haystack_backend import HaystackRAGService  # type: ignore

                service = HaystackRAGService()
                # Cache under both keys so callers asking for llamaindex do not
                # repeatedly retry a failing import.
                _RAG_SERVICE_SINGLETONS["haystack"] = service
                _RAG_SERVICE_SINGLETONS[name] = service
                return service
            except Exception:
                raise RAGBackendUnavailable(
                    f"No RAG backend available (llamaindex failure: {e})"
                )
    elif name == "haystack":
        try:
            from .rag_backends.haystack_backend import HaystackRAGService  # type: ignore

            service = HaystackRAGService()
            _RAG_SERVICE_SINGLETONS[name] = service
            return service
        except Exception as e:
            raise RAGBackendUnavailable(f"Haystack backend unavailable: {e}")
    else:
        raise ValueError(f"Unknown RAG backend '{backend}'")


# Minimal shim backends to keep code import-safe until concrete modules are added.
# These provide clear errors if called prematurely.


class _NotImplementedRAG(RAGService):
    def upsert_documents(self, docs: Iterable[Dict[str, Any]], *, namespace: Optional[str] = None, allow_partial_failures: bool = True) -> Tuple[int, int]:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")

    def delete_documents(self, ids: Iterable[str], *, namespace: Optional[str] = None) -> int:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")

    def reset_namespace(self, namespace: Optional[str] = None) -> None:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")

    def query(self, query_text: str, *, top_k: int = 5, namespace: Optional[str] = None, hybrid: bool = True, permissions_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")

    def embed(self, texts: Iterable[str]) -> List[List[float]]:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")
