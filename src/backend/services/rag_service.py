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
from typing import Protocol, Iterable, Dict, Any, Optional, List, Tuple


_RAG_SERVICE_SINGLETONS: dict[str, "RAGService"] = {}


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

    def query(self, query_text: str, *, top_k: int = 5, namespace: Optional[str] = None, hybrid: bool = True, permissions_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")

    def embed(self, texts: Iterable[str]) -> List[List[float]]:  # type: ignore
        raise RAGBackendUnavailable("RAG backend not implemented")
