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

    def delete_documents(
        self,
        ids: Iterable[str],
        *,
        namespace: Optional[str] = None,
    ) -> int:
        """Delete documents by id. Returns count removed."""

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

    def embed(
        self,
        texts: Iterable[str],
    ) -> List[List[float]]:
        """Return embeddings for provided texts (backend model-dependent)."""


class RAGBackendUnavailable(RuntimeError):
    pass


def get_rag_service(backend: Optional[str] = None) -> RAGService:
    """
    Factory to obtain a RAGService implementation.

    Selection rules (subject to settings integration):
    - `llamaindex` → use LlamaIndex backend (Apache-2.0)
    - `haystack`   → use Haystack backend (Apache-2.0)
    - default      → prefer LlamaIndex, fallback to Haystack
    """
    name = (backend or "llamaindex").lower()
    if name == "llamaindex":
        try:
            from .rag_backends.llamaindex_backend import LlamaIndexRAGService  # type: ignore
            return LlamaIndexRAGService()
        except Exception as e:
            # Fallback to Haystack if LlamaIndex not available
            try:
                from .rag_backends.haystack_backend import HaystackRAGService  # type: ignore
                return HaystackRAGService()
            except Exception:
                raise RAGBackendUnavailable(f"No RAG backend available (llamaindex failure: {e})")
    elif name == "haystack":
        try:
            from .rag_backends.haystack_backend import HaystackRAGService  # type: ignore
            return HaystackRAGService()
        except Exception as e:
            raise RAGBackendUnavailable(f"Haystack backend unavailable: {e}")
    else:
        raise ValueError(f"Unknown RAG backend '{backend}'")


# Minimal shim backends to keep code import-safe until concrete modules are added.
# These provide clear errors if called prematurely.

class _NotImplementedRAG(RAGService):
    def upsert_documents(self, docs: Iterable[Dict[str, Any]], *, namespace: Optional[str] = None, allow_partial_failures: bool = True) -> Tuple[int, int]:
        raise RAGBackendUnavailable("RAG backend not implemented")

    def delete_documents(self, ids: Iterable[str], *, namespace: Optional[str] = None) -> int:
        raise RAGBackendUnavailable("RAG backend not implemented")

    def query(self, query_text: str, *, top_k: int = 5, namespace: Optional[str] = None, hybrid: bool = True, permissions_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        raise RAGBackendUnavailable("RAG backend not implemented")

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        raise RAGBackendUnavailable("RAG backend not implemented")
