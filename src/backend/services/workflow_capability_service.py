"""Authority-aligned workflow retrieval surface for conversation-turn routing.

This module keeps the existing workflow capability API boundary while replacing
the previous Python-authored BM25 / English stopword core with a dedicated
retrieval surface backed by the shared RAG infrastructure.

All registered workflows (built-in + Vontology) are materialised into a
dedicated workflow retrieval namespace using authoritative workflow description
text and discovery exemplars. Python remains support-only here: it prepares the
documents, manages rebuild/readiness state, and queries the retrieval backend.
It does not impose lexical routing semantics through token tables or stopword
lists.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..workflows.workflow_definition_identity_service import (
    assess_workflow_id_hygiene,
)

logger = logging.getLogger(__name__)

WORKFLOW_CAPABILITY_NAMESPACE = "workflow_capabilities"


def _get_positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)
    return parsed if parsed > 0.0 else float(default)

# -------------------------------------------------------------------------
# Retired Python capability overrides.
#
# Conversation-turn routing is now expected to obtain capability text from
# authoritative Vontology workflow descriptions. Keep the constant so
# diagnostics and tests can assert that no runtime override surface remains.
# -------------------------------------------------------------------------

BUILTIN_WORKFLOW_CAPABILITIES: Dict[str, str] = {}

_CAPABILITY_INDEX_REBUILD_MIN_INTERVAL_SECONDS = 30.0
_WORKFLOW_CAPABILITY_INDEX_STARTUP_TIMEOUT_SECONDS = _get_positive_float_env(
    "VON_WORKFLOW_CAPABILITY_INDEX_STARTUP_TIMEOUT_SECONDS",
    45.0,
)
_WORKFLOW_CAPABILITY_RETRIEVAL_CANDIDATE_MULTIPLIER = 2
_WORKFLOW_CAPABILITY_RETRIEVAL_WARM_QUERIES: tuple[str, ...] = (
    "workflow discovery capability",
    (
        "workflow capability warmup\n\n"
        "Turn-intent routing guidance:\n"
        "- Routing guidance: prioritise grounded relationship retrieval "
        "workflows over generic inventory listing.\n"
        "- Grounding requirement: use relation-bearing evidence and explicit "
        "required tools when answering entity-relative questions.\n"
        "- Success target: select the authoritative workflow that can identify "
        "an entity and retrieve predicate-filtered extents such as papers, "
        "affiliations, or roles.\n"
        "- Required tools: fetch_concept, find_relations_with_argument"
    ),
)

_INDEX_REBUILD_LOCK = Lock()
_INDEX_STATE_LOCK = Lock()
_INDEX_REBUILD_STATE: Dict[str, Any] = {
    "last_attempt_monotonic": 0.0,
    "last_success_monotonic": 0.0,
    "last_built_size": 0,
    "build_in_progress": False,
    "last_error": None,
    "last_mode": None,
    "startup_last_report": None,
    "last_invalidation_reason": None,
    "last_invalidated_at_utc": None,
    "query_surface_ready": False,
    "query_surface_last_error": None,
    "query_surface_last_warm_monotonic": 0.0,
}
_INDEX_REBUILD_COMPLETED = threading.Event()
_INDEX_REBUILD_COMPLETED.set()


def _build_workflow_capability_document_id(workflow_id: str) -> str:
    clean_workflow_id = str(workflow_id or "").strip()
    return f"workflow_capability:{clean_workflow_id}"


def _coerce_retrieval_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return score


def _normalise_retrieval_score(*, raw_score: float, max_score: float) -> float:
    if max_score <= 0.0:
        return 0.0
    normalised = raw_score / max_score
    if not math.isfinite(normalised):
        return 0.0
    return max(0.0, min(1.0, normalised))


def _build_workflow_capability_result_description(entry: "_CapabilityEntry") -> str:
    summary_text = str(entry.metadata.get("summary_text") or "").strip()
    if summary_text:
        return summary_text
    text = str(entry.text or "").strip()
    if not text:
        return _workflow_id_to_description(entry.workflow_id)
    first_block = text.split("\n\n", 1)[0].strip()
    if first_block:
        return first_block[:300]
    return text[:300]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_workflow_capability_retrieval_candidate_limit(max_results: int) -> int:
    requested = max(1, int(max_results or 1))
    return max(
        requested,
        requested * _WORKFLOW_CAPABILITY_RETRIEVAL_CANDIDATE_MULTIPLIER,
    )


def _get_workflow_capability_rag_service() -> Any:
    from .rag_service import get_rag_service

    service = get_rag_service("llamaindex")
    if type(service).__name__ != "LlamaIndexRAGService":
        raise RuntimeError(
            "workflow capability retrieval requires the llamaindex backend"
        )
    return service


def _reset_workflow_capability_backend_namespace(rag_service: Any) -> None:
    reset_namespace = getattr(rag_service, "reset_namespace", None)
    if callable(reset_namespace):
        reset_namespace(WORKFLOW_CAPABILITY_NAMESPACE)
        return

    indices = getattr(rag_service, "_indices", None)
    if isinstance(indices, dict):
        indices.pop(WORKFLOW_CAPABILITY_NAMESPACE, None)

    namespace_dir_builder = getattr(rag_service, "_namespace_persist_dir", None)
    persistence_dir = getattr(rag_service, "persistence_dir", None)
    if not callable(namespace_dir_builder):
        return
    if not isinstance(persistence_dir, str) or not persistence_dir.strip():
        return

    namespace_dir = Path(
        str(namespace_dir_builder(WORKFLOW_CAPABILITY_NAMESPACE))
    ).resolve()
    namespaces_root = (Path(persistence_dir).resolve() / "namespaces").resolve()

    try:
        common_root = os.path.commonpath(
            [str(namespace_dir), str(namespaces_root)]
        )
    except ValueError as exc:
        raise RuntimeError(
            f"workflow capability namespace reset path mismatch: {exc}"
        ) from exc

    if common_root != str(namespaces_root):
        raise RuntimeError(
            "workflow capability namespace reset refused outside persistence root"
        )

    if namespace_dir.is_dir():
        shutil.rmtree(namespace_dir)
    elif namespace_dir.exists():
        raise RuntimeError(
            f"workflow capability namespace path is not a directory: {namespace_dir}"
        )


@dataclass
class _CapabilityEntry:
    workflow_id: str
    doc_id: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowCapabilityMatch:
    """A workflow matched from the dedicated workflow retrieval surface."""

    workflow_id: str
    name: str
    description: str
    relevance_score: float
    source: str = "capability_index"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_discovery_dict(self) -> Dict[str, Any]:
        """Convert to the dict format expected by discovery/selector."""
        return {
            "concept_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "relevance_score": round(self.relevance_score, 4),
            "match_source": self.source,
        }


class WorkflowCapabilityIndex:
    """Dedicated workflow retrieval surface backed by the RAG service."""

    def __init__(self) -> None:
        self._entries: Dict[str, _CapabilityEntry] = {}
        self._lock = Lock()

    @staticmethod
    def _entry_to_document(entry: _CapabilityEntry) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "workflow_id": entry.workflow_id,
            "name": str(
                entry.metadata.get("name") or _workflow_id_to_name(entry.workflow_id)
            ).strip(),
            "type": "workflow_capability",
        }
        source = str(entry.metadata.get("source") or "").strip()
        if source:
            metadata["source"] = source[:120]
        description_source = str(entry.metadata.get("description_source") or "").strip()
        if description_source:
            metadata["description_source"] = description_source[:240]
        return {
            "id": entry.doc_id,
            "text": entry.text,
            "metadata": metadata,
        }

    def _replace_entries(
        self,
        pending_entries: Mapping[str, _CapabilityEntry],
    ) -> None:
        rag_service = _get_workflow_capability_rag_service()
        _reset_workflow_capability_backend_namespace(rag_service)
        payload = [
            self._entry_to_document(entry) for entry in pending_entries.values()
        ]
        if payload:
            success_count, failure_count = rag_service.upsert_documents(
                payload,
                namespace=WORKFLOW_CAPABILITY_NAMESPACE,
                allow_partial_failures=False,
            )
            if failure_count or success_count != len(payload):
                raise RuntimeError(
                    "workflow capability retrieval sync failed "
                    f"(success={success_count}, failed={failure_count}, "
                    f"expected={len(payload)})"
                )
        with self._lock:
            self._entries = dict(pending_entries)

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_workflow(
        self,
        workflow_id: str,
        capability_text: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add or replace a workflow capability document and sync retrieval."""
        clean_text = str(capability_text or "").strip()
        if not clean_text:
            raise ValueError("workflow capability text must not be empty")
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("name", _workflow_id_to_name(workflow_id))
        merged_metadata.setdefault("summary_text", clean_text.split("\n\n", 1)[0].strip())
        entry = _CapabilityEntry(
            workflow_id=workflow_id,
            doc_id=_build_workflow_capability_document_id(workflow_id),
            text=clean_text,
            metadata=merged_metadata,
        )
        with self._lock:
            pending_entries = dict(self._entries)
        pending_entries[workflow_id] = entry
        self._replace_entries(pending_entries)

    def index_from_registry(self, registry: Any) -> int:
        """Index all workflows from a ``WorkflowRegistry``.

        Only indexes workflows whose routing text is already authoritative:
        Vontology-sourced registrations with non-empty narrative text.
        Non-authoritative registrations and textless workflows are skipped so
        discovery fails closed instead of routing on guessed fallback prose.

        Returns the number of workflows indexed.
        """
        skipped_non_authoritative = 0
        skipped_missing_purpose = 0
        skipped_invalid_workflow_id = 0
        pending_entries: Dict[str, _CapabilityEntry] = {}
        candidate_rows: list[tuple[str, Any, Any]] = []
        authoritative_workflow_ids: list[str] = []

        def _index_candidate(
            *,
            workflow_id: str,
            purpose: Any,
            source: Any,
            routing_metadata: Mapping[str, Any] | None = None,
        ) -> None:
            nonlocal skipped_non_authoritative
            nonlocal skipped_missing_purpose
            nonlocal skipped_invalid_workflow_id

            workflow_id_hygiene = assess_workflow_id_hygiene(workflow_id)
            if not bool(workflow_id_hygiene.get("valid")):
                skipped_invalid_workflow_id += 1
                return

            text, reason = _resolve_authoritative_capability_text(
                workflow_id=workflow_id,
                source=source,
                purpose=purpose,
                routing_metadata=routing_metadata,
            )
            if text is None:
                if reason == "non_authoritative_source":
                    skipped_non_authoritative += 1
                elif reason == "missing_authoritative_purpose":
                    skipped_missing_purpose += 1
                return

            pending_entries[workflow_id] = _CapabilityEntry(
                workflow_id=workflow_id,
                doc_id=_build_workflow_capability_document_id(workflow_id),
                text=text,
                metadata={
                    "name": _workflow_id_to_name(workflow_id),
                    "source": str(source or "unknown"),
                    "description_source": reason,
                    "purpose": _normalise_capability_text(purpose),
                    "summary_text": text.split("\n\n", 1)[0].strip(),
                },
            )

        # Eager registrations.
        for wid in list(registry.eager_workflow_ids()):
            reg = registry._workflows.get(wid)  # type: ignore[attr-defined]
            workflow_id = str(wid or "").strip()
            purpose = reg.purpose if reg else None
            source = reg.source if reg else None
            if workflow_id:
                candidate_rows.append((workflow_id, purpose, source))
                if str(source or "").strip().lower() == "vontology":
                    authoritative_workflow_ids.append(workflow_id)

        # Lazy registrations (metadata only, no definition load).
        for wid in list(registry.lazy_workflow_ids()):
            lazy = registry._lazy.get(wid)  # type: ignore[attr-defined]
            workflow_id = str(wid or "").strip()
            purpose = lazy.purpose if lazy else None
            source = lazy.source if lazy else None
            if workflow_id:
                candidate_rows.append((workflow_id, purpose, source))
                if str(source or "").strip().lower() == "vontology":
                    authoritative_workflow_ids.append(workflow_id)

        authoritative_routing_metadata: Dict[str, Dict[str, Any]] = {}
        if authoritative_workflow_ids:
            try:
                from ..workflows.vontology_loader import (
                    batch_fetch_workflow_routing_metadata,
                )

                authoritative_routing_metadata = batch_fetch_workflow_routing_metadata(
                    authoritative_workflow_ids
                )
            except Exception:
                authoritative_routing_metadata = {}

        for workflow_id, purpose, source in candidate_rows:
            _index_candidate(
                workflow_id=workflow_id,
                purpose=purpose,
                source=source,
                routing_metadata=authoritative_routing_metadata.get(workflow_id),
            )

        self._replace_entries(pending_entries)

        count = len(pending_entries)
        logger.info(
            "[workflow_capability_index] Indexed and synced %d workflows "
            "(%d eager, %d lazy), skipped_non_authoritative=%d "
            "skipped_missing_authoritative_text=%d "
            "skipped_invalid_workflow_id=%d",
            count,
            len(list(registry.eager_workflow_ids())),
            len(list(registry.lazy_workflow_ids())),
            skipped_non_authoritative,
            skipped_missing_purpose,
            skipped_invalid_workflow_id,
        )
        return count

    @property
    def size(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        min_score: float = 0.0,
        exclude_ids: Optional[set[str]] = None,
    ) -> List[WorkflowCapabilityMatch]:
        """Search for workflows matching *query*.

        Returns up to *max_results* matches sorted by retrieval score
        descending.
        """
        clean_query = str(query or "").strip()
        if not clean_query:
            return []

        with self._lock:
            entries = list(self._entries.values())

        if not entries:
            return []

        entry_lookup = {entry.workflow_id: entry for entry in entries}
        rag_service = _get_workflow_capability_rag_service()
        retrieval_candidate_limit = _compute_workflow_capability_retrieval_candidate_limit(
            max_results
        )
        rag_results = rag_service.query(
            query_text=clean_query,
            top_k=max(1, max_results),
            namespace=WORKFLOW_CAPABILITY_NAMESPACE,
            hybrid=True,
            permissions_context={
                "type": "workflow_capability",
                "retrieval_candidate_limit": retrieval_candidate_limit,
            },
        )

        scored_rows: List[Tuple[float, _CapabilityEntry]] = []
        seen_ids: set[str] = set()
        for result in rag_results:
            metadata = result.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            workflow_id = str(metadata.get("workflow_id") or "").strip()
            if not workflow_id or workflow_id in seen_ids:
                continue
            entry = entry_lookup.get(workflow_id)
            if entry is None:
                continue
            if exclude_ids and entry.workflow_id in exclude_ids:
                continue
            seen_ids.add(workflow_id)
            scored_rows.append((_coerce_retrieval_score(result.get("score")), entry))

        if not scored_rows:
            return []

        scored_rows.sort(key=lambda item: (-item[0], item[1].workflow_id))
        max_score = max(
            (score for score, _entry in scored_rows if score > 0.0),
            default=1.0,
        )

        results: List[WorkflowCapabilityMatch] = []
        for score, entry in scored_rows:
            normalised = _normalise_retrieval_score(
                raw_score=score,
                max_score=max_score,
            )
            if normalised < min_score:
                continue
            name = entry.metadata.get("name") or _workflow_id_to_name(entry.workflow_id)
            result_metadata = dict(entry.metadata)
            result_metadata["raw_retrieval_score"] = score
            results.append(
                WorkflowCapabilityMatch(
                    workflow_id=entry.workflow_id,
                    name=name,
                    description=_build_workflow_capability_result_description(entry),
                    relevance_score=round(normalised, 4),
                    source="capability_index",
                    metadata=result_metadata,
                )
            )
            if len(results) >= max_results:
                break
        return results


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _workflow_id_to_name(workflow_id: str) -> str:
    """Derive a human-readable name from a workflow ID."""
    clean = workflow_id
    if clean.startswith("#V#"):
        clean = clean[3:]
    return clean.replace("_", " ").strip().title()


