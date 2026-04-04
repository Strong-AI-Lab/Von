"""Dedicated workflow capability index for RAG-first workflow routing.

JVNAUTOSCI-1424 Phase 2: Provides a purpose-built search index for workflow
capabilities, separate from the general concept embedding namespace.  All
registered workflows (built-in + Vontology) are indexed here with rich
capability descriptions so that routing can find the best workflow for any
user request via semantic-like retrieval.

Architecture:
    - ``WorkflowCapabilityIndex``: In-memory BM25-scored index of workflow
      capability documents.  Fast to build, zero external dependencies,
      supports hybrid keyword + relevance matching.
    - ``index_from_registry()``: Indexes all workflows from a registry
      (both eager and lazy) using authoritative Vontology narrative text and
      skipping non-authoritative or textless workflows.

The dedicated namespace isolates workflow routing from general concept search,
enabling independent tuning and scaling as the workflow catalogue grows.
Phase 3 can upgrade to dense vector embeddings while preserving the same API.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

WORKFLOW_CAPABILITY_NAMESPACE = "workflow_capabilities"

# -------------------------------------------------------------------------
# Retired Python capability overrides.
#
# Conversation-turn routing is now expected to obtain capability text from
# authoritative Vontology workflow descriptions. Keep the constant so
# diagnostics and tests can assert that no runtime override surface remains.
# -------------------------------------------------------------------------

BUILTIN_WORKFLOW_CAPABILITIES: Dict[str, str] = {}


# -------------------------------------------------------------------------
# BM25 index implementation
# -------------------------------------------------------------------------

_TOKENISE_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "is", "was", "are", "were", "be", "been", "being",
    "it", "its", "this", "that", "from", "as", "not", "no", "do", "does",
})
_CAPABILITY_INDEX_REBUILD_MIN_INTERVAL_SECONDS = 30.0

_INDEX_REBUILD_LOCK = Lock()
_INDEX_STATE_LOCK = Lock()
_INDEX_REBUILD_STATE: Dict[str, Any] = {
    "last_attempt_monotonic": 0.0,
    "last_success_monotonic": 0.0,
    "last_built_size": 0,
    "build_in_progress": False,
    "last_error": None,
    "last_mode": None,
}
_INDEX_REBUILD_COMPLETED = threading.Event()
_INDEX_REBUILD_COMPLETED.set()


def _normalise_token_variants(token: str) -> List[str]:
    """Return stable lexical variants for simple singular/plural matching."""
    cleaned = str(token or "").strip().lower()
    if not cleaned:
        return []

    variants = [cleaned]
    singular = cleaned
    if cleaned.endswith("ies") and len(cleaned) > 4:
        singular = cleaned[:-3] + "y"
    elif (
        (cleaned.endswith("es") and len(cleaned) > 4 and cleaned[-3:-2] in {"s", "x", "z"})
        or cleaned.endswith(("ches", "shes"))
    ):
        singular = cleaned[:-2]
    elif (
        cleaned.endswith("s")
        and len(cleaned) > 4
        and not cleaned.endswith(("ss", "us", "is"))
    ):
        singular = cleaned[:-1]

    singular = singular.strip()
    if singular and singular not in variants:
        variants.append(singular)

    return variants


def _tokenise(text: str) -> List[str]:
    """Tokenise text into lowercase terms, filtering stop words."""
    tokens: list[str] = []
    for raw_token in _TOKENISE_RE.findall(text.lower()):
        for token in _normalise_token_variants(raw_token):
            if token in _STOP_WORDS or len(token) <= 1:
                continue
            tokens.append(token)
    return tokens


@dataclass
class _CapabilityEntry:
    workflow_id: str
    text: str
    tokens: List[str]
    token_freqs: Dict[str, int]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowCapabilityMatch:
    """A workflow matched from the capability index."""

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
    """In-memory BM25-scored index of workflow capability documents.

    Thread-safe.  Supports ``index_workflow()`` to add entries and
    ``search()`` to retrieve ranked matches for a query string.
    """

    # BM25 tuning parameters.
    _K1 = 1.5
    _B = 0.75

    def __init__(self) -> None:
        self._entries: Dict[str, _CapabilityEntry] = {}
        self._idf: Dict[str, float] = {}
        self._avg_dl: float = 0.0
        self._lock = Lock()

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
        """Add or replace a workflow capability document."""
        tokens = _tokenise(capability_text)
        entry = _CapabilityEntry(
            workflow_id=workflow_id,
            text=capability_text,
            tokens=tokens,
            token_freqs=dict(Counter(tokens)),
            metadata=metadata or {},
        )
        with self._lock:
            self._entries[workflow_id] = entry
            self._rebuild_idf_unlocked()

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
        pending_entries: Dict[str, _CapabilityEntry] = {}

        def _index_candidate(
            *,
            workflow_id: str,
            purpose: Any,
            source: Any,
        ) -> None:
            nonlocal skipped_non_authoritative
            nonlocal skipped_missing_purpose

            text, reason = _resolve_authoritative_capability_text(
                workflow_id=workflow_id,
                source=source,
                purpose=purpose,
            )
            if text is None:
                if reason == "non_authoritative_source":
                    skipped_non_authoritative += 1
                elif reason == "missing_authoritative_purpose":
                    skipped_missing_purpose += 1
                return

            tokens = _tokenise(text)
            pending_entries[workflow_id] = _CapabilityEntry(
                workflow_id=workflow_id,
                text=text,
                tokens=tokens,
                token_freqs=dict(Counter(tokens)),
                metadata={
                    "name": _workflow_id_to_name(workflow_id),
                    "source": str(source or "unknown"),
                    "description_source": reason,
                    "purpose": _normalise_capability_text(purpose),
                },
            )

        # Eager registrations.
        for wid in list(registry.eager_workflow_ids()):
            reg = registry._workflows.get(wid)  # type: ignore[attr-defined]
            _index_candidate(
                workflow_id=wid,
                purpose=(reg.purpose if reg else None),
                source=(reg.source if reg else None),
            )

        # Lazy registrations (metadata only, no definition load).
        for wid in list(registry.lazy_workflow_ids()):
            lazy = registry._lazy.get(wid)  # type: ignore[attr-defined]
            _index_candidate(
                workflow_id=wid,
                purpose=(lazy.purpose if lazy else None),
                source=(lazy.source if lazy else None),
            )

        with self._lock:
            self._entries = dict(pending_entries)
            self._rebuild_idf_unlocked()

        count = len(pending_entries)
        logger.info(
            "[workflow_capability_index] Indexed %d workflows "
            "(%d eager, %d lazy), skipped_non_authoritative=%d "
            "skipped_missing_authoritative_text=%d",
            count,
            len(list(registry.eager_workflow_ids())),
            len(list(registry.lazy_workflow_ids())),
            skipped_non_authoritative,
            skipped_missing_purpose,
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

        Returns up to *max_results* matches sorted by BM25 relevance
        score descending.
        """
        query_tokens = _tokenise(query)
        if not query_tokens:
            return []

        with self._lock:
            entries = list(self._entries.values())
            idf = dict(self._idf)
            avg_dl = self._avg_dl

        scored: List[Tuple[float, _CapabilityEntry]] = []
        for entry in entries:
            if exclude_ids and entry.workflow_id in exclude_ids:
                continue
            score = self._bm25_score(query_tokens, entry, idf, avg_dl)
            if score > min_score:
                scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])

        # Normalise scores to 0-1 range for compatibility with discovery.
        max_score = scored[0][0] if scored else 1.0
        if max_score <= 0:
            max_score = 1.0

        results: List[WorkflowCapabilityMatch] = []
        for score, entry in scored[:max_results]:
            normalised = min(1.0, score / max_score)
            name = entry.metadata.get("name") or _workflow_id_to_name(entry.workflow_id)
            results.append(WorkflowCapabilityMatch(
                workflow_id=entry.workflow_id,
                name=name,
                description=entry.text[:300],
                relevance_score=round(normalised, 4),
                source="capability_index",
                metadata=entry.metadata,
            ))
        return results

    # ------------------------------------------------------------------
    # BM25 scoring
    # ------------------------------------------------------------------

    def _rebuild_idf_unlocked(self) -> None:
        """Rebuild IDF table and average document length.  Caller holds lock."""
        n = len(self._entries)
        if n == 0:
            self._idf = {}
            self._avg_dl = 0.0
            return

        doc_freq: Counter[str] = Counter()
        total_tokens = 0
        for entry in self._entries.values():
            total_tokens += len(entry.tokens)
            for tok in set(entry.tokens):
                doc_freq[tok] += 1

        self._avg_dl = total_tokens / n
        self._idf = {
            tok: math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for tok, df in doc_freq.items()
        }

    @classmethod
    def _bm25_score(
        cls,
        query_tokens: List[str],
        entry: _CapabilityEntry,
        idf: Dict[str, float],
        avg_dl: float,
    ) -> float:
        k1 = cls._K1
        b = cls._B
        dl = len(entry.tokens)
        if avg_dl <= 0:
            avg_dl = 1.0

        score = 0.0
        for tok in set(query_tokens):
            tf = entry.token_freqs.get(tok, 0)
            if tf == 0:
                continue
            tok_idf = idf.get(tok, 0.0)
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * dl / avg_dl)
            score += tok_idf * numerator / denominator
        return score


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
) -> tuple[str | None, str]:
    """Return authoritative routing text or a deterministic skip reason."""

    source_token = str(source or "").strip().lower()
    if source_token != "vontology":
        return None, "non_authoritative_source"

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
    except Exception:
        pass

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


