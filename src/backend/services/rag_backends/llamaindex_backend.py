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

import errno
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Dict, Any, Optional, List, Tuple, Mapping
from uuid import uuid4

try:  # pragma: no cover - platform-specific import
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:  # pragma: no cover - platform-specific import
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

from ..rag_service import RAGQueryResults, RAGService, build_rag_retrieval_state
from ...utils.concept_id_utils import normalise_concept_id_for_compare


_LLAMAINDEX_MISSING_MESSAGE = (
    "LlamaIndex dependencies missing. Install the optional dependency group(s) "
    "that provide `llama-index` for this backend."
)
_QUERY_EMBED_ADVISORY_SECONDS = max(
    0.1,
    float(
        os.environ.get("VON_RAG_QUERY_EMBED_ADVISORY_SECONDS")
        or os.environ.get("VON_RAG_QUERY_EMBED_TIMEOUT_SECONDS", "8")
    ),
)
_QUERY_EMBED_MAX_RETRIES = 0
_INDEX_METADATA_FILENAME = "index_metadata.json"
_RUNTIME_CONFIGURATION_CACHE_TTL_SECONDS = 2.0
_WINDOWS_NAMESPACE_IO_MAX_RETRIES = 6
_WINDOWS_NAMESPACE_IO_RETRY_DELAY_SECONDS = 0.05


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