def _workflow_id_to_description(workflow_id: str) -> str:
    """Derive a minimal fallback description from a workflow ID."""
    name = _workflow_id_to_name(workflow_id)
    return f"{name} workflow."


def _normalise_capability_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _resolve_authoritative_capability_text(
    *,
    workflow_id: str,
    source: Any,
    purpose: Any,
    routing_metadata: Mapping[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Return authoritative routing text or a deterministic skip reason."""

    source_token = str(source or "").strip().lower()
    if source_token != "vontology":
        return None, "non_authoritative_source"

    relation_text = ""
    relation_source = ""
    discovery_exemplars: Mapping[str, Any] | None = None
    discovery_exemplars_source = ""
    if isinstance(routing_metadata, Mapping):
        relation_text = _normalise_capability_text(routing_metadata.get("description_text"))
        relation_source = str(routing_metadata.get("description_source") or "").strip()
        raw_exemplars = routing_metadata.get("discovery_exemplars")
        if isinstance(raw_exemplars, Mapping):
            discovery_exemplars = raw_exemplars
        discovery_exemplars_source = str(
            routing_metadata.get("discovery_exemplars_source") or ""
        ).strip()
    else:
        try:
            from ..workflows.vontology_loader import (
                resolve_workflow_description,
                resolve_workflow_discovery_exemplars,
            )

            relation_text, relation_source = resolve_workflow_description(
                workflow_id,
                workflow_source="vontology",
                registration_purpose=purpose,
            )
            discovery_exemplars, discovery_exemplars_source = (
                resolve_workflow_discovery_exemplars(workflow_id)
            )
        except Exception:
            relation_text = ""
            relation_source = ""
            discovery_exemplars = None
            discovery_exemplars_source = ""

    if relation_text and relation_source.startswith("text_relation:"):
        capability_parts = [relation_text]
        if isinstance(discovery_exemplars, Mapping):
            keywords = discovery_exemplars.get("keywords") or []
            if isinstance(keywords, Sequence) and not isinstance(keywords, str):
                keyword_text = ", ".join(
                    str(item).strip()
                    for item in keywords
                    if isinstance(item, str) and str(item).strip()
                )
                if keyword_text:
                    capability_parts.append(f"Keywords: {keyword_text}")
            examples = discovery_exemplars.get("examples") or []
            if isinstance(examples, Sequence) and not isinstance(examples, str):
                example_lines = [
                    str(item).strip()
                    for item in examples
                    if isinstance(item, str) and str(item).strip()
                ]
                if example_lines:
                    capability_parts.append(
                        "Example requests: " + " | ".join(example_lines)
                    )
        source_parts = [relation_source]
        if (
            isinstance(discovery_exemplars_source, str)
            and discovery_exemplars_source.startswith("text_relation:")
        ):
            source_parts.append(discovery_exemplars_source)
        return "\n\n".join(capability_parts), "+".join(source_parts)

    purpose_text = _normalise_capability_text(purpose)
    if not purpose_text:
        return None, "missing_authoritative_purpose"

    return purpose_text, "authoritative_registration_purpose"


def build_workflow_capability_text(
    workflow_id: str,
    *,
    purpose: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build rich searchable text for a workflow capability document.

    Combines purpose, description, and metadata
    into a single searchable text block.
    """
    parts: list[str] = []
    if purpose:
        parts.append(purpose)
    if description and description not in " ".join(parts):
        parts.append(description)

    if isinstance(metadata, Mapping):
        domain = metadata.get("domain")
        if isinstance(domain, str) and domain.strip():
            parts.append(f"Domain: {domain.strip()}")
        tags = metadata.get("tags")
        if isinstance(tags, (list, tuple)):
            tag_text = ", ".join(
                str(t).strip() for t in tags if isinstance(t, str) and t.strip()
            )
            if tag_text:
                parts.append(f"Tags: {tag_text}")

    if not parts:
        parts.append(_workflow_id_to_description(workflow_id))

    return " ".join(parts)


# -------------------------------------------------------------------------
# Global singleton
# -------------------------------------------------------------------------

_global_index: Optional[WorkflowCapabilityIndex] = None
_global_index_lock = Lock()


def get_workflow_capability_index() -> WorkflowCapabilityIndex:
    """Return the shared workflow capability index singleton.

    Creates an empty index on first call.  Use ``index_from_registry()``
    to populate it after the workflow registry is built.
    """
    global _global_index
    if _global_index is not None:
        return _global_index
    with _global_index_lock:
        if _global_index is not None:
            return _global_index
        _global_index = WorkflowCapabilityIndex()
        return _global_index


def _workflow_capability_rebuild_recently_attempted(
    now_monotonic: float,
    *,
    force_refresh: bool,
) -> bool:
    if force_refresh:
        return False
    with _INDEX_STATE_LOCK:
        last_attempt = float(_INDEX_REBUILD_STATE.get("last_attempt_monotonic", 0.0))
    return (
        last_attempt > 0.0
        and (now_monotonic - last_attempt) < _CAPABILITY_INDEX_REBUILD_MIN_INTERVAL_SECONDS
    )


def _set_workflow_capability_rebuild_state(
    *,
    build_in_progress: bool,
    mode: str | None = None,
    count: int | None = None,
    error: str | None = None,
    attempt_monotonic: float | None = None,
    success_monotonic: float | None = None,
    clear_invalidation: bool = False,
) -> None:
    with _INDEX_STATE_LOCK:
        _INDEX_REBUILD_STATE["build_in_progress"] = bool(build_in_progress)
        if mode is not None:
            _INDEX_REBUILD_STATE["last_mode"] = mode
        if count is not None:
            _INDEX_REBUILD_STATE["last_built_size"] = int(count)
        _INDEX_REBUILD_STATE["last_error"] = error
        if attempt_monotonic is not None:
            _INDEX_REBUILD_STATE["last_attempt_monotonic"] = float(attempt_monotonic)
        if success_monotonic is not None:
            _INDEX_REBUILD_STATE["last_success_monotonic"] = float(success_monotonic)
        if clear_invalidation:
            _INDEX_REBUILD_STATE["last_invalidation_reason"] = None
            _INDEX_REBUILD_STATE["last_invalidated_at_utc"] = None


def _set_workflow_capability_query_surface_state(
    *,
    ready: bool,
    error: str | None = None,
    warmed_monotonic: float | None = None,
) -> None:
    with _INDEX_STATE_LOCK:
        _INDEX_REBUILD_STATE["query_surface_ready"] = bool(ready)
        _INDEX_REBUILD_STATE["query_surface_last_error"] = error
        if warmed_monotonic is not None:
            _INDEX_REBUILD_STATE["query_surface_last_warm_monotonic"] = float(
                warmed_monotonic
            )


def _warm_workflow_capability_query_surface(
    index: "WorkflowCapabilityIndex",
    *,
    timeout_seconds: float | None = None,
    mode: str = "warm",
) -> None:
    warm_started_at = time.perf_counter()
    warm_failures: list[str] = []
    for warm_query in _WORKFLOW_CAPABILITY_RETRIEVAL_WARM_QUERIES:
        if (
            timeout_seconds is not None
            and timeout_seconds > 0.0
            and (time.perf_counter() - warm_started_at) >= timeout_seconds
        ):
            raise TimeoutError(
                "workflow capability query surface warm-up timed out after "
                f"{timeout_seconds:.3f}s"
            )
        try:
            index.search(warm_query, max_results=1)
        except Exception as warm_exc:
            warm_failures.append(str(warm_exc))
    if warm_failures:
        joined = "; ".join(warm_failures)
        _set_workflow_capability_query_surface_state(ready=False, error=joined)
        logger.warning("workflow_capability_index_warm_query_failed: %s", joined)
        raise RuntimeError(joined)

    _set_workflow_capability_query_surface_state(
        ready=True,
        error=None,
        warmed_monotonic=time.monotonic(),
    )
    logger.info(
        "[workflow_capability_index] %s query surface warmed in %.1fms.",
        mode,
        (time.perf_counter() - warm_started_at) * 1000.0,
    )


def get_workflow_capability_index_runtime_state() -> Dict[str, Any]:
    """Return lightweight runtime state for capability-index diagnostics."""

    index = get_workflow_capability_index()
    namespace_state = None
    try:
        rag_service = _get_workflow_capability_rag_service()
        get_namespace_runtime_state = getattr(
            rag_service,
            "get_namespace_runtime_state",
            None,
        )
        if callable(get_namespace_runtime_state):
            state = get_namespace_runtime_state(WORKFLOW_CAPABILITY_NAMESPACE)
            if isinstance(state, dict):
                namespace_state = state
    except Exception:
        namespace_state = None

    namespace_compatible = (
        bool(namespace_state.get("compatible", False))
        if isinstance(namespace_state, dict)
        else True
    )
    with _INDEX_STATE_LOCK:
        query_surface_ready = bool(
            _INDEX_REBUILD_STATE.get("query_surface_ready", False)
        )
        return {
            "surface": "workflow_retrieval",
            "backend": "llamaindex",
            "namespace": WORKFLOW_CAPABILITY_NAMESPACE,
            "size": int(index.size),
            "ready": bool(index.size > 0 and namespace_compatible and query_surface_ready),
            "build_in_progress": bool(_INDEX_REBUILD_STATE.get("build_in_progress", False)),
            "last_error": _INDEX_REBUILD_STATE.get("last_error"),
            "last_mode": _INDEX_REBUILD_STATE.get("last_mode"),
            "last_attempt_monotonic": float(
                _INDEX_REBUILD_STATE.get("last_attempt_monotonic", 0.0)
            ),
            "last_success_monotonic": float(
                _INDEX_REBUILD_STATE.get("last_success_monotonic", 0.0)
            ),
            "last_built_size": int(_INDEX_REBUILD_STATE.get("last_built_size", 0)),
            "last_invalidation_reason": _INDEX_REBUILD_STATE.get(
                "last_invalidation_reason"
            ),
            "last_invalidated_at_utc": _INDEX_REBUILD_STATE.get(
                "last_invalidated_at_utc"
            ),
            "query_surface_ready": query_surface_ready,
            "query_surface_last_error": _INDEX_REBUILD_STATE.get(
                "query_surface_last_error"
            ),
            "query_surface_last_warm_monotonic": float(
                _INDEX_REBUILD_STATE.get("query_surface_last_warm_monotonic", 0.0)
            ),
            "namespace_state": namespace_state,
        }


def get_workflow_capability_index_readiness_report() -> Dict[str, Any]:
    """Return a user-facing readiness report for the capability index."""

    runtime_state = get_workflow_capability_index_runtime_state()
    ready = bool(runtime_state.get("ready", False))
    build_in_progress = bool(runtime_state.get("build_in_progress", False))
    last_error = str(runtime_state.get("last_error") or "").strip()
    last_invalidation_reason = str(
        runtime_state.get("last_invalidation_reason") or ""
    ).strip()
    size = int(runtime_state.get("size") or 0)
    namespace_state = (
        runtime_state.get("namespace_state")
        if isinstance(runtime_state.get("namespace_state"), dict)
        else None
    )
    namespace_compatible = (
        bool(namespace_state.get("compatible", False))
        if isinstance(namespace_state, dict)
        else True
    )
    namespace_detail = (
        str(namespace_state.get("detail") or "").strip()
        if isinstance(namespace_state, dict)
        else ""
    )
    namespace_status = (
        str(namespace_state.get("status") or "").strip()
        if isinstance(namespace_state, dict)
        else ""
    )
    query_surface_ready = bool(runtime_state.get("query_surface_ready", False))
    query_surface_last_error = str(
        runtime_state.get("query_surface_last_error") or ""
    ).strip()

    if ready:
        status = "ready"
        warning_level = "ok"
        summary = "Workflow capability index ready."
        detail = (
            f"The authoritative workflow capability index is available with "
            f"{size} indexed workflow entries."
        )
    elif build_in_progress:
        status = "building"
        warning_level = "warning"
        summary = "Workflow capability index still building."
        detail = (
            "Workflow discovery is waiting on the authoritative capability "
            "index to finish building."
        )
    elif size > 0 and namespace_compatible and not query_surface_ready:
        status = "warming"
        warning_level = "warning"
        summary = "Workflow capability index query surface warming."
        detail = (
            f"Last warm-up failed: {query_surface_last_error}"
            if query_surface_last_error
            else (
                "The authoritative workflow capability index is built, but this "
                "process has not finished warming the query surface yet."
            )
        )
    elif not namespace_compatible and namespace_status:
        status = "error"
        warning_level = "error"
        summary = "Workflow capability index requires rebuild."
        detail = namespace_detail or (
            "The persisted workflow capability index is incompatible with the "
            "current embedding configuration."
        )
    elif last_error:
        status = "error"
        warning_level = "error"
        summary = "Workflow capability index not ready."
        detail = f"Last build failed: {last_error}"
    elif last_invalidation_reason:
        status = "rebuild_required"
        warning_level = "warning"
        summary = "Workflow capability index rebuild required."
        detail = last_invalidation_reason
    else:
        status = "not_ready"
        warning_level = "warning"
        summary = "Workflow capability index not ready."
        detail = (
            "No authoritative workflow capability snapshot has been built yet."
        )

    with _INDEX_STATE_LOCK:
        startup_report = _INDEX_REBUILD_STATE.get("startup_last_report")

    report: Dict[str, Any] = {
        **runtime_state,
        "status": status,
        "warning_level": warning_level,
        "summary": summary,
        "detail": detail,
        "checked_at_utc": _utc_now_iso(),
    }
    if isinstance(startup_report, dict):
        report["startup_check"] = dict(startup_report)
    else:
        report["startup_check"] = None
    return report


def run_workflow_capability_index_startup_check(
    *,
    timeout_seconds: float = _WORKFLOW_CAPABILITY_INDEX_STARTUP_TIMEOUT_SECONDS,
    workflow_registry: Any | None = None,
) -> Dict[str, Any]:
    """Perform a bounded startup readiness check for the capability index."""

    try:
        effective_timeout = max(0.0, float(timeout_seconds))
    except (TypeError, ValueError):
        effective_timeout = _WORKFLOW_CAPABILITY_INDEX_STARTUP_TIMEOUT_SECONDS

    started_at = time.perf_counter()
    checked_at_utc = _utc_now_iso()

    ensure_workflow_capability_index_populated(
        block=True,
        max_wait_seconds=effective_timeout,
        workflow_registry=workflow_registry,
    )

    runtime_state = get_workflow_capability_index_runtime_state()
    namespace_state_raw = runtime_state.get("namespace_state")
    namespace_state: Mapping[str, Any] = (
        namespace_state_raw if isinstance(namespace_state_raw, dict) else {}
    )
    if (
        int(runtime_state.get("size") or 0) > 0
        and bool(namespace_state.get("compatible", True))
        and not bool(runtime_state.get("query_surface_ready", False))
    ):
        remaining_timeout = max(
            0.0,
            effective_timeout - (time.perf_counter() - started_at),
        )
        _warm_workflow_capability_query_surface(
            get_workflow_capability_index(),
            timeout_seconds=remaining_timeout if remaining_timeout > 0.0 else None,
            mode="startup",
        )

    readiness_report = get_workflow_capability_index_readiness_report()
    ready = bool(readiness_report.get("ready", False))
    build_in_progress = bool(readiness_report.get("build_in_progress", False))
    last_error = str(readiness_report.get("last_error") or "").strip()

    if ready:
        status = "ready"
        summary = "Workflow capability index ready after startup check."
    elif build_in_progress:
        status = "timeout"
        summary = "Workflow capability index still not ready after startup check."
    elif last_error:
        status = "error"
        summary = "Workflow capability index failed startup readiness check."
    else:
        status = "not_ready"
        summary = "Workflow capability index not ready after startup check."

    startup_report: Dict[str, Any] = {
        "success": ready,
        "ready": ready,
        "status": status,
        "summary": summary,
        "detail": readiness_report.get("detail"),
        "timeout_seconds": effective_timeout,
        "duration_ms": round((time.perf_counter() - started_at) * 1000.0, 3),
        "checked_at_utc": checked_at_utc,
    }

    with _INDEX_STATE_LOCK:
        _INDEX_REBUILD_STATE["startup_last_report"] = dict(startup_report)

    return startup_report


def _perform_workflow_capability_index_build(
    *,
    force_refresh: bool = False,
    mode: str,
    workflow_registry: Any | None = None,
) -> WorkflowCapabilityIndex:
    attempt_monotonic = time.monotonic()
    _INDEX_REBUILD_COMPLETED.clear()
    _set_workflow_capability_rebuild_state(
        build_in_progress=True,
        mode=mode,
        error=None,
        attempt_monotonic=attempt_monotonic,
    )
    _set_workflow_capability_query_surface_state(ready=False, error=None)
    try:
        from ..workflows.durable.registry_factory import (
            get_shared_workflow_registry_read_only,
        )
        registry = (
            workflow_registry
            if workflow_registry is not None
            else get_shared_workflow_registry_read_only(defer_parity_work=True)
        )

        index = get_workflow_capability_index()
        count = index.index_from_registry(registry)
        if count > 0:
            _warm_workflow_capability_query_surface(index, mode=mode)
        success_monotonic = time.monotonic()
        _set_workflow_capability_rebuild_state(
            build_in_progress=False,
            mode=mode,
            count=count,
            error=None,
            success_monotonic=success_monotonic,
            clear_invalidation=True,
        )
        logger.info(
            "[workflow_capability_index] %s build completed with %d entries.",
            mode,
            count,
        )
        return index
    except Exception as exc:
        _set_workflow_capability_rebuild_state(
            build_in_progress=False,
            mode=mode,
            error=str(exc),
        )
        raise
    finally:
        _INDEX_REBUILD_COMPLETED.set()


def _start_background_workflow_capability_index_build(
    *,
    force_refresh: bool = False,
    workflow_registry: Any | None = None,
) -> bool:
    """Trigger a background build if one is not already running."""

    now = time.monotonic()
    with _INDEX_STATE_LOCK:
        if bool(_INDEX_REBUILD_STATE.get("build_in_progress", False)):
            return False
        last_attempt = float(_INDEX_REBUILD_STATE.get("last_attempt_monotonic", 0.0))
        if (
            not force_refresh
            and last_attempt > 0.0
            and (now - last_attempt) < _CAPABILITY_INDEX_REBUILD_MIN_INTERVAL_SECONDS
        ):
            return False
        _INDEX_REBUILD_STATE["build_in_progress"] = True
        _INDEX_REBUILD_STATE["last_mode"] = "background"
        _INDEX_REBUILD_STATE["last_error"] = None
        _INDEX_REBUILD_STATE["last_attempt_monotonic"] = now
        _INDEX_REBUILD_COMPLETED.clear()

    def _worker() -> None:
        try:
            with _INDEX_REBUILD_LOCK:
                _perform_workflow_capability_index_build(
                    force_refresh=force_refresh,
                    mode="background",
                    workflow_registry=workflow_registry,
                )
        except Exception as exc:
            logger.warning("workflow_capability_index_background_build_failed: %s", exc)

    try:
        thread = threading.Thread(
            target=_worker,
            name="workflow_capability_index_build",
            daemon=True,
        )
        thread.start()
        return True
    except Exception as exc:
        _set_workflow_capability_rebuild_state(
            build_in_progress=False,
            mode="background",
            error=f"thread_start_failed:{exc}",
        )
        _INDEX_REBUILD_COMPLETED.set()
        logger.warning("workflow_capability_index_background_thread_start_failed: %s", exc)
        return False


def ensure_workflow_capability_index_populated(
    *,
    force_refresh: bool = False,
    block: bool = True,
    max_wait_seconds: float | None = None,
    workflow_registry: Any | None = None,
) -> WorkflowCapabilityIndex:
    """Build the shared capability index on demand from authoritative workflows.

    Workflow discovery can run before deferred registry background work has
    populated the search substrate. Building on demand keeps routed turns from
    silently degrading to builtin-only candidates.
    """

    index = get_workflow_capability_index()
    runtime_state = get_workflow_capability_index_runtime_state()
    if index.size > 0 and bool(runtime_state.get("ready", False)) and not force_refresh:
        return index

    if not block:
        _start_background_workflow_capability_index_build(
            force_refresh=force_refresh,
            workflow_registry=workflow_registry,
        )
        wait_seconds = max(0.0, float(max_wait_seconds or 0.0))
        if wait_seconds > 0.0:
            _INDEX_REBUILD_COMPLETED.wait(wait_seconds)
        return get_workflow_capability_index()

    if bool(get_workflow_capability_index_runtime_state().get("build_in_progress", False)):
        wait_seconds = None if max_wait_seconds is None else max(0.0, float(max_wait_seconds))
        _INDEX_REBUILD_COMPLETED.wait(wait_seconds)
        index = get_workflow_capability_index()
        runtime_state = get_workflow_capability_index_runtime_state()
        if index.size > 0 and bool(runtime_state.get("ready", False)) and not force_refresh:
            return index

    now = time.monotonic()
    if _workflow_capability_rebuild_recently_attempted(
        now,
        force_refresh=force_refresh,
    ):
        return get_workflow_capability_index()

    with _INDEX_REBUILD_LOCK:
        index = get_workflow_capability_index()
        runtime_state = get_workflow_capability_index_runtime_state()
        if index.size > 0 and bool(runtime_state.get("ready", False)) and not force_refresh:
            return index
        try:
            return _perform_workflow_capability_index_build(
                force_refresh=force_refresh,
                mode="blocking",
                workflow_registry=workflow_registry,
            )
        except Exception as exc:
            logger.warning("workflow_capability_index_on_demand_build_failed: %s", exc)
            return get_workflow_capability_index()


def search_workflow_capabilities(
    query: str,
    *,
    max_results: int = 10,
    min_score: float = 0.0,
    non_blocking: bool = False,
    max_wait_seconds: float | None = None,
    workflow_registry: Any | None = None,
) -> List[WorkflowCapabilityMatch]:
    """Search workflow capabilities, rebuilding the retrieval surface on misses."""

    index = ensure_workflow_capability_index_populated(
        block=not non_blocking,
        max_wait_seconds=max_wait_seconds,
        workflow_registry=workflow_registry,
    )
    try:
        results = index.search(query, max_results=max_results, min_score=min_score)
        _set_workflow_capability_query_surface_state(
            ready=True,
            error=None,
            warmed_monotonic=time.monotonic(),
        )
    except Exception as exc:
        _set_workflow_capability_rebuild_state(
            build_in_progress=False,
            mode="query",
            error=f"query_failed:{exc}",
        )
        raise
    if results:
        return results
    if non_blocking:
        return []

    refreshed = ensure_workflow_capability_index_populated(
        force_refresh=True,
        workflow_registry=workflow_registry,
    )
    if refreshed is index and refreshed.size == 0:
        return []
    return refreshed.search(query, max_results=max_results, min_score=min_score)


def prewarm_workflow_capability_index(
    *,
    force_refresh: bool = False,
    workflow_registry: Any | None = None,
) -> bool:
    """Start capability-index warm-up without blocking the caller."""

    return _start_background_workflow_capability_index_build(
        force_refresh=force_refresh,
        workflow_registry=workflow_registry,
    )


def reset_workflow_capability_index() -> None:
    """Reset the global index.  Intended for tests."""
    global _global_index
    with _global_index_lock:
        _global_index = None
    with _INDEX_STATE_LOCK:
        _INDEX_REBUILD_STATE["last_attempt_monotonic"] = 0.0
        _INDEX_REBUILD_STATE["last_success_monotonic"] = 0.0
        _INDEX_REBUILD_STATE["last_built_size"] = 0
        _INDEX_REBUILD_STATE["build_in_progress"] = False
        _INDEX_REBUILD_STATE["last_error"] = None
        _INDEX_REBUILD_STATE["last_mode"] = None
        _INDEX_REBUILD_STATE["startup_last_report"] = None
        _INDEX_REBUILD_STATE["last_invalidation_reason"] = None
        _INDEX_REBUILD_STATE["last_invalidated_at_utc"] = None
        _INDEX_REBUILD_STATE["query_surface_ready"] = False
        _INDEX_REBUILD_STATE["query_surface_last_error"] = None
        _INDEX_REBUILD_STATE["query_surface_last_warm_monotonic"] = 0.0
    _INDEX_REBUILD_COMPLETED.set()


def invalidate_workflow_capability_index(
    *,
    reason: str | None = None,
    reset_backend_namespace: bool = False,
) -> dict[str, Any]:
    """Drop the cached capability index so the next lookup rebuilds it."""

    previous_state = get_workflow_capability_index_runtime_state()
    backend_namespace_reset = False
    backend_reset_error = None
    if reset_backend_namespace:
        try:
            rag_service = _get_workflow_capability_rag_service()
            _reset_workflow_capability_backend_namespace(rag_service)
            backend_namespace_reset = True
        except Exception as exc:
            backend_reset_error = str(exc)
    reset_workflow_capability_index()
    with _INDEX_STATE_LOCK:
        _INDEX_REBUILD_STATE["last_invalidation_reason"] = (
            str(reason).strip() if reason else None
        )
        _INDEX_REBUILD_STATE["last_invalidated_at_utc"] = _utc_now_iso()
        _INDEX_REBUILD_STATE["query_surface_ready"] = False
        _INDEX_REBUILD_STATE["query_surface_last_error"] = None
        _INDEX_REBUILD_STATE["query_surface_last_warm_monotonic"] = 0.0
    return {
        "success": True,
        "cache": "workflow_capability_index",
        "had_cached_entries": bool(previous_state.get("size", 0)),
        "backend_namespace_reset": backend_namespace_reset,
        "backend_reset_error": backend_reset_error,
        "reason": str(reason).strip() if reason else None,
    }
