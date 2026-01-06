"""src.backend.services.rag_backends.llamaindex_backend

LlamaIndex RAG Backend Implementation (Apache-2.0)

This module implements the RAGService protocol using LlamaIndex.
It is imported lazily to avoid hard dependencies.

Namespace behaviour:
- The RAGService interface supports a `namespace` argument on upsert/query/delete.
- This backend treats namespace as a hard isolation boundary by persisting a
    separate on-disk index per namespace.
- Documents are also stamped with `metadata["namespace"]` to aid debugging and
    allow defensive filtering.
"""

import hashlib
import importlib
import os
import re
import time
from datetime import datetime, timezone
from typing import Iterable, Dict, Any, Optional, List, Tuple

from ..rag_service import RAGService
from ...utils.concept_id_utils import normalise_concept_id_for_compare


_LLAMAINDEX_MISSING_MESSAGE = (
    "LlamaIndex dependencies missing. Install the optional dependency group(s) "
    "that provide `llama-index` for this backend."
)


class _MissingVectorStoreIndex:
    @classmethod
    def from_documents(cls, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


class _MissingDocument:
    def __init__(self, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


class _MissingStorageContext:
    @classmethod
    def from_defaults(cls, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


def _missing_load_index_from_storage(*args, **kwargs):  # pragma: no cover
    raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


class _FallbackServiceContext:
    """Compatibility shim.

    Newer LlamaIndex releases removed ServiceContext in favour of global Settings.
    We keep this symbol so tests can patch `ServiceContext.from_defaults`.
    """

    @classmethod
    def from_defaults(cls, *args, **kwargs):  # pragma: no cover
        return None


# These are intentionally `Any` so Pylance doesn't complain when we swap in
# the real LlamaIndex implementations at runtime.
VectorStoreIndex: Any = _MissingVectorStoreIndex
Document: Any = _MissingDocument
StorageContext: Any = _MissingStorageContext
load_index_from_storage: Any = _missing_load_index_from_storage
ServiceContext: Any = _FallbackServiceContext


# Attempt imports in a version-tolerant way.
# - Newer LlamaIndex exposes most APIs under llama_index.core
# - Older versions exposed them at the top-level llama_index package
try:  # pragma: no cover
    core = importlib.import_module("llama_index.core")
    VectorStoreIndex = getattr(core, "VectorStoreIndex")
    Document = getattr(core, "Document")
    StorageContext = getattr(core, "StorageContext")
    load_index_from_storage = getattr(core, "load_index_from_storage")

    try:
        service_context_mod = importlib.import_module(
            "llama_index.core.service_context"
        )
        ServiceContext = getattr(service_context_mod, "ServiceContext")
    except Exception:
        # Keep the fallback shim.
        pass
except Exception:  # pragma: no cover
    try:
        llama_index = importlib.import_module("llama_index")
        VectorStoreIndex = getattr(llama_index, "VectorStoreIndex")
        Document = getattr(llama_index, "Document")
        StorageContext = getattr(llama_index, "StorageContext")
        load_index_from_storage = getattr(llama_index, "load_index_from_storage")
        ServiceContext = getattr(llama_index, "ServiceContext", ServiceContext)
    except Exception:
        # Leave stubs in place so the module remains importable.
        pass


class LlamaIndexRAGService(RAGService):
    def __init__(self, persistence_dir: str = "./data/rag_storage"):
        self.persistence_dir = persistence_dir

        # Cache of namespace -> index instance
        self._indices: Dict[str, Any] = {}

        # Ensure base persistence directory exists
        os.makedirs(self.persistence_dir, exist_ok=True)

        # Initialise ServiceContext (can be customised with specific LLM/Embed model).
        # In newer LlamaIndex releases this may return None (Settings-based).
        self.service_context = ServiceContext.from_defaults()

        # Best-effort diagnostics for UI/debugging.
        self._last_query_info: Dict[str, Any] | None = None

    def _resolve_effective_namespace(self, namespace: Optional[str]) -> str:
        # Keep behaviour consistent with other parts of the system that may
        # set VON_DEFAULT_NAMESPACE.
        return namespace or os.getenv("VON_DEFAULT_NAMESPACE") or "chat_history"

    def _namespace_dirname(self, namespace: str) -> str:
        """Return a filesystem-safe, collision-resistant directory name."""
        normalised = namespace.strip()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", normalised)
        digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]
        return f"{safe}_{digest}"

    def _namespace_persist_dir(self, namespace: str) -> str:
        # Group namespace indices under a single subdirectory to avoid mixing
        # with legacy flat storage.
        return os.path.join(
            self.persistence_dir, "namespaces", self._namespace_dirname(namespace)
        )

    def _maybe_load_index(self, namespace: str) -> Any:
        if namespace in self._indices:
            return self._indices[namespace]

        persist_dir = self._namespace_persist_dir(namespace)
        if not os.path.isdir(persist_dir):
            return None

        # Avoid calling LlamaIndex load on an empty directory.
        try:
            if not os.listdir(persist_dir):
                return None
        except Exception:
            return None

        try:
            storage_context = StorageContext.from_defaults(persist_dir=persist_dir)
            kwargs = {}
            if self.service_context is not None:
                kwargs["service_context"] = self.service_context
            index = load_index_from_storage(storage_context, **kwargs)
        except Exception:
            return None

        self._indices[namespace] = index
        return index

    def _get_or_create_index(self, namespace: str, documents: List[Any]) -> Any:
        index = self._maybe_load_index(namespace)
        if index is not None:
            return index

        persist_dir = self._namespace_persist_dir(namespace)
        os.makedirs(persist_dir, exist_ok=True)
        kwargs = {}
        if self.service_context is not None:
            kwargs["service_context"] = self.service_context
        index = VectorStoreIndex.from_documents(documents, **kwargs)
        index.storage_context.persist(persist_dir=persist_dir)
        self._indices[namespace] = index
        return index

    def upsert_documents(
        self,
        docs: Iterable[Dict[str, Any]],
        *,
        namespace: Optional[str] = None,
        allow_partial_failures: bool = True,
    ) -> Tuple[int, int]:
        """
        Upsert documents into the index.
        LlamaIndex 0.9.x simple index doesn't support granular updates easily without a vector store.
        For this implementation, we will convert dicts to Documents and insert them.
        """
        effective_namespace = self._resolve_effective_namespace(namespace)

        llama_docs: List[Any] = []
        for doc in docs:
            # Convert dict to LlamaIndex Document
            text = doc.get("text", "")
            metadata = doc.get("metadata", {})
            doc_id = doc.get("id")

            if not text:
                continue

            # Use doc_id parameter if present, ensuring it is a string
            if doc_id:
                l_doc = Document(text=text, doc_id=str(doc_id))
            else:
                l_doc = Document(text=text)

            # Stamp namespace into metadata to support filtering/debugging.
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["namespace"] = effective_namespace
            l_doc.metadata = metadata
            llama_docs.append(l_doc)

        if not llama_docs:
            return (0, 0)

        index = self._maybe_load_index(effective_namespace)
        if index is None:
            index = self._get_or_create_index(effective_namespace, llama_docs)
            return (len(llama_docs), 0)

        success = 0
        failed = 0

        def _persist() -> None:
            index.storage_context.persist(
                persist_dir=self._namespace_persist_dir(effective_namespace)
            )

        # Prefer bulk insertion when supported (significantly faster for embedding-backed indices).
        try:
            insert_documents = getattr(index, "insert_documents", None)
            if callable(insert_documents):
                insert_documents(llama_docs)
                success = len(llama_docs)
                _persist()
                return (success, failed)
        except Exception:
            # Fall back to per-document insertion below.
            pass

        # Fallback: per-document insertion, optionally allowing partial failures.
        for l_doc in llama_docs:
            try:
                index.insert(l_doc)
                success += 1
            except Exception:
                failed += 1
                if not allow_partial_failures:
                    raise

        if success > 0:
            try:
                _persist()
            except Exception:
                # Persist failures should not mask successful indexing.
                pass

        return (success, failed)

    def delete_documents(
        self,
        ids: Iterable[str],
        *,
        namespace: Optional[str] = None,
    ) -> int:
        """
        Delete documents from the index.
        Note: Simple VectorStoreIndex delete might be limited depending on the store.
        """
        effective_namespace = self._resolve_effective_namespace(namespace)
        index = self._maybe_load_index(effective_namespace)
        if not index:
            return 0

        count = 0
        for doc_id in ids:
            try:
                index.delete_ref_doc(doc_id, delete_from_docstore=True)
                count += 1
            except Exception:
                pass

        if count > 0:
            index.storage_context.persist(
                persist_dir=self._namespace_persist_dir(effective_namespace)
            )

        return count

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        namespace: Optional[str] = None,
        hybrid: bool = True,
        permissions_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        effective_namespace = self._resolve_effective_namespace(namespace)
        index = self._maybe_load_index(effective_namespace)
        if not index:
            return []

        # Note: Metadata filtering support varies by vector store implementation.
        # To ensure correctness, we do coarse retrieval first then apply filtering
        # locally.
        similarity_top_k = max(top_k * 10, top_k)
        start = time.perf_counter()
        retriever = index.as_retriever(similarity_top_k=similarity_top_k)
        nodes = retriever.retrieve(query_text)

        def _matches_permissions(metadata: Any) -> bool:
            if not permissions_context:
                return True
            if not isinstance(metadata, dict):
                return False

            # Optional semantic filtering.
            # Backwards compatible: if no filter keys provided, behaviour is unchanged.
            mode = permissions_context.get("mode")
            requested_type = permissions_context.get("type")
            predicate = permissions_context.get("predicate")
            predicates = permissions_context.get("predicates")

            # Normalise filter inputs.
            requested_types: List[str] = []
            if isinstance(requested_type, str) and requested_type.strip():
                requested_types = [requested_type.strip()]

            if isinstance(mode, str):
                m = mode.strip().lower()
                if m == "chat":
                    requested_types = ["chat_message"]
                elif m == "concepts":
                    requested_types = ["text_relation"]
                elif m == "all" or not m:
                    pass

            if requested_types:
                actual_type = metadata.get("type")
                # Defensive fallback for older indices missing explicit type.
                if not isinstance(actual_type, str) or not actual_type.strip():
                    src = metadata.get("source")
                    doc_id = metadata.get("id")
                    if src == "vontology_text_relation":
                        actual_type = "text_relation"
                    elif src == "chat_history":
                        actual_type = "chat_message"
                    elif isinstance(doc_id, str) and doc_id.startswith(
                        "text_relation:"
                    ):
                        actual_type = "text_relation"

                if not isinstance(actual_type, str):
                    return False
                if actual_type not in requested_types:
                    return False

            # Predicate filter (only meaningful for text_relation docs).
            if isinstance(predicate, str) and predicate.strip():
                if metadata.get("predicate") != predicate.strip():
                    return False
            elif isinstance(predicates, list):
                allow = {
                    str(p).strip()
                    for p in predicates
                    if isinstance(p, str) and p.strip()
                }
                if allow:
                    if metadata.get("predicate") not in allow:
                        return False

            user_id = permissions_context.get("user_id")
            if user_id:
                if normalise_concept_id_for_compare(
                    metadata.get("user_id")
                ) != normalise_concept_id_for_compare(user_id):
                    return False

            org_id = permissions_context.get("organisation_concept_id")
            if org_id:
                if normalise_concept_id_for_compare(
                    metadata.get("organisation_concept_id")
                ) != normalise_concept_id_for_compare(org_id):
                    return False

            return True

        results = []
        for node in nodes:
            if not _matches_permissions(getattr(node.node, "metadata", None)):
                continue
            results.append(
                {
                    "id": node.node.ref_doc_id or node.node.node_id,
                    "text": node.node.get_content(),
                    "metadata": node.node.metadata,
                    "score": node.score,
                }
            )

            if len(results) >= top_k:
                break

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        try:
            self._last_query_info = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "effective_namespace": effective_namespace,
                "query_length": len(query_text or ""),
                "top_k": int(top_k),
                "similarity_top_k": int(similarity_top_k),
                "retrieved": len(nodes) if isinstance(nodes, list) else None,
                "returned": len(results),
                "elapsed_ms": elapsed_ms,
            }
        except Exception:
            # Never let diagnostics interfere with retrieval.
            pass

        return results

    def embed(
        self,
        texts: Iterable[str],
    ) -> List[List[float]]:
        # Use the embedding model from service context (if present).
        if self.service_context is None:
            raise RuntimeError(
                "Embeddings unavailable: LlamaIndex ServiceContext is not configured."
            )

        embed_model = getattr(self.service_context, "embed_model", None)
        if embed_model is None:
            raise RuntimeError(
                "Embeddings unavailable: LlamaIndex embed_model is not configured."
            )

        return [embed_model.get_text_embedding(t) for t in texts]