logger = logging.getLogger(__name__)


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
        embeddings_mod = importlib.import_module(
            "llama_index.core.base.embeddings.base"
        )
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
            CompletionResponse = getattr(
                llms_mod, "CompletionResponse", CompletionResponse
            )
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
        self._index_cache_generations: Dict[str, str] = {}
        self._persisted_index_cache_namespaces: set[str] = set()
        self._namespace_runtime_state: Dict[str, Dict[str, Any]] = {}
        self._namespace_retrieval_states: Dict[str, Dict[str, Any]] = {}
        self._retrieval_state_lock = threading.Lock()
        self._namespace_locks: Dict[str, threading.RLock] = {}
        self._namespace_locks_guard = threading.Lock()
        self._runtime_configuration_lock = threading.RLock()
        self._runtime_configuration: Dict[str, Any] | None = None
        self._runtime_configuration_serialized: str | None = None
        self._runtime_configuration_refreshed_monotonic: float = 0.0
        self._runtime_embed_model: Any = None
        self._runtime_llm: Any = None

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

    def _build_service_context(
        self, *, embed_model: Any = None, llm: Any = None
    ) -> Any:
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
    def _normalise_runtime_entry(
        entry: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
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
        selection_source = (
            str(resolution.get("selection_source") or "").strip() or "unknown"
        )
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

    def invalidate_runtime_configuration_cache(self) -> None:
        with self._runtime_configuration_lock:
            self._runtime_configuration_refreshed_monotonic = 0.0

    def _refresh_runtime_configuration(self, *, force: bool = False) -> None:
        from ..settings_service import (
            resolve_rag_embedder_setting,
            resolve_rag_llm_setting,
        )

        with self._runtime_configuration_lock:
            now = time.monotonic()
            if (
                not force
                and self._runtime_configuration is not None
                and self._runtime_configuration_serialized is not None
                and self._runtime_configuration_refreshed_monotonic > 0.0
                and (now - self._runtime_configuration_refreshed_monotonic)
                < _RUNTIME_CONFIGURATION_CACHE_TTL_SECONDS
            ):
                return

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
                self._runtime_configuration_refreshed_monotonic = now
                return

            self._indices.clear()
            self.service_context = self._build_service_context(
                embed_model=embed_model,
                llm=runtime_llm,
            )
            self._runtime_embed_model = embed_model
            self._runtime_llm = runtime_llm
            self._apply_runtime_components(embed_model=embed_model, llm=runtime_llm)
            self._runtime_configuration = runtime_configuration
            self._runtime_configuration_serialized = serialised
            self._runtime_configuration_refreshed_monotonic = now

    def _index_runtime_kwargs(self, *, embed_model: Any) -> Dict[str, Any]:
        if self.service_context is not None:
            return {"service_context": self.service_context}
        return {"embed_model": embed_model}

    def _get_runtime_component(self, component_name: str) -> Any:
        self._refresh_runtime_configuration()
        if component_name == "embed_model":
            return self._runtime_embed_model
        if component_name == "llm":
            return self._runtime_llm
        return None

    def get_runtime_embed_model(self) -> Any:
        return self._get_runtime_component("embed_model")

    def get_runtime_llm(self) -> Any:
        return self._get_runtime_component("llm")

    def get_runtime_configuration_summary(self) -> dict[str, Any]:
        self._refresh_runtime_configuration()
        return dict(self._runtime_configuration or {})

    def _temporarily_bound_query_embed_model(self) -> tuple[Any, Dict[str, Any]]:
        """Disable excess retries without imposing a query-time deadline.

        Provider and transport resource boundaries remain authoritative. The
        former fixed eight-second timeout was an orchestration cutoff: it made
        a slow, otherwise usable embedding fail and forced a retrieval fallback.
        """
        embed_model = self.get_runtime_embed_model()
        if embed_model is None:
            return None, {}

        previous: Dict[str, Any] = {}
        settings_changed = False
        current_retries = getattr(embed_model, "max_retries", None)
        if (
            isinstance(current_retries, int)
            and current_retries > _QUERY_EMBED_MAX_RETRIES
        ):
            previous["max_retries"] = current_retries
            setattr(embed_model, "max_retries", _QUERY_EMBED_MAX_RETRIES)
            settings_changed = True

        # LlamaIndex may keep a cached OpenAI client built with the previous retry
        # count. Reset it so the retry setting actually takes effect.
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

    @staticmethod
    def _is_windows_sharing_violation(exc: BaseException) -> bool:
        if not isinstance(exc, OSError):
            return False
        if getattr(exc, "winerror", None) == 32:
            return True
        message = str(exc).strip().lower()
        return (
            "used by another process" in message or "cannot access the file" in message
        )

    def _get_namespace_lock(self, namespace: str) -> threading.RLock:
        with self._namespace_locks_guard:
            lock = self._namespace_locks.get(namespace)
            if lock is None:
                lock = threading.RLock()
                self._namespace_locks[namespace] = lock
            return lock

    @staticmethod
    def _is_windows_lock_contention(exc: OSError) -> bool:
        return getattr(exc, "winerror", None) in {32, 33} or exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
        }

    @contextmanager
    def _namespace_process_lock(self, namespace: str):
        """Serialise one namespace mutation across worker/app processes."""

        lock_root = os.path.join(self.persistence_dir, ".namespace_locks")
        os.makedirs(lock_root, exist_ok=True)
        lock_path = os.path.join(
            lock_root,
            f"{self._namespace_dirname(namespace)}.lock",
        )
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                while True:
                    try:
                        _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if not self._is_windows_lock_contention(exc):
                            raise
                        time.sleep(_WINDOWS_NAMESPACE_IO_RETRY_DELAY_SECONDS)
            else:  # pragma: no cover - unsupported platform
                raise RuntimeError(
                    "No inter-process file-lock implementation is available"
                )
            try:
                yield
            finally:
                handle.seek(0)
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:  # pragma: no cover - Windows
                    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)

    @contextmanager
    def _namespace_mutation_lock(self, namespace: str):
        with self._get_namespace_lock(namespace):
            with self._namespace_process_lock(namespace):
                yield

    def _run_namespace_io(
        self,
        namespace: str,
        operation: Any,
        *,
        action_label: str,
    ) -> Any:
        attempt = 0
        while True:
            try:
                return operation()
            except OSError as exc:
                if (
                    not self._is_windows_sharing_violation(exc)
                    or attempt >= _WINDOWS_NAMESPACE_IO_MAX_RETRIES
                ):
                    raise
                attempt += 1
                logger.warning(
                    "Transient namespace I/O sharing violation during %s for '%s' "
                    "(attempt %d/%d): %s",
                    action_label,
                    namespace,
                    attempt,
                    _WINDOWS_NAMESPACE_IO_MAX_RETRIES,
                    exc,
                )
                time.sleep(_WINDOWS_NAMESPACE_IO_RETRY_DELAY_SECONDS * attempt)

    def _read_namespace_metadata(self, namespace: str) -> dict[str, Any] | None:
        metadata_path = self._namespace_metadata_path(namespace)
        if not os.path.isfile(metadata_path):
            return None

        def _read_payload() -> Any:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                return json.load(handle)

        try:
            with self._get_namespace_lock(namespace):
                payload = self._run_namespace_io(
                    namespace,
                    _read_payload,
                    action_label="read namespace metadata",
                )
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _capture_embedding_runtime(
        self,
        namespace: str,
    ) -> tuple[dict[str, Any], Any, Dict[str, Any]]:
        with self._runtime_configuration_lock:
            self._refresh_runtime_configuration()
            runtime_summary = dict(self._runtime_configuration or {})
            embedding_signature = runtime_summary.get("embedding_signature")
            embed_model = self._runtime_embed_model
            if isinstance(embedding_signature, Mapping) and embed_model is not None:
                return (
                    runtime_summary,
                    embed_model,
                    self._index_runtime_kwargs(embed_model=embed_model),
                )

            embedder_resolution = runtime_summary.get("embedder_resolution")
            reason = (
                str(embedder_resolution.get("reason") or "").strip()
                if isinstance(embedder_resolution, Mapping)
                else ""
            )
            raise RuntimeError(
                reason
                or (
                    "RAG index mutation requires a resolved embedding configuration "
                    f"for namespace '{namespace}'."
                )
            )

    def _write_namespace_metadata(
        self,
        namespace: str,
        *,
        runtime_summary: Mapping[str, Any] | None = None,
        persist_dir_override: str | None = None,
    ) -> str:
        if runtime_summary is None:
            runtime_summary, _, _ = self._capture_embedding_runtime(namespace)
        embedding_signature = runtime_summary.get("embedding_signature")
        if not isinstance(embedding_signature, Mapping):
            raise RuntimeError(
                "RAG index metadata requires a resolved embedding signature."
            )
        index_generation = str(uuid4())
        payload = {
            "schema_version": "rag_index_metadata.v1",
            "namespace": namespace,
            "index_generation": index_generation,
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "embedding_signature": dict(embedding_signature),
            "llm_signature": runtime_summary.get("llm_signature"),
        }
        persist_dir = persist_dir_override or self._namespace_persist_dir(namespace)
        metadata_path = os.path.join(persist_dir, _INDEX_METADATA_FILENAME)
        with self._get_namespace_lock(namespace):
            os.makedirs(persist_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix="index_metadata.",
                suffix=".tmp",
                dir=persist_dir,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, indent=2)
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                self._run_namespace_io(
                    namespace,
                    lambda: os.replace(temp_path, metadata_path),
                    action_label="replace namespace metadata",
                )
            finally:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
        return index_generation

    def _advance_namespace_generation(self, namespace: str) -> str:
        """Publish a new cache generation without changing index semantics."""

        existing = self._read_namespace_metadata(namespace)
        embedding_signature = (
            existing.get("embedding_signature")
            if isinstance(existing, Mapping)
            else None
        )
        runtime_summary: Mapping[str, Any] | None = None
        if isinstance(embedding_signature, Mapping):
            runtime_summary = {
                "embedding_signature": dict(embedding_signature),
                "llm_signature": existing.get("llm_signature"),
            }
        return self._write_namespace_metadata(
            namespace,
            runtime_summary=runtime_summary,
        )

    def _require_namespace_mutation_compatible(
        self,
        namespace: str,
        *,
        embedding_signature: Mapping[str, Any],
    ) -> None:
        persist_dir = self._namespace_persist_dir(namespace)
        try:
            has_persisted_index = os.path.isdir(persist_dir) and bool(
                os.listdir(persist_dir)
            )
        except Exception:
            has_persisted_index = os.path.isdir(persist_dir)
        if not has_persisted_index:
            return

        metadata = self._read_namespace_metadata(namespace)
        stored_signature = (
            metadata.get("embedding_signature")
            if isinstance(metadata, Mapping)
            else None
        )
        if not isinstance(stored_signature, Mapping):
            raise RuntimeError(
                "rebuild_required: persisted RAG index metadata has no verified "
                f"embedding signature for namespace '{namespace}'."
            )
        if dict(stored_signature) != dict(embedding_signature):
            raise RuntimeError(
                "rebuild_required: persisted RAG index embeddings do not match "
                f"the configured embedder for namespace '{namespace}'."
            )

    def _get_current_embedding_signature(self) -> dict[str, Any] | None:
        runtime_summary = self.get_runtime_configuration_summary()
        signature = runtime_summary.get("embedding_signature")
        return dict(signature) if isinstance(signature, dict) else None

    def _build_namespace_runtime_state(
        self,
        namespace: str,
        *,
        non_blocking: bool = True,
    ) -> dict[str, Any]:
        """Build a diagnostic snapshot of namespace runtime state.

        JVNAUTOSCI-2124: This is a *read-only* diagnostic surface. Callers
        include workflow discovery's runtime-state probe, which runs on the
        latency-sensitive turn path under a sub-second budget. Holding the
        per-namespace write lock here would serialise diagnostic reads behind
        long-running rebuilds (``_get_or_create_index`` holds the same lock
        through ``VectorStoreIndex.from_documents`` and
        ``storage_context.persist``, which can take 25-30s for the
        workflow-capability namespace).

        We therefore default to a non-blocking acquire. When another thread
        holds the lock (almost certainly a rebuild worker), return a fast
        ``rebuild_in_progress`` snapshot so callers can short-circuit
        gracefully instead of blocking. Callers that explicitly want strict
        serialisation can pass ``non_blocking=False``; reentrant calls from
        the lock holder always succeed because the namespace lock is an
        ``RLock``.
        """

        persist_dir = self._namespace_persist_dir(namespace)
        metadata_path = self._namespace_metadata_path(namespace)
        lock = self._get_namespace_lock(namespace)
        if non_blocking:
            acquired = lock.acquire(blocking=False)
        else:
            lock.acquire()
            acquired = True
        if not acquired:
            # Another thread holds the namespace lock. Return a quick
            # diagnostic snapshot so latency-sensitive callers can bail out
            # without blocking on the rebuild. ``compatible=False`` ensures
            # downstream readiness gates treat this as "index unsafe to use
            # right now" rather than silently falling through to a stale
            # success path.
            return {
                "namespace": namespace,
                "persist_dir": persist_dir,
                "metadata_path": metadata_path,
                "has_persisted_index": None,
                "compatible": False,
                "status": "rebuild_in_progress",
                "detail": (
                    "Namespace lock is held by another thread (almost "
                    "certainly a background rebuild). Returning a "
                    "non-blocking diagnostic snapshot; retry once the "
                    "rebuild completes for an authoritative state."
                ),
                "current_embedding_signature": None,
                "stored_embedding_signature": None,
                "metadata": None,
                "lock_contended": True,
            }
        try:
            has_persisted_index = False
            try:
                has_persisted_index = os.path.isdir(persist_dir) and bool(
                    os.listdir(persist_dir)
                )
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
        finally:
            lock.release()

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
            "lock_contended": False,
        }

    def get_namespace_runtime_state(
        self,
        namespace: Optional[str] = None,
        *,
        non_blocking: bool = True,
    ) -> dict[str, Any]:
        """Return diagnostic runtime state for ``namespace`` (default: all).

        JVNAUTOSCI-2124: Defaults to ``non_blocking=True`` so that callers
        on the latency-sensitive turn path (workflow discovery, status
        endpoints) cannot stall behind a rebuild worker holding the
        namespace lock. Strict callers that need to observe the
        post-rebuild authoritative state can pass ``non_blocking=False``.
        """
        effective_namespace = self._resolve_effective_namespace(namespace)
        state = self._build_namespace_runtime_state(
            effective_namespace, non_blocking=non_blocking
        )
        # Only update the cached snapshot when we observed authoritative
        # state. A contended snapshot is intentionally fast and partial; do
        # not let it overwrite a previously good cache entry.
        if not state.get("lock_contended", False):
            self._namespace_runtime_state[effective_namespace] = dict(state)
        return state

    def _query_results_with_state(
        self,
        results: Iterable[Dict[str, Any]],
        *,
        namespace: str,
        status: str,
        cause: str | None = None,
        detail: str | None = None,
        candidate_count: int | None = None,
        filtered_candidate_count: int | None = None,
        candidate_limit: int | None = None,
        candidate_limit_reached: bool | None = None,
    ) -> RAGQueryResults:
        materialised_results = list(results)
        state = build_rag_retrieval_state(
            status,
            result_count=len(materialised_results),
            cause=cause,
            detail=detail,
            candidate_count=candidate_count,
            filtered_candidate_count=filtered_candidate_count,
            candidate_limit=candidate_limit,
            candidate_limit_reached=candidate_limit_reached,
        )
        with self._retrieval_state_lock:
            self._namespace_retrieval_states[namespace] = dict(state)
        return RAGQueryResults(
            materialised_results,
            retrieval_state=state,
        )

    def get_last_retrieval_state(
        self,
        namespace: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """Return the latest state for one namespace without exposing index data."""

        effective_namespace = self._resolve_effective_namespace(namespace)
        with self._retrieval_state_lock:
            state = self._namespace_retrieval_states.get(effective_namespace)
            return dict(state) if isinstance(state, dict) else None

    def _maybe_load_index(
        self,
        namespace: str,
        *,
        runtime_index_kwargs: Mapping[str, Any] | None = None,
        expected_embedding_signature: Mapping[str, Any] | None = None,
    ) -> Any:
        with self._get_namespace_lock(namespace):
            if expected_embedding_signature is not None:
                self._require_namespace_mutation_compatible(
                    namespace,
                    embedding_signature=expected_embedding_signature,
                )
            else:
                state = self.get_namespace_runtime_state(namespace)
                if not bool(state.get("compatible", False)):
                    return None

            persist_dir = self._namespace_persist_dir(namespace)
            try:
                has_persisted_state = os.path.isdir(persist_dir) and bool(
                    os.listdir(persist_dir)
                )
            except OSError:
                has_persisted_state = os.path.isdir(persist_dir)
            metadata = (
                self._read_namespace_metadata(namespace)
                if has_persisted_state
                else None
            )
            disk_generation = (
                str(metadata.get("index_generation") or "").strip()
                if isinstance(metadata, Mapping)
                else ""
            )
            if namespace in self._indices:
                # Ephemeral/test indices have no persisted state. Persisted
                # caches are reusable only while their generation still
                # matches the version published by the last process holding
                # the namespace mutation lock.
                cache_was_persisted = (
                    namespace in self._persisted_index_cache_namespaces
                    or bool(self._index_cache_generations.get(namespace))
                )
                if (not has_persisted_state and not cache_was_persisted) or (
                    disk_generation
                    and self._index_cache_generations.get(namespace) == disk_generation
                ):
                    return self._indices[namespace]
                self._indices.pop(namespace, None)
                self._index_cache_generations.pop(namespace, None)
                self._persisted_index_cache_namespaces.discard(namespace)

            if not os.path.isdir(persist_dir):
                return None

            # Avoid calling LlamaIndex load on an empty directory.
            try:
                if not os.listdir(persist_dir):
                    return None
            except Exception:
                return None

            try:
                if runtime_index_kwargs is None:
                    _, _, runtime_index_kwargs = self._capture_embedding_runtime(
                        namespace
                    )
                storage_context = StorageContext.from_defaults(persist_dir=persist_dir)
                index = load_index_from_storage(
                    storage_context, **dict(runtime_index_kwargs)
                )
            except Exception:
                return None

            self._indices[namespace] = index
            self._persisted_index_cache_namespaces.add(namespace)
            if disk_generation:
                self._index_cache_generations[namespace] = disk_generation
            return index

    def _get_or_create_index(
        self,
        namespace: str,
        documents: List[Any],
        *,
        runtime_summary: Mapping[str, Any],
        runtime_index_kwargs: Mapping[str, Any],
    ) -> Any:
        with self._get_namespace_lock(namespace):
            embedding_signature = runtime_summary.get("embedding_signature")
            if not isinstance(embedding_signature, Mapping):
                raise RuntimeError(
                    "RAG index creation requires a resolved embedding signature."
                )
            self._require_namespace_mutation_compatible(
                namespace,
                embedding_signature=embedding_signature,
            )
            index = self._maybe_load_index(
                namespace,
                runtime_index_kwargs=runtime_index_kwargs,
                expected_embedding_signature=embedding_signature,
            )
            if index is not None:
                return index

            persist_dir = self._namespace_persist_dir(namespace)
            namespaces_root = os.path.dirname(persist_dir)
            os.makedirs(namespaces_root, exist_ok=True)
            staging_dir = tempfile.mkdtemp(
                prefix=f".{self._namespace_dirname(namespace)}.creating.",
                dir=namespaces_root,
            )
            try:
                index = VectorStoreIndex.from_documents(
                    documents, **dict(runtime_index_kwargs)
                )
                index.storage_context.persist(persist_dir=staging_dir)
                index_generation = self._write_namespace_metadata(
                    namespace,
                    runtime_summary=runtime_summary,
                    persist_dir_override=staging_dir,
                )
                # The whole initial index, including its verified embedding
                # signature, becomes visible as one same-volume directory
                # rename. A failed build leaves no misleading final namespace.
                os.rename(staging_dir, persist_dir)
                staging_dir = ""
                self._indices[namespace] = index
                self._index_cache_generations[namespace] = index_generation
                self._persisted_index_cache_namespaces.add(namespace)
                return index
            finally:
                if staging_dir and os.path.isdir(staging_dir):
                    shutil.rmtree(staging_dir)

    def upsert_documents(
        self,
        docs: Iterable[Dict[str, Any]],
        *,
        namespace: Optional[str] = None,
        allow_partial_failures: bool = True,
    ) -> Tuple[int, int]:
        """Idempotently insert or refresh documents and durably persist them."""
        effective_namespace = self._resolve_effective_namespace(namespace)
        runtime_summary, _, runtime_index_kwargs = self._capture_embedding_runtime(
            effective_namespace
        )
        embedding_signature = runtime_summary["embedding_signature"]

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
            else:
                metadata = dict(metadata)
            metadata["namespace"] = effective_namespace
            l_doc.metadata = metadata
            if metadata.get("assertion_form") == "standalone_text":
                # The raw assertion body is the entire semantic embedding
                # input. Keep authority, lifecycle, context, and provenance
                # metadata in the docstore for live filtering and reasoning,
                # but do not send operational tenant identifiers to an
                # external embedder or let them distort similarity.
                excluded_keys = sorted(metadata)
                l_doc.excluded_embed_metadata_keys = excluded_keys
                l_doc.excluded_llm_metadata_keys = excluded_keys
            llama_docs.append(l_doc)

        if not llama_docs:
            return (0, 0)

        with self._namespace_mutation_lock(effective_namespace):
            self._require_namespace_mutation_compatible(
                effective_namespace,
                embedding_signature=embedding_signature,
            )
            index = self._maybe_load_index(
                effective_namespace,
                runtime_index_kwargs=runtime_index_kwargs,
                expected_embedding_signature=embedding_signature,
            )
            if index is None:
                index = self._get_or_create_index(
                    effective_namespace,
                    llama_docs,
                    runtime_summary=runtime_summary,
                    runtime_index_kwargs=runtime_index_kwargs,
                )
                return (len(llama_docs), 0)

            success = 0
            failed = 0

            def _persist() -> None:
                index.storage_context.persist(
                    persist_dir=self._namespace_persist_dir(effective_namespace)
                )
                generation = self._write_namespace_metadata(
                    effective_namespace,
                    runtime_summary=runtime_summary,
                )
                self._index_cache_generations[effective_namespace] = generation
                self._persisted_index_cache_namespaces.add(effective_namespace)

            # ``refresh_ref_docs`` compares each stable document ID and updates
            # changed text/metadata instead of accumulating duplicate nodes on
            # retries or assertion revisions. It also inserts missing IDs.
            refresh_ref_docs = getattr(index, "refresh_ref_docs", None)
            if callable(refresh_ref_docs):
                try:
                    refresh_ref_docs(llama_docs)
                    success = len(llama_docs)
                except Exception:
                    if not allow_partial_failures:
                        raise
                    failed = len(llama_docs)
            else:
                # Compatibility path for an older index implementation: update
                # each stable ref-doc ID (delete plus insert), never append it.
                update_ref_doc = getattr(index, "update_ref_doc", None)
                if not callable(update_ref_doc):
                    raise RuntimeError(
                        "RAG index does not support stable document refresh"
                    )
                for l_doc in llama_docs:
                    try:
                        update_ref_doc(l_doc)
                        success += 1
                    except Exception:
                        failed += 1
                        if not allow_partial_failures:
                            raise

            if success > 0:
                # A persistence failure means the durable effect is unknown;
                # propagate it so the assertion queue remains retryable.
                _persist()

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
        with self._namespace_mutation_lock(effective_namespace):
            index = self._maybe_load_index(effective_namespace)
            if not index:
                persist_dir = self._namespace_persist_dir(effective_namespace)
                try:
                    namespace_has_state = os.path.isdir(persist_dir) and bool(
                        os.listdir(persist_dir)
                    )
                except OSError as exc:
                    raise RuntimeError(
                        "RAG namespace state could not be inspected for deletion"
                    ) from exc
                if namespace_has_state:
                    raise RuntimeError(
                        "Existing RAG namespace could not be loaded for deletion"
                    )
                return 0

            count = 0
            for doc_id in ids:
                index.delete_ref_doc(doc_id, delete_from_docstore=True)
                count += 1
            if count > 0:
                index.storage_context.persist(
                    persist_dir=self._namespace_persist_dir(effective_namespace)
                )
                generation = self._advance_namespace_generation(effective_namespace)
                self._index_cache_generations[effective_namespace] = generation
                self._persisted_index_cache_namespaces.add(effective_namespace)
            return count

    def reset_namespace(
        self,
        namespace: Optional[str] = None,
    ) -> None:
        effective_namespace = self._resolve_effective_namespace(namespace)
        with self._namespace_mutation_lock(effective_namespace):
            self._indices.pop(effective_namespace, None)
            self._index_cache_generations.pop(effective_namespace, None)
            self._persisted_index_cache_namespaces.discard(effective_namespace)
            self._namespace_runtime_state.pop(effective_namespace, None)
            with self._retrieval_state_lock:
                self._namespace_retrieval_states.pop(effective_namespace, None)

            persist_dir = os.path.abspath(
                self._namespace_persist_dir(effective_namespace)
            )
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
                self._run_namespace_io(
                    effective_namespace,
                    lambda: shutil.rmtree(persist_dir),
                    action_label="reset namespace persistence",
                )
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
        namespace_status = str(namespace_state.get("status") or "").strip()
        # An in-memory index was built under the current runtime configuration
        # and is usable even if no persisted directory has been written yet.
        # This is distinct from a namespace with neither an in-memory nor a
        # persisted index, which must remain a typed ``missing_index`` state.
        has_current_in_memory_index = effective_namespace in self._indices
        blocked_statuses = {
            "signature_missing": "signature_missing",
            "embedding_signature_mismatch": "embedding_signature_mismatch",
            "rebuild_in_progress": "rebuild_in_progress",
            "embedder_unconfigured": "unavailable",
        }
        if namespace_status == "missing_index" and not has_current_in_memory_index:
            blocked_statuses["missing_index"] = "missing_index"
        retrieval_status = blocked_statuses.get(namespace_status)
        if retrieval_status is not None or not bool(
            namespace_state.get("compatible", False)
        ):
            if retrieval_status is None:
                retrieval_status = "unavailable"
            results = self._query_results_with_state(
                [],
                namespace=effective_namespace,
                status=retrieval_status,
                cause=(
                    "namespace_lock_contended"
                    if bool(namespace_state.get("lock_contended", False))
                    else namespace_status or "namespace_state_unavailable"
                ),
                detail=namespace_state.get("detail"),
            )
            try:
                self._last_query_info = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "effective_namespace": effective_namespace,
                    "query_length": len(query_text or ""),
                    "top_k": int(top_k),
                    "returned": 0,
                    "compatibility_status": namespace_state.get("status"),
                    "compatibility_detail": namespace_state.get("detail"),
                    "retrieval_state": dict(results.retrieval_state),
                }
            except Exception:
                pass
            return results

        index = self._maybe_load_index(effective_namespace)
        if not index:
            results = self._query_results_with_state(
                [],
                namespace=effective_namespace,
                status="unavailable",
                cause="index_load_failed",
                detail=(
                    "A compatible persisted index was reported but could not be "
                    "loaded for this retrieval attempt."
                ),
            )
            try:
                self._last_query_info = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "effective_namespace": effective_namespace,
                    "query_length": len(query_text or ""),
                    "top_k": int(top_k),
                    "returned": 0,
                    "retrieval_state": dict(results.retrieval_state),
                }
            except Exception:
                pass
            return results

        # Note: Metadata filtering support varies by vector store implementation.
        # To ensure correctness, we do coarse retrieval first then apply filtering
        # locally.
        retrieval_candidate_limit = None
        if isinstance(permissions_context, dict):
            raw_candidate_limit = permissions_context.get("retrieval_candidate_limit")
            if raw_candidate_limit is None:
                parsed_candidate_limit = 0
            else:
                try:
                    parsed_candidate_limit = int(raw_candidate_limit)
                except (TypeError, ValueError):
                    parsed_candidate_limit = 0
            if parsed_candidate_limit >= max(1, int(top_k)):
                retrieval_candidate_limit = parsed_candidate_limit

        similarity_top_k = (
            retrieval_candidate_limit
            if retrieval_candidate_limit is not None
            else max(top_k * 10, top_k)
        )
        start = time.perf_counter()
        embed_model, previous_embed_settings = (
            self._temporarily_bound_query_embed_model()
        )
        try:
            retriever = index.as_retriever(similarity_top_k=similarity_top_k)
            nodes = retriever.retrieve(query_text)
            query_elapsed_seconds = time.perf_counter() - start
            query_advisory_exceeded = (
                query_elapsed_seconds >= _QUERY_EMBED_ADVISORY_SECONDS
            )
            if query_advisory_exceeded:
                logger.warning(
                    "RAG query crossed its %.3fs embedding/retrieval advisory; "
                    "the usable result was retained after %.3fs.",
                    _QUERY_EMBED_ADVISORY_SECONDS,
                    query_elapsed_seconds,
                )
        except Exception as exc:
            state = build_rag_retrieval_state(
                "degraded",
                result_count=0,
                cause=f"query_failed:{type(exc).__name__}",
                detail="The configured retrieval backend failed during query execution.",
            )
            with self._retrieval_state_lock:
                self._namespace_retrieval_states[effective_namespace] = dict(state)
            try:
                self._last_query_info = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "effective_namespace": effective_namespace,
                    "query_length": len(query_text or ""),
                    "top_k": int(top_k),
                    "returned": 0,
                    "retrieval_state": state,
                }
            except Exception:
                pass
            raise
        finally:
            if embed_model is not None:
                for attr, value in previous_embed_settings.items():
                    setattr(embed_model, attr, value)

        knowledge_candidate_metadata = [
            metadata
            for node in nodes
            if isinstance(
                metadata := getattr(node.node, "metadata", None),
                dict,
            )
            and (
                metadata.get("type") in {"text_relation", "scoped_knowledge_assertion"}
                or metadata.get("source")
                in {
                    "vontology_text_relation",
                    "scoped_knowledge_assertion",
                }
            )
        ]
        authorised_knowledge_keys: set[tuple[str, str]] = set()
        if knowledge_candidate_metadata:
            from ..scoped_rag_authority_service import (
                current_authorised_rag_candidate_keys,
            )

            actor_user_id = (
                permissions_context.get("user_id")
                if isinstance(permissions_context, dict)
                else None
            )
            actor_org_id = (
                permissions_context.get("organisation_concept_id")
                if isinstance(permissions_context, dict)
                else None
            )
            try:
                authorised_knowledge_keys = current_authorised_rag_candidate_keys(
                    knowledge_candidate_metadata,
                    user_concept_id=actor_user_id,
                    organisation_concept_id=actor_org_id,
                )
            except Exception as exc:
                # Knowledge rows fail closed when their live authority surface
                # is unavailable; unrelated chat candidates remain usable.
                logger.warning(
                    "Knowledge RAG authority refresh failed closed: %s",
                    exc,
                )
                authorised_knowledge_keys = set()

        # Live authority is part of the retrieval boundary, not an ordinary
        # semantic filter. Remove inaccessible knowledge rows before computing
        # any externally visible count or candidate-window diagnostic so a
        # stale vector entry cannot disclose that a relation or assertion exists.
        authority_visible_nodes = []
        for node in nodes:
            metadata = getattr(node.node, "metadata", None)
            candidate_key: tuple[str, str] | None = None
            if isinstance(metadata, dict):
                metadata_type = str(metadata.get("type") or "").strip()
                metadata_source = str(metadata.get("source") or "").strip()
                if (
                    metadata_type == "scoped_knowledge_assertion"
                    or metadata_source == "scoped_knowledge_assertion"
                ):
                    candidate_key = (
                        "scoped_knowledge_assertion",
                        str(metadata.get("assertion_id") or "").strip(),
                    )
                elif (
                    metadata_type == "text_relation"
                    or metadata_source == "vontology_text_relation"
                ):
                    candidate_key = (
                        "text_relation",
                        str(metadata.get("relation_id") or "").strip(),
                    )
            if candidate_key is not None:
                if (
                    not candidate_key[1]
                    or candidate_key not in authorised_knowledge_keys
                ):
                    continue
            authority_visible_nodes.append(node)

        def _matches_permissions(metadata: Any) -> bool:
            if not isinstance(metadata, dict):
                return False
            actual_type = metadata.get("type")
            metadata_source = metadata.get("source")
            is_live_authorised_scoped_assertion = bool(
                actual_type == "scoped_knowledge_assertion"
                or metadata_source == "scoped_knowledge_assertion"
            )
            if not permissions_context:
                return True

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
                    requested_types = [
                        "text_relation",
                        "scoped_knowledge_assertion",
                    ]
                elif m == "all" or not m:
                    pass

            if requested_types:
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

            # Knowledge rows have already passed the canonical live authority
            # check above. Re-applying generic creator metadata equality would
            # incorrectly reject user-scoped knowledge in an organisation turn
            # and organisation-scoped knowledge written by another member.
            if not is_live_authorised_scoped_assertion:
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
        for node in authority_visible_nodes:
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
        requested_result_count = max(1, int(top_k))
        visible_result_count = len(results)
        visible_window_complete = visible_result_count >= requested_result_count
        query_results = self._query_results_with_state(
            results,
            namespace=effective_namespace,
            status=(
                "results_available"
                if visible_window_complete
                else "partial_results"
                if visible_result_count
                else "degraded"
            ),
            cause=(
                "query_completed"
                if visible_window_complete
                else "bounded_visible_result_window_underfilled"
                if visible_result_count
                else "bounded_visible_result_window_inconclusive"
            ),
            # Public diagnostics are deliberately authority-opaque. Expose only
            # the visible result count and requested result bound, never raw,
            # permission-filtered, or authority-filtered candidate volumes.
            candidate_count=visible_result_count,
            candidate_limit=requested_result_count,
            candidate_limit_reached=visible_window_complete,
        )
        try:
            self._last_query_info = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "effective_namespace": effective_namespace,
                "query_length": len(query_text or ""),
                "top_k": int(top_k),
                "similarity_top_k": int(similarity_top_k),
                "retrieval_candidate_limit": (
                    int(retrieval_candidate_limit)
                    if retrieval_candidate_limit is not None
                    else None
                ),
                "retrieved": visible_result_count,
                "returned": len(results),
                "elapsed_ms": elapsed_ms,
                "elapsed_time_enforcement": "advisory",
                "advisory_seconds": _QUERY_EMBED_ADVISORY_SECONDS,
                "advisory_exceeded": query_advisory_exceeded,
                "hard_timeout_seconds": None,
                "retrieval_state": dict(query_results.retrieval_state),
            }
        except Exception:
            # Never let diagnostics interfere with retrieval.
            pass

        return query_results

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
