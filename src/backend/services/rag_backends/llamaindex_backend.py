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
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from typing import Iterable, Dict, Any, Optional, List, Tuple, Mapping

from ..rag_service import RAGService
from ...utils.concept_id_utils import normalise_concept_id_for_compare


_LLAMAINDEX_MISSING_MESSAGE = (
    "LlamaIndex dependencies missing. Install the optional dependency group(s) "
    "that provide `llama-index` for this backend."
)
_QUERY_EMBED_TIMEOUT_SECONDS = 8.0
_QUERY_EMBED_MAX_RETRIES = 0
_INDEX_METADATA_FILENAME = "index_metadata.json"


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

    Newer LlamaIndex releases deprecated ServiceContext in favour of global
    Settings. We keep this symbol so tests can patch
    `ServiceContext.from_defaults`.
    """

    @classmethod
    def from_defaults(cls, *args, **kwargs):  # pragma: no cover
        return None


class _FallbackSettings:
    """Compatibility shim for Settings-based access in newer LlamaIndex."""

    embed_model = None
    llm = None


class _FallbackBaseEmbedding:
    def __init__(self, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


class _FallbackCustomLLM:
    def __init__(self, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


class _FallbackCompletionResponse:
    def __init__(self, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


class _FallbackLLMMetadata:
    def __init__(self, *args, **kwargs):  # pragma: no cover
        raise ImportError(_LLAMAINDEX_MISSING_MESSAGE)


# These are intentionally `Any` so Pylance doesn't complain when we swap in
# the real LlamaIndex implementations at runtime.
VectorStoreIndex: Any = _MissingVectorStoreIndex
Document: Any = _MissingDocument
StorageContext: Any = _MissingStorageContext
load_index_from_storage: Any = _missing_load_index_from_storage
ServiceContext: Any = _FallbackServiceContext
Settings: Any = _FallbackSettings
BaseEmbedding: Any = _FallbackBaseEmbedding
CustomLLM: Any = _FallbackCustomLLM
CompletionResponse: Any = _FallbackCompletionResponse
LLMMetadata: Any = _FallbackLLMMetadata


# Attempt imports in a version-tolerant way.
# - Newer LlamaIndex exposes most APIs under llama_index.core
# - Older versions exposed them at the top-level llama_index package
try:  # pragma: no cover
    core = importlib.import_module("llama_index.core")
    VectorStoreIndex = getattr(core, "VectorStoreIndex")
    Document = getattr(core, "Document")
    StorageContext = getattr(core, "StorageContext")
    load_index_from_storage = getattr(core, "load_index_from_storage")
    Settings = getattr(core, "Settings", Settings)
    try:
        embeddings_mod = importlib.import_module("llama_index.core.base.embeddings.base")
        BaseEmbedding = getattr(embeddings_mod, "BaseEmbedding", BaseEmbedding)
    except Exception:
        pass
    try:
        llms_mod = importlib.import_module("llama_index.core.llms")
        CustomLLM = getattr(llms_mod, "CustomLLM", CustomLLM)
        CompletionResponse = getattr(llms_mod, "CompletionResponse", CompletionResponse)
        LLMMetadata = getattr(llms_mod, "LLMMetadata", LLMMetadata)
    except Exception:
        pass

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
        Settings = getattr(llama_index, "Settings", Settings)
        try:
            embeddings_mod = importlib.import_module("llama_index.base.embeddings.base")
            BaseEmbedding = getattr(embeddings_mod, "BaseEmbedding", BaseEmbedding)
        except Exception:
            pass
        try:
            llms_mod = importlib.import_module("llama_index.llms")
            CustomLLM = getattr(llms_mod, "CustomLLM", CustomLLM)
            CompletionResponse = getattr(llms_mod, "CompletionResponse", CompletionResponse)
            LLMMetadata = getattr(llms_mod, "LLMMetadata", LLMMetadata)
        except Exception:
            pass
    except Exception:
        # Leave stubs in place so the module remains importable.
        pass


def _build_llm_interface_client(provider: str, *, host: str | None = None) -> Any:
    from ...languagemodels.llm_interface import GeminiClient, OllamaClient, OpenAIClient

    token = str(provider or "").strip().lower()
    if token == "openai":
        return OpenAIClient()
    if token == "ollama":
        return OllamaClient(host=host)
    if token == "gemini":
        return GeminiClient()
    raise RuntimeError(f"Unsupported RAG model provider '{provider}'.")


class _ConfiguredEmbeddingModel(BaseEmbedding):
    provider: str
    model_name: str
    host: Optional[str] = None
    selection_source: str = "explicit_setting"

    @classmethod
    def class_name(cls) -> str:
        return "configured_embedding_model"

    def _build_client(self) -> Any:
        return _build_llm_interface_client(self.provider, host=self.host)

    def _get_query_embedding(self, query: str) -> List[float]:
        return self._build_client().get_embedding(query, model=self.model_name)

    async def _aget_query_embedding(self, query: str) -> List[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> List[float]:
        return self._build_client().get_embedding(text, model=self.model_name)

    async def _aget_text_embedding(self, text: str) -> List[float]:
        return self._get_text_embedding(text)


class _ConfiguredRuntimeLLM(CustomLLM):
    provider: str
    model_name: str
    host: Optional[str] = None
    selection_source: str = "explicit_setting"

    @property
    def metadata(self) -> Any:
        return LLMMetadata(
            model_name=self.model_name,
            is_chat_model=True,
        )

    @classmethod
    def class_name(cls) -> str:
        return "configured_runtime_llm"

    def _build_client(self) -> Any:
        return _build_llm_interface_client(self.provider, host=self.host)

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> Any:
        text = self._build_client().generate(prompt, model=self.model_name)
        return CompletionResponse(text=text)

    def stream_complete(
        self,
        prompt: str,
        formatted: bool = False,
        **kwargs: Any,
    ) -> Any:
        yield self.complete(prompt, formatted=formatted, **kwargs)


class LlamaIndexRAGService(RAGService):
    def __init__(self, persistence_dir: str = "./data/rag_storage"):
        self.persistence_dir = persistence_dir

        # Cache of namespace -> index instance
        self._indices: Dict[str, Any] = {}
        self._namespace_runtime_state: Dict[str, Dict[str, Any]] = {}
        self._runtime_configuration: Dict[str, Any] | None = None
        self._runtime_configuration_serialized: str | None = None

        # Ensure base persistence directory exists
        os.makedirs(self.persistence_dir, exist_ok=True)

        # Newer LlamaIndex releases use global Settings rather than
        # ServiceContext. Keep a reference to whichever runtime surface the
        # installed version exposes.
        self.llamaindex_settings = Settings
        self.service_context = None

        # Best-effort diagnostics for UI/debugging.
        self._last_query_info: Dict[str, Any] | None = None
        self._refresh_runtime_configuration()

    @staticmethod
    def _is_service_context_deprecation_error(exc: Exception) -> bool:
        message = str(exc)
        return (
            isinstance(exc, ValueError)
            and "ServiceContext is deprecated" in message
            and "Settings" in message
        )

    def _build_service_context(self, *, embed_model: Any = None, llm: Any = None) -> Any:
        from_defaults = getattr(ServiceContext, "from_defaults", None)
        if not callable(from_defaults):
            return None

        try:
            return from_defaults(embed_model=embed_model, llm=llm)
        except TypeError:
            context = from_defaults()
            if context is not None:
                try:
                    setattr(context, "embed_model", embed_model)
                except Exception:
                    pass
                try:
                    setattr(context, "llm", llm)
                except Exception:
                    pass
            return context
        except Exception as exc:
            # LlamaIndex 0.14+ keeps the symbol but raises on use. Treat that as
            # the signal to switch to Settings-based behaviour.
            if self._is_service_context_deprecation_error(exc):
                return None
            raise

    def _apply_runtime_components(self, *, embed_model: Any, llm: Any) -> None:
        settings_obj = getattr(self, "llamaindex_settings", None)
        if settings_obj is None:
            return
        try:
            setattr(settings_obj, "embed_model", embed_model)
        except Exception:
            pass
        try:
            setattr(settings_obj, "llm", llm)
        except Exception:
            pass

    @staticmethod
    def _normalise_runtime_entry(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(entry, Mapping):
            return None
        provider = str(entry.get("provider") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            return None
        payload: dict[str, Any] = {
            "provider": provider,
            "model": model,
        }
        host = str(entry.get("host") or "").strip()
        if host:
            payload["host"] = host
        scope = str(entry.get("scope") or "").strip()
        if scope:
            payload["scope"] = scope
        return payload

    @staticmethod
    def _build_component_signature(
        kind: str,
        entry: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        normalised = LlamaIndexRAGService._normalise_runtime_entry(entry)
        if normalised is None:
            return None
        return {
            "schema_version": "rag_component_signature.v1",
            "kind": str(kind or "").strip() or "unknown",
            "provider": normalised["provider"],
            "model": normalised["model"],
            "host": normalised.get("host"),
        }

    @staticmethod
    def _build_runtime_component_from_resolution(
        resolution: Mapping[str, Any] | None,
        *,
        kind: str,
    ) -> Any:
        if not isinstance(resolution, Mapping):
            return None
        effective = LlamaIndexRAGService._normalise_runtime_entry(
            resolution.get("effective") if isinstance(resolution, Mapping) else None
        )
        if effective is None:
            return None
        selection_source = str(resolution.get("selection_source") or "").strip() or "unknown"
        if kind == "embedder":
            return _ConfiguredEmbeddingModel(
                provider=effective["provider"],
                model_name=effective["model"],
                host=effective.get("host"),
                selection_source=selection_source,
            )
        if kind == "llm":
            return _ConfiguredRuntimeLLM(
                provider=effective["provider"],
                model_name=effective["model"],
                host=effective.get("host"),
                selection_source=selection_source,
            )
        return None

    def _refresh_runtime_configuration(self) -> None:
        from ..settings_service import resolve_rag_embedder_setting, resolve_rag_llm_setting

        embedder_resolution = resolve_rag_embedder_setting()
        llm_resolution = resolve_rag_llm_setting()
        embed_model = self._build_runtime_component_from_resolution(
            embedder_resolution,
            kind="embedder",
        )
        runtime_llm = self._build_runtime_component_from_resolution(
            llm_resolution,
            kind="llm",
        )
        runtime_configuration: dict[str, Any] = {
            "schema_version": "rag_runtime_configuration.v1",
            "embedder_resolution": dict(embedder_resolution),
            "llm_resolution": dict(llm_resolution),
            "embedding_signature": self._build_component_signature(
                "embedder",
                embedder_resolution.get("effective")
                if isinstance(embedder_resolution, Mapping)
                else None,
            ),
            "llm_signature": self._build_component_signature(
                "llm",
                llm_resolution.get("effective")
                if isinstance(llm_resolution, Mapping)
                else None,
            ),
        }
        serialised = json.dumps(
            runtime_configuration,
            sort_keys=True,
            separators=(",", ":"),
        )
        if serialised == self._runtime_configuration_serialized:
            return

        self._indices.clear()
        self.service_context = self._build_service_context(
            embed_model=embed_model,
            llm=runtime_llm,
        )
        self._apply_runtime_components(embed_model=embed_model, llm=runtime_llm)
        self._runtime_configuration = runtime_configuration
        self._runtime_configuration_serialized = serialised

    def _index_runtime_kwargs(self) -> Dict[str, Any]:
        self._refresh_runtime_configuration()
        if self.service_context is not None:
            return {"service_context": self.service_context}
        return {}

    def _get_runtime_component(self, component_name: str) -> Any:
        self._refresh_runtime_configuration()
        if self.service_context is not None:
            component = getattr(self.service_context, component_name, None)
            if component is not None:
                return component

        settings_obj = getattr(self, "llamaindex_settings", None)
        if settings_obj is None:
            return None

        try:
            return getattr(settings_obj, component_name, None)
        except Exception:
            return None

    def get_runtime_embed_model(self) -> Any:
        return self._get_runtime_component("embed_model")

    def get_runtime_llm(self) -> Any:
        return self._get_runtime_component("llm")

    def get_runtime_configuration_summary(self) -> dict[str, Any]:
        self._refresh_runtime_configuration()
        return dict(self._runtime_configuration or {})

    def _temporarily_bound_query_embed_model(self) -> tuple[Any, Dict[str, Any]]:
        """Keep semantic-query embedding failures bounded so callers can fall back."""
        embed_model = self.get_runtime_embed_model()
        if embed_model is None:
            return None, {}

        previous: Dict[str, Any] = {}
        settings_changed = False
        current_retries = getattr(embed_model, "max_retries", None)
        if isinstance(current_retries, int) and current_retries > _QUERY_EMBED_MAX_RETRIES:
            previous["max_retries"] = current_retries
            setattr(embed_model, "max_retries", _QUERY_EMBED_MAX_RETRIES)
            settings_changed = True

        current_timeout = getattr(embed_model, "timeout", None)
        if isinstance(current_timeout, (int, float)) and current_timeout > _QUERY_EMBED_TIMEOUT_SECONDS:
            previous["timeout"] = current_timeout
            setattr(embed_model, "timeout", _QUERY_EMBED_TIMEOUT_SECONDS)
            settings_changed = True

        # LlamaIndex may keep a cached OpenAI client built with the previous retry
        # budget. Reset it so the bounded query settings actually take effect.
        if settings_changed:
            if hasattr(embed_model, "_client"):
                previous["_client"] = getattr(embed_model, "_client")
                setattr(embed_model, "_client", None)
            if hasattr(embed_model, "_aclient"):
                previous["_aclient"] = getattr(embed_model, "_aclient")
                setattr(embed_model, "_aclient", None)

        return embed_model, previous

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

    def _namespace_metadata_path(self, namespace: str) -> str:
        return os.path.join(
            self._namespace_persist_dir(namespace),
            _INDEX_METADATA_FILENAME,
        )

    def _read_namespace_metadata(self, namespace: str) -> dict[str, Any] | None:
        metadata_path = self._namespace_metadata_path(namespace)
        if not os.path.isfile(metadata_path):
            return None
        try:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _write_namespace_metadata(self, namespace: str) -> None:
        runtime_summary = self.get_runtime_configuration_summary()
        payload = {
            "schema_version": "rag_index_metadata.v1",
            "namespace": namespace,
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "embedding_signature": runtime_summary.get("embedding_signature"),
            "llm_signature": runtime_summary.get("llm_signature"),
        }
        os.makedirs(self._namespace_persist_dir(namespace), exist_ok=True)
        with open(self._namespace_metadata_path(namespace), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)

    def _get_current_embedding_signature(self) -> dict[str, Any] | None:
        runtime_summary = self.get_runtime_configuration_summary()
        signature = runtime_summary.get("embedding_signature")
        return dict(signature) if isinstance(signature, dict) else None

    def _build_namespace_runtime_state(self, namespace: str) -> dict[str, Any]:
        persist_dir = self._namespace_persist_dir(namespace)
        metadata_path = self._namespace_metadata_path(namespace)
        has_persisted_index = False
        try:
            has_persisted_index = os.path.isdir(persist_dir) and bool(os.listdir(persist_dir))
        except Exception:
            has_persisted_index = os.path.isdir(persist_dir)

        metadata = self._read_namespace_metadata(namespace)
        current_signature = self._get_current_embedding_signature()
        stored_signature_raw = (
            metadata.get("embedding_signature")
            if isinstance(metadata, dict)
            else None
        )
        stored_signature = (
            dict(stored_signature_raw)
            if isinstance(stored_signature_raw, Mapping)
            else None
        )
        runtime_summary = self.get_runtime_configuration_summary()
        embedder_resolution = runtime_summary.get("embedder_resolution")
        unresolved_reason = (
            str(embedder_resolution.get("reason") or "").strip()
            if isinstance(embedder_resolution, Mapping)
            else ""
        )

        status = "compatible"
        compatible = True
        detail = "Namespace embedding signature matches the current runtime configuration."
        if current_signature is None:
            status = "embedder_unconfigured"
            compatible = False
            detail = unresolved_reason or "No effective RAG embedder is configured."
        elif not has_persisted_index:
            status = "missing_index"
            detail = "No persisted index exists for this namespace yet."
        elif metadata is None:
            status = "signature_missing"
            compatible = False
            detail = (
                "Persisted index metadata is missing, so embedding compatibility "
                "cannot be verified. Rebuild is required."
            )
        elif stored_signature != current_signature:
            status = "embedding_signature_mismatch"
            compatible = False
            detail = (
                "Persisted index embeddings were built with a different embedding "
                "signature. Rebuild is required before this namespace is trustworthy."
            )

        return {
            "namespace": namespace,
            "persist_dir": persist_dir,
            "metadata_path": metadata_path,
            "has_persisted_index": has_persisted_index,
            "compatible": compatible,
            "status": status,
            "detail": detail,
            "current_embedding_signature": current_signature,
            "stored_embedding_signature": stored_signature,
            "metadata": metadata,
        }

    def get_namespace_runtime_state(self, namespace: Optional[str] = None) -> dict[str, Any]:
        effective_namespace = self._resolve_effective_namespace(namespace)
        state = self._build_namespace_runtime_state(effective_namespace)
        self._namespace_runtime_state[effective_namespace] = dict(state)
        return state

    def _maybe_load_index(self, namespace: str) -> Any:
        state = self.get_namespace_runtime_state(namespace)
        if not bool(state.get("compatible", False)):
            return None

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
            index = load_index_from_storage(
                storage_context, **self._index_runtime_kwargs()
            )
        except Exception:
            return None

        self._indices[namespace] = index
        return index

    def _get_or_create_index(self, namespace: str, documents: List[Any]) -> Any:
        state = self.get_namespace_runtime_state(namespace)
        if state.get("status") in {"signature_missing", "embedding_signature_mismatch"}:
            self.reset_namespace(namespace)
        index = self._maybe_load_index(namespace)
        if index is not None:
            return index

        persist_dir = self._namespace_persist_dir(namespace)
        os.makedirs(persist_dir, exist_ok=True)
        index = VectorStoreIndex.from_documents(
            documents, **self._index_runtime_kwargs()
        )
        index.storage_context.persist(persist_dir=persist_dir)
        self._write_namespace_metadata(namespace)
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
        if self.get_runtime_embed_model() is None:
            state = self.get_namespace_runtime_state(effective_namespace)
            raise RuntimeError(
                state.get("detail")
                or "Embeddings unavailable: LlamaIndex embed_model is not configured."
            )

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
            self._write_namespace_metadata(effective_namespace)

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

    def reset_namespace(
        self,
        namespace: Optional[str] = None,
    ) -> None:
        effective_namespace = self._resolve_effective_namespace(namespace)
        self._indices.pop(effective_namespace, None)
        self._namespace_runtime_state.pop(effective_namespace, None)

        persist_dir = os.path.abspath(self._namespace_persist_dir(effective_namespace))
        namespaces_root = os.path.abspath(
            os.path.join(self.persistence_dir, "namespaces")
        )

        try:
            common_root = os.path.commonpath([persist_dir, namespaces_root])
        except ValueError as exc:
            raise RuntimeError(
                f"RAG namespace reset path mismatch for '{effective_namespace}': {exc}"
            ) from exc

        if common_root != namespaces_root:
            raise RuntimeError(
                f"Refusing to reset namespace outside persistence root: {effective_namespace}"
            )

        if os.path.isdir(persist_dir):
            shutil.rmtree(persist_dir)
        elif os.path.exists(persist_dir):
            raise RuntimeError(
                f"Namespace persistence path is not a directory: {persist_dir}"
            )

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
        namespace_state = self.get_namespace_runtime_state(effective_namespace)
        if not bool(namespace_state.get("compatible", False)):
            try:
                self._last_query_info = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "effective_namespace": effective_namespace,
                    "query_length": len(query_text or ""),
                    "top_k": int(top_k),
                    "returned": 0,
                    "compatibility_status": namespace_state.get("status"),
                    "compatibility_detail": namespace_state.get("detail"),
                }
            except Exception:
                pass
            return []

        index = self._maybe_load_index(effective_namespace)
        if not index:
            return []

        # Note: Metadata filtering support varies by vector store implementation.
        # To ensure correctness, we do coarse retrieval first then apply filtering
        # locally.
        similarity_top_k = max(top_k * 10, top_k)
        start = time.perf_counter()
        embed_model, previous_embed_settings = self._temporarily_bound_query_embed_model()
        try:
            retriever = index.as_retriever(similarity_top_k=similarity_top_k)
            nodes = retriever.retrieve(query_text)
        finally:
            if embed_model is not None:
                for attr, value in previous_embed_settings.items():
                    setattr(embed_model, attr, value)

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
        embed_model = self.get_runtime_embed_model()
        if embed_model is None:
            raise RuntimeError(
                "Embeddings unavailable: LlamaIndex embed_model is not configured."
            )

        return [embed_model.get_text_embedding(t) for t in texts]