def get_workflow_capability_index_runtime_state() -> Dict[str, Any]:
    """Return lightweight runtime state for capability-index diagnostics."""

    index = get_workflow_capability_index()
    with _INDEX_STATE_LOCK:
        return {
            "size": int(index.size),
            "ready": bool(index.size > 0),
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
        }


def _perform_workflow_capability_index_build(
    *,
    force_refresh: bool = False,
    mode: str,
) -> WorkflowCapabilityIndex:
    attempt_monotonic = time.monotonic()
    _INDEX_REBUILD_COMPLETED.clear()
    _set_workflow_capability_rebuild_state(
        build_in_progress=True,
        mode=mode,
        error=None,
        attempt_monotonic=attempt_monotonic,
    )
    try:
        from ..workflows.durable.registry_factory import (
            build_durable_workflow_registry_read_only,
        )
        from ..workflows.vontology_loader import batch_fetch_workflow_purposes
        from ..workflows.workflow_registry import LazyWorkflowRegistration

        registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
        lazy_ids = list(registry.lazy_workflow_ids())
        if lazy_ids:
            purposes = batch_fetch_workflow_purposes(lazy_ids)
            for workflow_id, purpose in purposes.items():
                registration = registry.peek_registration(workflow_id)
                if (
                    isinstance(registration, LazyWorkflowRegistration)
                    and not registration.purpose
                    and isinstance(purpose, str)
                    and purpose.strip()
                ):
                    registration.purpose = purpose.strip()

        index = get_workflow_capability_index()
        count = index.index_from_registry(registry)
        success_monotonic = time.monotonic()
        _set_workflow_capability_rebuild_state(
            build_in_progress=False,
            mode=mode,
            count=count,
            error=None,
            success_monotonic=success_monotonic,
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
) -> WorkflowCapabilityIndex:
    """Build the shared capability index on demand from authoritative workflows.

    Workflow discovery can run before deferred registry background work has
    populated the search substrate. Building on demand keeps routed turns from
    silently degrading to builtin-only candidates.
    """

    index = get_workflow_capability_index()
    if index.size > 0 and not force_refresh:
        return index

    if not block:
        _start_background_workflow_capability_index_build(force_refresh=force_refresh)
        wait_seconds = max(0.0, float(max_wait_seconds or 0.0))
        if wait_seconds > 0.0:
            _INDEX_REBUILD_COMPLETED.wait(wait_seconds)
        return get_workflow_capability_index()

    if bool(get_workflow_capability_index_runtime_state().get("build_in_progress", False)):
        wait_seconds = None if max_wait_seconds is None else max(0.0, float(max_wait_seconds))
        _INDEX_REBUILD_COMPLETED.wait(wait_seconds)
        index = get_workflow_capability_index()
        if index.size > 0 and not force_refresh:
            return index

    now = time.monotonic()
    if _workflow_capability_rebuild_recently_attempted(
        now,
        force_refresh=force_refresh,
    ):
        return get_workflow_capability_index()

    with _INDEX_REBUILD_LOCK:
        index = get_workflow_capability_index()
        if index.size > 0 and not force_refresh:
            return index
        try:
            return _perform_workflow_capability_index_build(
                force_refresh=force_refresh,
                mode="blocking",
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
) -> List[WorkflowCapabilityMatch]:
    """Search workflow capabilities, rebuilding the index on bounded misses."""

    index = ensure_workflow_capability_index_populated(
        block=not non_blocking,
        max_wait_seconds=max_wait_seconds,
    )
    results = index.search(query, max_results=max_results, min_score=min_score)
    if results:
        return results
    if non_blocking:
        return []

    refreshed = ensure_workflow_capability_index_populated(force_refresh=True)
    if refreshed is index and refreshed.size == 0:
        return []
    return refreshed.search(query, max_results=max_results, min_score=min_score)


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
    _INDEX_REBUILD_COMPLETED.set()
