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

import hashlib
import json
from datetime import datetime, timezone
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..workflows.workflow_definition_identity_service import (
    assess_workflow_id_hygiene,
    collect_workflow_action_ids,
)
from ..workflows.mcp_tool_bridge import candidate_internal_mcp_tool_names

logger = logging.getLogger(__name__)

WORKFLOW_CAPABILITY_NAMESPACE = "workflow_capabilities"
_WORKFLOW_CAPABILITY_MANIFEST_FILENAME = "workflow_capability_manifest.json"
_WORKFLOW_CAPABILITY_MANIFEST_SCHEMA_VERSION = "workflow_capability_manifest.v2"
WORKFLOW_ROUTING_INDEX_ENTRY_SCHEMA_VERSION = "workflow_routing_index_entry.v1"


def _get_positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)
    return parsed if parsed > 0.0 else float(default)


def _truthy_env_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _is_agent_test_instance() -> bool:
    return _truthy_env_value(os.getenv("VON_AGENT_TEST_INSTANCE"))


# -------------------------------------------------------------------------
# Retired Python capability overrides.
#
# Conversation-turn routing is now expected to obtain capability text from
# authoritative Vontology workflow descriptions. Keep the constant so
# diagnostics and tests can assert that no runtime override surface remains.
# -------------------------------------------------------------------------

BUILTIN_WORKFLOW_CAPABILITIES: Dict[str, str] = {}

_CAPABILITY_INDEX_REBUILD_MIN_INTERVAL_SECONDS = 30.0
_CAPABILITY_INDEX_AUTO_REBUILD_MIN_INTERVAL_SECONDS = _get_positive_float_env(
    "VON_WORKFLOW_CAPABILITY_INDEX_AUTO_REBUILD_MIN_INTERVAL_SECONDS",
    300.0,
)
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
_AUTO_REBUILD_REPAIRABLE_NAMESPACE_STATUSES = frozenset(
    {
        "signature_missing",
        "embedding_signature_mismatch",
    }
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
    "last_manifest_status": None,
    "last_manifest_detail": None,
    "last_manifest_path": None,
    "last_manifest_digest": None,
    "last_manifest_checked_at_utc": None,
    "auto_rebuild_attempt_count": 0,
    "auto_rebuild_last_attempt_monotonic": 0.0,
    "auto_rebuild_last_attempt_at_utc": None,
    "auto_rebuild_last_started_at_utc": None,
    "auto_rebuild_last_finished_at_utc": None,
    "auto_rebuild_last_status": None,
    "auto_rebuild_last_skipped_reason": None,
    "auto_rebuild_last_detail": None,
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


_MEMORY_SEARCH_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _memory_search_tokens(value: Any) -> set[str]:
    return set(_MEMORY_SEARCH_TOKEN_RE.findall(str(value or "").lower()))


def _memory_capability_field_score(
    *,
    query_tokens: set[str],
    field_text: Any,
    weight: float,
) -> float:
    field_tokens = _memory_search_tokens(field_text)
    if not query_tokens or not field_tokens:
        return 0.0
    overlap_count = len(query_tokens.intersection(field_tokens))
    if overlap_count <= 0:
        return 0.0
    query_coverage = overlap_count / max(len(query_tokens), 1)
    field_coverage = overlap_count / max(len(field_tokens), 1)
    return float(weight) * ((0.4 * query_coverage) + (0.6 * field_coverage))


def _memory_capability_match_score(query_text: str, entry: "_CapabilityEntry") -> float:
    query_tokens = _memory_search_tokens(query_text)
    if not query_tokens:
        return 0.0
    metadata = entry.metadata
    identity_text = "\n".join(
        str(value or "")
        for value in (
            entry.workflow_id,
            metadata.get("name"),
        )
    )
    exemplars_text = json.dumps(
        metadata.get("discovery_exemplars") or {},
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
    action_text = json.dumps(
        {
            "workflow_action_ids": metadata.get("workflow_action_ids") or [],
            "required_tools": metadata.get("required_tools") or [],
        },
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
    score = 0.0
    score += _memory_capability_field_score(
        query_tokens=query_tokens,
        field_text=identity_text,
        weight=1.8,
    )
    score += _memory_capability_field_score(
        query_tokens=query_tokens,
        field_text=exemplars_text,
        weight=2.0,
    )
    score += _memory_capability_field_score(
        query_tokens=query_tokens,
        field_text=action_text,
        weight=1.2,
    )
    score += _memory_capability_field_score(
        query_tokens=query_tokens,
        field_text=metadata.get("summary_text"),
        weight=0.8,
    )
    score += _memory_capability_field_score(
        query_tokens=query_tokens,
        field_text=entry.text,
        weight=0.25,
    )
    return score


def _search_memory_capability_entries(
    query_text: str,
    entries: Sequence["_CapabilityEntry"],
    *,
    exclude_ids: Optional[set[str]] = None,
) -> list[tuple[float, "_CapabilityEntry", str]]:
    scored_rows: list[tuple[float, _CapabilityEntry, str]] = []
    for entry in entries:
        cue_status = _entry_query_cue_status(query_text, entry)
        if cue_status.get("required_query_cue_absent"):
            continue
        if cue_status.get("excluded_query_cue_present"):
            continue
        if exclude_ids and entry.workflow_id in exclude_ids:
            continue
        score = _memory_capability_match_score(query_text, entry)
        if score <= 0.0:
            continue
        scored_rows.append((score, entry, "capability_index_memory"))
    return scored_rows


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
        common_root = os.path.commonpath([str(namespace_dir), str(namespaces_root)])
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


def _normalise_manifest_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_manifest_value(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalise_manifest_value(item) for item in value]
    return str(value)


def _digest_manifest_value(value: Any) -> str:
    payload = json.dumps(
        _normalise_manifest_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _entry_manifest_row(entry: "_CapabilityEntry") -> dict[str, Any]:
    return {
        "workflow_id": entry.workflow_id,
        "doc_id": entry.doc_id,
        "text": entry.text,
        "metadata": _normalise_manifest_value(entry.metadata),
    }


def _entry_from_manifest_row(row: Mapping[str, Any]) -> "_CapabilityEntry | None":
    workflow_id = str(row.get("workflow_id") or "").strip()
    doc_id = str(row.get("doc_id") or "").strip()
    text = str(row.get("text") or "").strip()
    if not workflow_id or not doc_id or not text:
        return None
    metadata_raw = row.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
    return _CapabilityEntry(
        workflow_id=workflow_id,
        doc_id=doc_id,
        text=text,
        metadata=metadata,
    )


def _entries_from_manifest_payload(
    manifest: Mapping[str, Any],
) -> dict[str, "_CapabilityEntry"]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, Sequence) or isinstance(
        raw_entries,
        (str, bytes, bytearray),
    ):
        return {}
    entries: dict[str, _CapabilityEntry] = {}
    for raw_row in raw_entries:
        if not isinstance(raw_row, Mapping):
            continue
        entry = _entry_from_manifest_row(raw_row)
        if entry is None:
            continue
        entries[entry.workflow_id] = entry
    return entries


def _authoritative_registry_workflow_ids(registry: Any) -> tuple[str, ...]:
    return tuple(
        row["workflow_id"] for row in _authoritative_registry_fingerprint_rows(registry)
    )


def is_authoritative_workflow_concept_id(concept_id: str) -> bool:
    """Return True when ``concept_id`` is an authoritative workflow concept.

    Used to scope routing-projection invalidation: only writes to concepts the
    capability index actually tracks should churn it. Description/content writes
    on unrelated concepts (e.g. task or episode-critique concepts produced by
    background workflows) must not invalidate the workflow routing index.

    Fails open (returns True) when the registry cannot be resolved, so a lookup
    failure degrades to the prior always-invalidate behaviour rather than
    silently skipping a legitimate workflow update.
    """

    cleaned = str(concept_id or "").strip()
    if not cleaned:
        return False
    try:
        from ..workflows.durable.registry_factory import (
            get_shared_workflow_registry_read_only,
        )

        registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
        authoritative_ids = set(_authoritative_registry_workflow_ids(registry))
    except Exception:
        return True
    return cleaned in authoritative_ids


def _authoritative_registry_fingerprint_rows(
    registry: Any,
) -> tuple[dict[str, str], ...]:
    rows_out: list[dict[str, str]] = []
    ids: set[str] = set()
    for method_name, attr_name in (
        ("eager_workflow_ids", "_workflows"),
        ("lazy_workflow_ids", "_lazy"),
    ):
        method = getattr(registry, method_name, None)
        rows = getattr(registry, attr_name, None)
        if not callable(method) or not isinstance(rows, Mapping):
            continue
        try:
            raw_workflow_ids = method()
        except Exception:
            continue
        if not isinstance(raw_workflow_ids, Sequence) or isinstance(
            raw_workflow_ids,
            (str, bytes, bytearray),
        ):
            continue
        workflow_ids = list(raw_workflow_ids)
        for raw_workflow_id in workflow_ids:
            workflow_id = str(raw_workflow_id or "").strip()
            if not workflow_id:
                continue
            registration = rows.get(workflow_id)
            source = str(getattr(registration, "source", "") or "").strip().lower()
            if source in _AUTHORITATIVE_CAPABILITY_SOURCES:
                ids.add(workflow_id)
                rows_out.append(
                    {
                        "workflow_id": workflow_id,
                        "source": source,
                    }
                )
    return tuple(sorted(rows_out, key=lambda item: item["workflow_id"]))


def _authoritative_registry_fingerprint_digest(registry: Any) -> str:
    return _digest_manifest_value(
        {
            "schema_version": "workflow_registry_routing_fingerprint.v1",
            "rows": list(_authoritative_registry_fingerprint_rows(registry)),
        }
    )


def _compute_workflow_capability_entries_digest(
    entries: Mapping[str, "_CapabilityEntry"],
) -> str:
    rows = [
        _entry_manifest_row(entry)
        for _workflow_id, entry in sorted(entries.items(), key=lambda item: item[0])
    ]
    payload = json.dumps(
        rows,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_workflow_capability_namespace_state(
    rag_service: Any,
) -> dict[str, Any] | None:
    get_namespace_runtime_state = getattr(
        rag_service,
        "get_namespace_runtime_state",
        None,
    )
    if not callable(get_namespace_runtime_state):
        return None
    try:
        state = get_namespace_runtime_state(WORKFLOW_CAPABILITY_NAMESPACE)
    except Exception:
        return None
    return dict(state) if isinstance(state, dict) else None


def _workflow_capability_manifest_path(rag_service: Any) -> Path | None:
    namespace_dir_builder = getattr(rag_service, "_namespace_persist_dir", None)
    if not callable(namespace_dir_builder):
        return None

    namespace_dir = Path(
        str(namespace_dir_builder(WORKFLOW_CAPABILITY_NAMESPACE))
    ).resolve()
    persistence_dir = getattr(rag_service, "persistence_dir", None)
    if isinstance(persistence_dir, str) and persistence_dir.strip():
        namespaces_root = (Path(persistence_dir).resolve() / "namespaces").resolve()
        try:
            common_root = os.path.commonpath([str(namespace_dir), str(namespaces_root)])
        except ValueError as exc:
            raise RuntimeError(
                f"workflow capability manifest path mismatch: {exc}"
            ) from exc
        if common_root != str(namespaces_root):
            raise RuntimeError(
                "workflow capability manifest path refused outside persistence root"
            )
    return namespace_dir / _WORKFLOW_CAPABILITY_MANIFEST_FILENAME


def _set_workflow_capability_manifest_state(
    *,
    status: str | None,
    detail: str | None = None,
    path: Path | str | None = None,
    digest: str | None = None,
) -> None:
    with _INDEX_STATE_LOCK:
        _INDEX_REBUILD_STATE["last_manifest_status"] = (
            str(status).strip() if status else None
        )
        _INDEX_REBUILD_STATE["last_manifest_detail"] = (
            str(detail).strip() if detail else None
        )
        _INDEX_REBUILD_STATE["last_manifest_path"] = str(path) if path else None
        _INDEX_REBUILD_STATE["last_manifest_digest"] = (
            str(digest).strip() if digest else None
        )
        _INDEX_REBUILD_STATE["last_manifest_checked_at_utc"] = _utc_now_iso()


def _read_workflow_capability_manifest(rag_service: Any) -> dict[str, Any] | None:
    path = _workflow_capability_manifest_path(rag_service)
    if path is None:
        _set_workflow_capability_manifest_state(
            status="unavailable",
            detail="RAG backend does not expose a namespace persistence path.",
        )
        return None
    if not path.is_file():
        _set_workflow_capability_manifest_state(
            status="missing",
            detail="No workflow capability manifest exists for the persisted namespace.",
            path=path,
        )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _set_workflow_capability_manifest_state(
            status="read_error",
            detail=str(exc),
            path=path,
        )
        return None
    if not isinstance(payload, dict):
        _set_workflow_capability_manifest_state(
            status="invalid",
            detail="Workflow capability manifest payload is not a mapping.",
            path=path,
        )
        return None
    return payload


def _write_workflow_capability_manifest(
    rag_service: Any,
    entries: Mapping[str, "_CapabilityEntry"],
    *,
    mode: str | None = None,
    registry: Any | None = None,
) -> None:
    path = _workflow_capability_manifest_path(rag_service)
    digest = _compute_workflow_capability_entries_digest(entries)
    if path is None:
        _set_workflow_capability_manifest_state(
            status="write_skipped",
            detail="RAG backend does not expose a namespace persistence path.",
            digest=digest,
        )
        return

    namespace_state = _get_workflow_capability_namespace_state(rag_service) or {}
    payload = {
        "schema_version": _WORKFLOW_CAPABILITY_MANIFEST_SCHEMA_VERSION,
        "namespace": WORKFLOW_CAPABILITY_NAMESPACE,
        "written_at_utc": _utc_now_iso(),
        "build_mode": str(mode or "").strip() or None,
        "entry_count": len(entries),
        "entry_digest": digest,
        "workflow_ids": sorted(str(workflow_id) for workflow_id in entries.keys()),
        "registry_fingerprint_digest": (
            _authoritative_registry_fingerprint_digest(registry)
            if registry is not None
            else None
        ),
        "entries": [
            _entry_manifest_row(entry)
            for _workflow_id, entry in sorted(
                entries.items(),
                key=lambda item: item[0],
            )
        ],
        "embedding_signature": namespace_state.get("current_embedding_signature")
        or namespace_state.get("stored_embedding_signature"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix="workflow_capability_manifest.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_path, path)
        _set_workflow_capability_manifest_state(
            status="written",
            detail=f"Workflow capability manifest written with {len(entries)} entries.",
            path=path,
            digest=digest,
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


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
        *,
        mode: str | None = None,
        write_manifest: bool = True,
        registry: Any | None = None,
    ) -> None:
        rag_service = _get_workflow_capability_rag_service()
        _reset_workflow_capability_backend_namespace(rag_service)
        payload = [self._entry_to_document(entry) for entry in pending_entries.values()]
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
        if write_manifest and payload:
            _write_workflow_capability_manifest(
                rag_service,
                pending_entries,
                mode=mode,
                registry=registry,
            )
        elif write_manifest:
            _set_workflow_capability_manifest_state(
                status="empty_not_written",
                detail="No workflow capability entries were available to persist.",
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
        merged_metadata.setdefault(
            "summary_text", clean_text.split("\n\n", 1)[0].strip()
        )
        entry = _CapabilityEntry(
            workflow_id=workflow_id,
            doc_id=_build_workflow_capability_document_id(workflow_id),
            text=clean_text,
            metadata=merged_metadata,
        )
        with self._lock:
            pending_entries = dict(self._entries)
        pending_entries[workflow_id] = entry
        self._replace_entries(pending_entries, mode="single_workflow")

    def _entries_from_registry(
        self,
        registry: Any,
    ) -> tuple[Dict[str, _CapabilityEntry], dict[str, Any]]:
        """Build capability entries from a ``WorkflowRegistry`` without syncing RAG.

        Only indexes workflows whose routing text is already authoritative:
        Vontology-sourced registrations with non-empty narrative text, plus
        the AgentTest repo-seed registrations whose registered ``purpose`` is
        the authority surface for that environment.
        Non-authoritative registrations and textless workflows are skipped so
        discovery fails closed instead of routing on guessed fallback prose.
        """
        skipped_non_authoritative = 0
        skipped_missing_purpose = 0
        skipped_invalid_workflow_id = 0
        pending_entries: Dict[str, _CapabilityEntry] = {}
        candidate_rows: list[tuple[str, Any, Any, Mapping[str, Any] | None]] = []
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

            routing_profile = None
            routing_profile_source = ""
            discovery_exemplars = None
            publication_lifecycle = None
            publication_lifecycle_source = ""
            compact_executability = None
            workflow_action_ids: list[str] | None = None
            required_tools: list[str] | None = None
            if isinstance(routing_metadata, Mapping):
                raw_discovery_exemplars = routing_metadata.get("discovery_exemplars")
                if isinstance(raw_discovery_exemplars, Mapping):
                    discovery_exemplars = dict(raw_discovery_exemplars)

                raw_routing_profile = routing_metadata.get("routing_profile")
                if isinstance(raw_routing_profile, Mapping):
                    routing_profile = dict(raw_routing_profile)
                routing_profile_source = str(
                    routing_metadata.get("routing_profile_source") or ""
                ).strip()

                raw_publication_lifecycle = routing_metadata.get(
                    "publication_lifecycle"
                )
                if isinstance(raw_publication_lifecycle, Mapping):
                    publication_lifecycle = dict(raw_publication_lifecycle)
                publication_lifecycle_source = str(
                    routing_metadata.get("publication_lifecycle_source") or ""
                ).strip()

                raw_compact_executability = routing_metadata.get(
                    "compact_executability"
                )
                if isinstance(raw_compact_executability, Mapping):
                    compact_executability = dict(raw_compact_executability)

                raw_workflow_action_ids = routing_metadata.get("workflow_action_ids")
                if isinstance(raw_workflow_action_ids, Sequence) and not isinstance(
                    raw_workflow_action_ids,
                    (str, bytes, bytearray),
                ):
                    workflow_action_ids = _dedupe_capability_strings(
                        raw_workflow_action_ids
                    )

                raw_required_tools = routing_metadata.get("required_tools")
                if isinstance(raw_required_tools, Sequence) and not isinstance(
                    raw_required_tools,
                    (str, bytes, bytearray),
                ):
                    required_tools = _dedupe_capability_strings(raw_required_tools)

            pending_entries[workflow_id] = _CapabilityEntry(
                workflow_id=workflow_id,
                doc_id=_build_workflow_capability_document_id(workflow_id),
                text=text,
                metadata={
                    "routing_index_schema_version": WORKFLOW_ROUTING_INDEX_ENTRY_SCHEMA_VERSION,
                    "name": _workflow_id_to_name(workflow_id),
                    "source": str(source or "unknown"),
                    "authority_source": str(source or "unknown"),
                    "description_source": reason,
                    "purpose": _normalise_capability_text(purpose),
                    "summary_text": text.split("\n\n", 1)[0].strip(),
                    "has_authoritative_routing_text": (
                        _capability_text_reason_is_authoritative(
                            reason=reason,
                            source=source,
                        )
                    ),
                    "routing_profile": routing_profile,
                    "routing_profile_source": routing_profile_source,
                    "discovery_exemplars": discovery_exemplars,
                    "publication_lifecycle": publication_lifecycle,
                    "publication_lifecycle_source": publication_lifecycle_source,
                    "compact_executability": compact_executability,
                    "workflow_action_ids": workflow_action_ids,
                    "required_tools": required_tools,
                },
            )

        # Eager registrations.
        for wid in list(registry.eager_workflow_ids()):
            reg = registry._workflows.get(wid)  # type: ignore[attr-defined]
            workflow_id = str(wid or "").strip()
            purpose = reg.purpose if reg else None
            source = reg.source if reg else None
            if workflow_id:
                definition_metadata = (
                    dict(getattr(reg.definition, "metadata", {}) or {})
                    if reg is not None
                    and isinstance(getattr(reg.definition, "metadata", None), Mapping)
                    else None
                )
                definition_metadata = _workflow_definition_capability_metadata(
                    getattr(reg, "definition", None) if reg is not None else None,
                    base_metadata=definition_metadata,
                )
                candidate_rows.append(
                    (workflow_id, purpose, source, definition_metadata)
                )
                if str(source or "").strip().lower() == "vontology":
                    authoritative_workflow_ids.append(workflow_id)

        # Lazy registrations (metadata only, no definition load).
        for wid in list(registry.lazy_workflow_ids()):
            lazy = registry._lazy.get(wid)  # type: ignore[attr-defined]
            workflow_id = str(wid or "").strip()
            purpose = lazy.purpose if lazy else None
            source = lazy.source if lazy else None
            if workflow_id:
                definition_metadata = None
                resolved_registration = (
                    getattr(lazy, "_resolved", None) if lazy else None
                )
                resolved_definition = (
                    getattr(resolved_registration, "definition", None)
                    if resolved_registration is not None
                    else None
                )
                resolved_definition_metadata = getattr(
                    resolved_definition,
                    "metadata",
                    None,
                )
                if isinstance(resolved_definition_metadata, Mapping):
                    definition_metadata = dict(resolved_definition_metadata)
                definition_metadata = _workflow_definition_capability_metadata(
                    resolved_definition,
                    base_metadata=definition_metadata,
                )
                candidate_rows.append(
                    (workflow_id, purpose, source, definition_metadata)
                )
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

        for workflow_id, purpose, source, definition_metadata in candidate_rows:
            routing_metadata = (
                authoritative_routing_metadata.get(workflow_id) or definition_metadata
            )
            _index_candidate(
                workflow_id=workflow_id,
                purpose=purpose,
                source=source,
                routing_metadata=routing_metadata,
            )

        count = len(pending_entries)
        diagnostics = {
            "count": count,
            "eager_count": len(list(registry.eager_workflow_ids())),
            "lazy_count": len(list(registry.lazy_workflow_ids())),
            "skipped_non_authoritative": skipped_non_authoritative,
            "skipped_missing_authoritative_text": skipped_missing_purpose,
            "skipped_invalid_workflow_id": skipped_invalid_workflow_id,
        }
        return pending_entries, diagnostics

    def index_from_registry(self, registry: Any, *, mode: str | None = None) -> int:
        """Index all authoritative workflows from a ``WorkflowRegistry``."""

        pending_entries, diagnostics = self._entries_from_registry(registry)
        self._replace_entries(
            pending_entries,
            mode=mode or "registry",
            registry=registry,
        )

        count = len(pending_entries)
        logger.info(
            "[workflow_capability_index] Indexed and synced %d workflows "
            "(%d eager, %d lazy), skipped_non_authoritative=%d "
            "skipped_missing_authoritative_text=%d "
            "skipped_invalid_workflow_id=%d",
            count,
            int(diagnostics.get("eager_count") or 0),
            int(diagnostics.get("lazy_count") or 0),
            int(diagnostics.get("skipped_non_authoritative") or 0),
            int(diagnostics.get("skipped_missing_authoritative_text") or 0),
            int(diagnostics.get("skipped_invalid_workflow_id") or 0),
        )
        return count

    def load_entries_from_registry_without_rag_sync(
        self,
        registry: Any,
        *,
        mode: str | None = None,
    ) -> int:
        """Load authoritative workflow entries into process memory only.

        AgentTest uses repo-seed workflow definitions and deterministic replay
        support. It needs the same represented capability entries, but should
        not wake persisted vector-index loading during server startup.
        """

        pending_entries, diagnostics = self._entries_from_registry(registry)
        with self._lock:
            self._entries = dict(pending_entries)

        count = len(pending_entries)
        _set_workflow_capability_manifest_state(
            status="agent_test_memory_only",
            detail=(
                "Loaded AgentTest workflow capability entries from the "
                "authoritative repo-seed registry without RAG sync or persisted "
                "namespace warm-up."
            ),
        )
        logger.info(
            "[workflow_capability_index] AgentTest loaded %d workflow entries "
            "in memory only (%d eager, %d lazy), skipped_non_authoritative=%d "
            "skipped_missing_authoritative_text=%d skipped_invalid_workflow_id=%d",
            count,
            int(diagnostics.get("eager_count") or 0),
            int(diagnostics.get("lazy_count") or 0),
            int(diagnostics.get("skipped_non_authoritative") or 0),
            int(diagnostics.get("skipped_missing_authoritative_text") or 0),
            int(diagnostics.get("skipped_invalid_workflow_id") or 0),
        )
        return count

    def load_from_persisted_namespace_if_current(self, registry: Any) -> bool:
        """Load process-local entries when the persisted RAG namespace is current.

        The manifest is a support-surface fingerprint of the authoritative
        Vontology-derived capability documents.  It lets startup avoid deleting
        and re-embedding a compatible namespace while still detecting changed
        workflow routing text before trusting the persisted index.
        """

        rag_service = _get_workflow_capability_rag_service()
        namespace_state = _get_workflow_capability_namespace_state(rag_service)
        if isinstance(namespace_state, dict):
            if not bool(namespace_state.get("compatible", False)):
                _set_workflow_capability_manifest_state(
                    status="namespace_incompatible",
                    detail=str(namespace_state.get("detail") or "").strip()
                    or "Persisted namespace is not compatible with this runtime.",
                    path=_workflow_capability_manifest_path(rag_service),
                )
                return False
            if not bool(namespace_state.get("has_persisted_index", False)):
                _set_workflow_capability_manifest_state(
                    status="namespace_missing",
                    detail="No persisted workflow capability namespace exists.",
                    path=_workflow_capability_manifest_path(rag_service),
                )
                return False

        manifest = _read_workflow_capability_manifest(rag_service)
        manifest_path = _workflow_capability_manifest_path(rag_service)
        if not isinstance(manifest, dict):
            return False

        if (
            manifest.get("schema_version")
            != _WORKFLOW_CAPABILITY_MANIFEST_SCHEMA_VERSION
        ):
            _set_workflow_capability_manifest_state(
                status="schema_mismatch",
                detail="Workflow capability manifest schema is unsupported.",
                path=manifest_path,
            )
            return False
        if manifest.get("namespace") != WORKFLOW_CAPABILITY_NAMESPACE:
            _set_workflow_capability_manifest_state(
                status="namespace_mismatch",
                detail="Workflow capability manifest belongs to a different namespace.",
                path=manifest_path,
            )
            return False

        manifest_digest = str(manifest.get("entry_digest") or "").strip()
        manifest_entries = _entries_from_manifest_payload(manifest)
        manifest_entry_digest = _compute_workflow_capability_entries_digest(
            manifest_entries
        )
        if not manifest_entries:
            _set_workflow_capability_manifest_state(
                status="entries_missing",
                detail=(
                    "Workflow capability manifest did not contain materialised "
                    "routing projection entries."
                ),
                path=manifest_path,
            )
            return False
        if manifest_digest != manifest_entry_digest:
            _set_workflow_capability_manifest_state(
                status="digest_mismatch",
                detail=(
                    "Workflow capability manifest entry digest does not match "
                    "its materialised routing projection entries."
                ),
                path=manifest_path,
                digest=manifest_entry_digest,
            )
            logger.info(
                "[workflow_capability_index] Persisted manifest digest mismatch "
                "(manifest=%s current=%s); rebuilding namespace.",
                manifest_digest,
                manifest_entry_digest,
            )
            return False

        current_registry_fingerprint = _authoritative_registry_fingerprint_digest(
            registry
        )
        manifest_registry_fingerprint = str(
            manifest.get("registry_fingerprint_digest") or ""
        ).strip()
        if not manifest_registry_fingerprint:
            manifest_workflow_ids = tuple(
                sorted(str(item) for item in manifest_entries.keys())
            )
            registry_workflow_ids = _authoritative_registry_workflow_ids(registry)
            if registry_workflow_ids and manifest_workflow_ids != registry_workflow_ids:
                _set_workflow_capability_manifest_state(
                    status="registry_workflow_set_mismatch",
                    detail=(
                        "Authoritative workflow registry IDs differ from the "
                        "legacy materialised routing projection manifest."
                    ),
                    path=manifest_path,
                    digest=manifest_entry_digest,
                )
                return False
        elif manifest_registry_fingerprint != current_registry_fingerprint:
            _set_workflow_capability_manifest_state(
                status="registry_fingerprint_mismatch",
                detail=(
                    "Authoritative workflow registry metadata differs from the "
                    "materialised routing projection manifest."
                ),
                path=manifest_path,
                digest=manifest_entry_digest,
            )
            return False

        if isinstance(namespace_state, dict):
            current_signature = namespace_state.get("current_embedding_signature")
            manifest_signature = manifest.get("embedding_signature")
            if (
                isinstance(current_signature, Mapping)
                and isinstance(manifest_signature, Mapping)
                and dict(current_signature) != dict(manifest_signature)
            ):
                _set_workflow_capability_manifest_state(
                    status="embedding_signature_mismatch",
                    detail=(
                        "Workflow capability manifest was built with a different "
                        "embedding signature."
                    ),
                    path=manifest_path,
                    digest=manifest_entry_digest,
                )
                return False

        with self._lock:
            self._entries = dict(manifest_entries)
        _set_workflow_capability_manifest_state(
            status="loaded",
            detail=(
                "Loaded workflow capability process cache and compact routing "
                "projection from a compatible persisted namespace without "
                "rebuilding embeddings or rereading routing text."
            ),
            path=manifest_path,
            digest=manifest_entry_digest,
        )
        logger.info(
            "[workflow_capability_index] Loaded %d workflows from persisted "
            "namespace manifest without RAG reset/upsert or routing metadata "
            "reread.",
            len(manifest_entries),
        )
        return True

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
        retrieval_candidate_limit = (
            _compute_workflow_capability_retrieval_candidate_limit(max_results)
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

        scored_rows: List[Tuple[float, _CapabilityEntry, str]] = []
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
            cue_status = _entry_query_cue_status(clean_query, entry)
            if cue_status.get("required_query_cue_absent"):
                continue
            if cue_status.get("excluded_query_cue_present"):
                continue
            if exclude_ids and entry.workflow_id in exclude_ids:
                continue
            seen_ids.add(workflow_id)
            scored_rows.append(
                (_coerce_retrieval_score(result.get("score")), entry, "capability_index")
            )

        if not scored_rows:
            scored_rows = _search_memory_capability_entries(
                clean_query,
                entries,
                exclude_ids=exclude_ids,
            )

        if not scored_rows:
            return []

        scored_rows.sort(key=lambda item: (-item[0], item[1].workflow_id))
        max_score = max(
            (score for score, _entry, _source in scored_rows if score > 0.0),
            default=1.0,
        )

        results: List[WorkflowCapabilityMatch] = []
        for score, entry, source in scored_rows:
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
                    source=source,
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


_AUTHORITATIVE_CAPABILITY_SOURCES: frozenset[str] = frozenset(
    {"vontology", "repo_seed_agent_test"}
)


def _capability_discovery_exemplar_text(
    discovery_exemplars: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(discovery_exemplars, Mapping):
        return []
    capability_parts: list[str] = []
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
            capability_parts.append("Example requests: " + " | ".join(example_lines))
    routing_notes = discovery_exemplars.get("routing_notes") or []
    if isinstance(routing_notes, Sequence) and not isinstance(routing_notes, str):
        routing_note_lines = [
            str(item).strip()
            for item in routing_notes
            if isinstance(item, str) and str(item).strip()
        ]
        if routing_note_lines:
            capability_parts.append("Routing notes: " + " | ".join(routing_note_lines))
    required_query_cues = discovery_exemplars.get("required_query_cues") or []
    if isinstance(required_query_cues, Sequence) and not isinstance(
        required_query_cues, str
    ):
        required_query_cue_text = ", ".join(
            str(item).strip()
            for item in required_query_cues
            if isinstance(item, str) and str(item).strip()
        )
        if required_query_cue_text:
            capability_parts.append(f"Required query cues: {required_query_cue_text}")
    excluded_query_cues = discovery_exemplars.get("excluded_query_cues") or []
    if isinstance(excluded_query_cues, Sequence) and not isinstance(
        excluded_query_cues, str
    ):
        excluded_query_cue_text = ", ".join(
            str(item).strip()
            for item in excluded_query_cues
            if isinstance(item, str) and str(item).strip()
        )
        if excluded_query_cue_text:
            capability_parts.append(f"Excluded query cues: {excluded_query_cue_text}")
    negative_query_cues = discovery_exemplars.get("negative_query_cues") or []
    if isinstance(negative_query_cues, Sequence) and not isinstance(
        negative_query_cues, str
    ):
        negative_query_cue_text = ", ".join(
            str(item).strip()
            for item in negative_query_cues
            if isinstance(item, str) and str(item).strip()
        )
        if negative_query_cue_text:
            capability_parts.append(f"Negative query cues: {negative_query_cue_text}")
    return capability_parts


_QUERY_CUE_TOKEN_RE = re.compile(r"[a-z0-9_#.:/-]+")


def _normalise_query_cue_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.lower().split())


def _capability_query_cues(
    discovery_exemplars: Mapping[str, Any] | None,
    *field_names: str,
) -> list[str]:
    if not isinstance(discovery_exemplars, Mapping):
        return []
    values: list[str] = []
    for field_name in field_names:
        raw_values = discovery_exemplars.get(field_name)
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if not isinstance(raw_values, Sequence):
            continue
        for item in raw_values:
            text = _normalise_query_cue_text(item)
            if text:
                values.append(text)
    return _dedupe_capability_strings(values)


def _query_contains_represented_cue(query_text: str, cue: str) -> bool:
    query = _normalise_query_cue_text(query_text)
    clean_cue = _normalise_query_cue_text(cue)
    if not query or not clean_cue:
        return False
    if len(clean_cue) <= 3:
        return clean_cue in set(_QUERY_CUE_TOKEN_RE.findall(query))
    return clean_cue in query


def _entry_query_cue_status(
    query_text: str,
    entry: "_CapabilityEntry",
) -> dict[str, Any]:
    discovery_exemplars = entry.metadata.get("discovery_exemplars")
    if not isinstance(discovery_exemplars, Mapping):
        return {
            "required_query_cues": [],
            "matched_required_query_cues": [],
            "required_query_cue_absent": False,
            "excluded_query_cues": [],
            "matched_excluded_query_cues": [],
            "excluded_query_cue_present": False,
        }
    required_cues = _capability_query_cues(
        discovery_exemplars,
        "required_query_cues",
        "required_cues",
        "source_required_cues",
    )
    excluded_cues = _capability_query_cues(
        discovery_exemplars,
        "excluded_query_cues",
        "exclude_query_cues",
    )
    matched_required = [
        cue for cue in required_cues if _query_contains_represented_cue(query_text, cue)
    ]
    matched_excluded = [
        cue for cue in excluded_cues if _query_contains_represented_cue(query_text, cue)
    ]
    return {
        "required_query_cues": required_cues,
        "matched_required_query_cues": matched_required,
        "required_query_cue_absent": bool(required_cues and not matched_required),
        "excluded_query_cues": excluded_cues,
        "matched_excluded_query_cues": matched_excluded,
        "excluded_query_cue_present": bool(matched_excluded),
    }


def _capability_string_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(_capability_string_values(item))
        return values
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = []
        for item in value:
            values.extend(_capability_string_values(item))
        return values
    return []


def _dedupe_capability_strings(values: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(text)
    return ordered


def _normalise_contract_symbol_set(values: Sequence[Any] | None) -> set[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return set()
    return {
        str(value).strip()
        for value in values
        if isinstance(value, str) and str(value).strip()
    }


@lru_cache(maxsize=2048)
def _dispatch_surface_families_for_tool(tool_name: str) -> tuple[str, ...]:
    clean_tool_name = str(tool_name or "").strip()
    if not clean_tool_name:
        return ()
    try:
        from .tool_metadata_service import get_tool_dispatch_surface_metadata

        surface = get_tool_dispatch_surface_metadata(clean_tool_name)
    except Exception:
        surface = None
    if surface is None:
        return ()
    families = _dedupe_capability_strings(
        [
            getattr(surface, "surface_family", None),
            getattr(surface, "evidence_surface_family", None),
        ]
    )
    return tuple(families)


def _dispatch_surface_family_set(tool_names: Sequence[str]) -> set[str]:
    families: set[str] = set()
    for tool_name in tool_names:
        families.update(_dispatch_surface_families_for_tool(tool_name))
    return families


def _entry_declared_tool_names(entry: "_CapabilityEntry") -> set[str]:
    metadata = entry.metadata if isinstance(entry.metadata, Mapping) else {}
    values: list[Any] = []
    raw_tools = metadata.get("required_tools")
    if isinstance(raw_tools, Sequence) and not isinstance(
        raw_tools,
        (str, bytes, bytearray),
    ):
        values.extend(raw_tools)
    raw_action_ids = metadata.get("workflow_action_ids")
    if isinstance(raw_action_ids, Sequence) and not isinstance(
        raw_action_ids,
        (str, bytes, bytearray),
    ):
        for action_id in raw_action_ids:
            values.extend(candidate_internal_mcp_tool_names(str(action_id or "")))
    return set(_dedupe_capability_strings(values))


def _entry_declared_action_ids(entry: "_CapabilityEntry") -> set[str]:
    metadata = entry.metadata if isinstance(entry.metadata, Mapping) else {}
    raw_action_ids = metadata.get("workflow_action_ids")
    if not isinstance(raw_action_ids, Sequence) or isinstance(
        raw_action_ids,
        (str, bytes, bytearray),
    ):
        return set()
    return set(_dedupe_capability_strings(raw_action_ids))


def _score_contract_capability_entry(
    entry: "_CapabilityEntry",
    *,
    contract_tools: set[str],
    contract_actions: set[str],
) -> tuple[float, dict[str, Any]] | None:
    declared_tools = _entry_declared_tool_names(entry)
    declared_actions = _entry_declared_action_ids(entry)
    tool_overlap = contract_tools.intersection(declared_tools)
    action_overlap = contract_actions.intersection(declared_actions)
    contract_families = _dispatch_surface_family_set(sorted(contract_tools))
    declared_families = _dispatch_surface_family_set(sorted(declared_tools))
    surface_overlap = contract_families.intersection(declared_families)

    if not tool_overlap and not action_overlap and not surface_overlap:
        return None

    tool_coverage_ratio = (
        len(tool_overlap) / len(contract_tools) if contract_tools else 0.0
    )
    action_coverage_ratio = (
        len(action_overlap) / len(contract_actions) if contract_actions else 0.0
    )
    surface_coverage_ratio = (
        len(surface_overlap) / len(contract_families) if contract_families else 0.0
    )
    exact_tool_coverage = bool(contract_tools and contract_tools.issubset(declared_tools))
    exact_action_coverage = bool(
        contract_actions and contract_actions.issubset(declared_actions)
    )

    score = 0.70
    if exact_tool_coverage:
        score += 0.20
    else:
        score += min(0.12, tool_coverage_ratio * 0.12)
    if exact_action_coverage:
        score += 0.07
    else:
        score += min(0.05, action_coverage_ratio * 0.05)
    score += min(0.08, surface_coverage_ratio * 0.08)
    if tool_overlap:
        score += 0.03
    if action_overlap:
        score += 0.02
    score = min(0.99, round(score, 4))

    metadata = {
        "schema_version": "workflow_contract_capability_match.v1",
        "contract_required_tools": sorted(contract_tools),
        "contract_required_actions": sorted(contract_actions),
        "candidate_declared_tools": sorted(declared_tools),
        "candidate_declared_actions": sorted(declared_actions),
        "tool_overlap": sorted(tool_overlap),
        "action_overlap": sorted(action_overlap),
        "contract_tool_surface_families": sorted(contract_families),
        "candidate_tool_surface_families": sorted(declared_families),
        "tool_surface_family_overlap": sorted(surface_overlap),
        "exact_tool_coverage": exact_tool_coverage,
        "exact_action_coverage": exact_action_coverage,
        "tool_coverage_ratio": round(tool_coverage_ratio, 4),
        "action_coverage_ratio": round(action_coverage_ratio, 4),
        "surface_coverage_ratio": round(surface_coverage_ratio, 4),
    }
    return score, metadata


def _workflow_definition_capability_metadata(
    definition: Any | None,
    *,
    base_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    metadata = dict(base_metadata) if isinstance(base_metadata, Mapping) else {}
    if definition is None:
        return metadata or None
    action_ids = _dedupe_capability_strings(collect_workflow_action_ids(definition))
    if action_ids:
        metadata.setdefault("workflow_action_ids", action_ids)
    return metadata or None


def _capability_metadata_contract_text(
    routing_metadata: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(routing_metadata, Mapping):
        return []

    fields: tuple[tuple[tuple[str, ...], str], ...] = (
        (
            (
                "required_tools",
                "required_tool_ids",
                "turn_expected_required_tools",
                "invoked_tools",
                "tool_names",
            ),
            "Required tools",
        ),
        (
            (
                "workflow_action_ids",
                "action_ids",
                "required_actions",
                "required_action_ids",
                "required_workflow_actions",
                "invokes_actions",
                "invoked_actions",
            ),
            "Workflow actions",
        ),
        (
            ("postconditions", "required_postconditions", "required_effects"),
            "Postconditions",
        ),
        (
            ("target_type_ids", "target_types", "requested_type_ids"),
            "Target type IDs",
        ),
    )
    parts: list[str] = []
    for field_names, label in fields:
        values: list[str] = []
        for field_name in field_names:
            values.extend(_capability_string_values(routing_metadata.get(field_name)))
        values = _dedupe_capability_strings(values)
        if values:
            parts.append(f"{label}: {', '.join(values[:12])}")
    return parts


def _authoritative_discovery_exemplar_source(
    *,
    source_token: str,
    discovery_exemplars_source: str,
) -> bool:
    source_text = str(discovery_exemplars_source or "").strip()
    if source_text.startswith("text_relation:"):
        return True
    return source_token == "repo_seed_agent_test" and source_text.startswith(
        "repo_seed_text_relation:"
    )


def _capability_text_reason_is_authoritative(
    *,
    reason: str,
    source: Any,
) -> bool:
    reason_text = str(reason or "").strip()
    if reason_text.startswith("text_relation:"):
        return True
    source_token = str(source or "").strip().lower()
    return source_token == "repo_seed_agent_test" and (
        reason_text == "authoritative_registration_purpose"
        or "repo_seed_text_relation:" in reason_text
    )


def _resolve_authoritative_capability_text(
    *,
    workflow_id: str,
    source: Any,
    purpose: Any,
    routing_metadata: Mapping[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Return authoritative routing text or a deterministic skip reason."""

    source_token = str(source or "").strip().lower()
    if source_token not in _AUTHORITATIVE_CAPABILITY_SOURCES:
        return None, "non_authoritative_source"

    relation_text = ""
    relation_source = ""
    discovery_exemplars: Mapping[str, Any] | None = None
    discovery_exemplars_source = ""
    if isinstance(routing_metadata, Mapping):
        relation_text = _normalise_capability_text(
            routing_metadata.get("description_text")
        )
        relation_source = str(routing_metadata.get("description_source") or "").strip()
        raw_exemplars = routing_metadata.get("discovery_exemplars")
        if isinstance(raw_exemplars, Mapping):
            discovery_exemplars = raw_exemplars
        discovery_exemplars_source = str(
            routing_metadata.get("discovery_exemplars_source") or ""
        ).strip()
    elif source_token == "vontology":
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

    exemplar_parts = _capability_discovery_exemplar_text(discovery_exemplars)
    metadata_contract_parts = _capability_metadata_contract_text(routing_metadata)
    exemplar_source_is_authoritative = _authoritative_discovery_exemplar_source(
        source_token=source_token,
        discovery_exemplars_source=discovery_exemplars_source,
    )

    if relation_text and relation_source.startswith("text_relation:"):
        capability_parts = [relation_text]
        capability_parts.extend(metadata_contract_parts)
        if exemplar_source_is_authoritative:
            capability_parts.extend(exemplar_parts)
        source_parts = [relation_source]
        if exemplar_source_is_authoritative:
            source_parts.append(discovery_exemplars_source)
        return "\n\n".join(capability_parts), "+".join(source_parts)

    purpose_text = _normalise_capability_text(purpose)
    if not purpose_text:
        return None, "missing_authoritative_purpose"

    if exemplar_source_is_authoritative and exemplar_parts:
        source = "authoritative_registration_purpose"
        if isinstance(discovery_exemplars_source, str) and discovery_exemplars_source:
            source = f"{source}+{discovery_exemplars_source}"
        return (
            "\n\n".join([purpose_text, *metadata_contract_parts, *exemplar_parts]),
            source,
        )

    return "\n\n".join([purpose_text, *metadata_contract_parts]), (
        "authoritative_registration_purpose"
    )


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
        and (now_monotonic - last_attempt)
        < _CAPABILITY_INDEX_REBUILD_MIN_INTERVAL_SECONDS
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


def _snapshot_workflow_capability_auto_rebuild_state_locked() -> dict[str, Any]:
    return {
        "attempt_count": int(
            _INDEX_REBUILD_STATE.get("auto_rebuild_attempt_count", 0) or 0
        ),
        "last_attempt_at_utc": _INDEX_REBUILD_STATE.get(
            "auto_rebuild_last_attempt_at_utc"
        ),
        "last_started_at_utc": _INDEX_REBUILD_STATE.get(
            "auto_rebuild_last_started_at_utc"
        ),
        "last_finished_at_utc": _INDEX_REBUILD_STATE.get(
            "auto_rebuild_last_finished_at_utc"
        ),
        "last_status": _INDEX_REBUILD_STATE.get("auto_rebuild_last_status"),
        "last_skipped_reason": _INDEX_REBUILD_STATE.get(
            "auto_rebuild_last_skipped_reason"
        ),
        "last_detail": _INDEX_REBUILD_STATE.get("auto_rebuild_last_detail"),
        "min_interval_seconds": _CAPABILITY_INDEX_AUTO_REBUILD_MIN_INTERVAL_SECONDS,
    }


def _snapshot_workflow_capability_auto_rebuild_state() -> dict[str, Any]:
    with _INDEX_STATE_LOCK:
        return _snapshot_workflow_capability_auto_rebuild_state_locked()


def _record_workflow_capability_auto_rebuild_state(
    *,
    status: str | None = None,
    skipped_reason: str | None = None,
    detail: str | None = None,
    mark_attempt: bool = False,
    mark_started: bool = False,
    mark_finished: bool = False,
    attempt_monotonic: float | None = None,
) -> None:
    now_utc = _utc_now_iso()
    with _INDEX_STATE_LOCK:
        if mark_attempt:
            _INDEX_REBUILD_STATE["auto_rebuild_attempt_count"] = (
                int(_INDEX_REBUILD_STATE.get("auto_rebuild_attempt_count", 0) or 0) + 1
            )
            _INDEX_REBUILD_STATE["auto_rebuild_last_attempt_at_utc"] = now_utc
            if attempt_monotonic is not None:
                _INDEX_REBUILD_STATE["auto_rebuild_last_attempt_monotonic"] = float(
                    attempt_monotonic
                )
        if mark_started:
            _INDEX_REBUILD_STATE["auto_rebuild_last_started_at_utc"] = now_utc
        if mark_finished:
            _INDEX_REBUILD_STATE["auto_rebuild_last_finished_at_utc"] = now_utc
        if status is not None:
            _INDEX_REBUILD_STATE["auto_rebuild_last_status"] = str(status)
        _INDEX_REBUILD_STATE["auto_rebuild_last_skipped_reason"] = skipped_reason
        if detail is not None:
            _INDEX_REBUILD_STATE["auto_rebuild_last_detail"] = str(detail)


def _workflow_capability_namespace_status(
    namespace_state: Mapping[str, Any] | None,
) -> str:
    if not isinstance(namespace_state, Mapping):
        return ""
    return str(namespace_state.get("status") or "").strip()


def _workflow_capability_namespace_auto_rebuildable(
    namespace_state: Mapping[str, Any] | None,
) -> bool:
    return (
        _workflow_capability_namespace_status(namespace_state)
        in _AUTO_REBUILD_REPAIRABLE_NAMESPACE_STATUSES
    )


def _workflow_capability_runtime_embedder_ready_for_auto_rebuild(
    namespace_state: Mapping[str, Any],
) -> tuple[bool, str | None]:
    current_signature = namespace_state.get("current_embedding_signature")
    if not isinstance(current_signature, Mapping) or not current_signature:
        return False, "current_embedding_signature_missing"

    try:
        rag_service = _get_workflow_capability_rag_service()
        get_runtime_embed_model = getattr(rag_service, "get_runtime_embed_model", None)
        if callable(get_runtime_embed_model) and get_runtime_embed_model() is None:
            return False, "runtime_embedder_unavailable"
    except Exception as exc:
        return False, f"runtime_embedder_check_failed:{exc}"
    return True, None


def _maybe_start_workflow_capability_index_auto_rebuild(
    runtime_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Start one throttled repair rebuild for a derived-index signature mismatch."""

    event: dict[str, Any] = {
        "checked": False,
        "started": False,
        "skipped_reason": None,
        "detail": None,
    }
    if bool(runtime_state.get("ready", False)):
        return event

    namespace_state_raw = runtime_state.get("namespace_state")
    namespace_state: Mapping[str, Any] | None = (
        namespace_state_raw if isinstance(namespace_state_raw, Mapping) else None
    )
    if not _workflow_capability_namespace_auto_rebuildable(namespace_state):
        return event

    event["checked"] = True
    namespace_status = _workflow_capability_namespace_status(namespace_state)
    namespace_detail = (
        str(namespace_state.get("detail") or "").strip()
        if isinstance(namespace_state, Mapping)
        else ""
    )

    if bool(runtime_state.get("build_in_progress", False)):
        event["skipped_reason"] = "build_in_progress"
        _record_workflow_capability_auto_rebuild_state(
            status="skipped",
            skipped_reason="build_in_progress",
            detail=namespace_detail,
        )
        return event

    embedder_ready, embedder_skip_reason = (
        _workflow_capability_runtime_embedder_ready_for_auto_rebuild(namespace_state)
        if isinstance(namespace_state, Mapping)
        else (False, "namespace_state_missing")
    )
    if not embedder_ready:
        event["skipped_reason"] = embedder_skip_reason
        _record_workflow_capability_auto_rebuild_state(
            status="skipped",
            skipped_reason=embedder_skip_reason,
            detail=namespace_detail,
        )
        return event

    now = time.monotonic()
    with _INDEX_STATE_LOCK:
        if bool(_INDEX_REBUILD_STATE.get("build_in_progress", False)):
            event["skipped_reason"] = "build_in_progress"
            _INDEX_REBUILD_STATE["auto_rebuild_last_status"] = "skipped"
            _INDEX_REBUILD_STATE["auto_rebuild_last_skipped_reason"] = (
                "build_in_progress"
            )
            _INDEX_REBUILD_STATE["auto_rebuild_last_detail"] = namespace_detail
            return event
        last_attempt = float(
            _INDEX_REBUILD_STATE.get("auto_rebuild_last_attempt_monotonic", 0.0) or 0.0
        )
        if (
            last_attempt > 0.0
            and (now - last_attempt)
            < _CAPABILITY_INDEX_AUTO_REBUILD_MIN_INTERVAL_SECONDS
        ):
            event["skipped_reason"] = "throttled"
            _INDEX_REBUILD_STATE["auto_rebuild_last_status"] = "skipped"
            _INDEX_REBUILD_STATE["auto_rebuild_last_skipped_reason"] = "throttled"
            _INDEX_REBUILD_STATE["auto_rebuild_last_detail"] = namespace_detail
            return event

    _record_workflow_capability_auto_rebuild_state(
        status="starting",
        skipped_reason=None,
        detail=namespace_detail,
        mark_attempt=True,
        attempt_monotonic=now,
    )
    logger.warning(
        "[workflow_capability_index] auto rebuild starting for repairable "
        "namespace status %s: %s",
        namespace_status,
        namespace_detail,
    )
    started = _start_background_workflow_capability_index_build(
        force_refresh=True,
        mode="auto_rebuild",
    )
    event["started"] = bool(started)
    if started:
        _record_workflow_capability_auto_rebuild_state(
            status="started",
            skipped_reason=None,
            detail=namespace_detail,
            mark_started=True,
        )
    else:
        event["skipped_reason"] = "background_start_refused"
        _record_workflow_capability_auto_rebuild_state(
            status="skipped",
            skipped_reason="background_start_refused",
            detail=namespace_detail,
        )
    return event


def _maybe_start_workflow_capability_index_background_initialisation(
    runtime_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Start a non-blocking initial load/build when the process cache is empty.

    A compatible persisted RAG namespace can exist while the process-local
    capability cache is still empty after restart. The status endpoint should
    report that initialisation has started rather than doing registry and
    materialisation work synchronously on the request path.
    """

    event: dict[str, Any] = {
        "checked": False,
        "started": False,
        "skipped_reason": None,
        "detail": None,
    }
    if bool(runtime_state.get("ready", False)):
        return event
    if bool(runtime_state.get("build_in_progress", False)):
        event["skipped_reason"] = "build_in_progress"
        return event
    if int(runtime_state.get("size") or 0) > 0:
        event["skipped_reason"] = "process_cache_present"
        return event

    namespace_state_raw = runtime_state.get("namespace_state")
    namespace_state: Mapping[str, Any] | None = (
        namespace_state_raw if isinstance(namespace_state_raw, Mapping) else None
    )
    if isinstance(namespace_state, Mapping) and not bool(
        namespace_state.get("compatible", False)
    ):
        event["skipped_reason"] = "namespace_incompatible"
        return event
    if isinstance(namespace_state, Mapping) and not bool(
        namespace_state.get("has_persisted_index", False)
    ):
        event["skipped_reason"] = "persisted_namespace_missing"
        return event

    event["checked"] = True
    embedder_ready, embedder_skip_reason = (
        _workflow_capability_runtime_embedder_ready_for_auto_rebuild(namespace_state)
        if isinstance(namespace_state, Mapping)
        else (False, "namespace_state_missing")
    )
    if not embedder_ready:
        event["skipped_reason"] = embedder_skip_reason
        event["detail"] = (
            str(namespace_state.get("detail") or "").strip()
            if isinstance(namespace_state, Mapping)
            else ""
        )
        return event

    started = _start_background_workflow_capability_index_build(
        force_refresh=False,
        mode="background",
    )
    event["started"] = bool(started)
    if started:
        event["detail"] = "Background workflow capability index initialisation started."
    else:
        event["skipped_reason"] = "background_start_refused"
    return event


def _warm_workflow_capability_query_surface(
    index: "WorkflowCapabilityIndex",
    *,
    timeout_seconds: float | None = None,
    mode: str = "warm",
) -> None:
    warm_started_at = time.perf_counter()
    warm_failures: list[str] = []
    warm_match_count = 0
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
            warm_matches = index.search(warm_query, max_results=1)
        except Exception as warm_exc:
            warm_failures.append(str(warm_exc))
            continue
        if warm_matches:
            warm_match_count += 1
        else:
            warm_failures.append(
                "workflow capability query surface returned no warm-up match "
                f"for query {warm_query!r}"
            )
    if warm_match_count <= 0:
        joined = "; ".join(warm_failures)
        _set_workflow_capability_query_surface_state(ready=False, error=joined)
        logger.warning("workflow_capability_index_warm_query_failed: %s", joined)
        raise RuntimeError(joined)
    if warm_failures:
        logger.info(
            "[workflow_capability_index] %s query surface warmed with %d "
            "matched warm quer%s; non-blocking warm misses: %s",
            mode,
            warm_match_count,
            "y" if warm_match_count == 1 else "ies",
            "; ".join(warm_failures),
        )

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


def get_workflow_capability_index_runtime_state(
    *,
    latency_sensitive: bool = False,
) -> Dict[str, Any]:
    """Return lightweight runtime state for capability-index diagnostics."""

    index = get_workflow_capability_index()
    if latency_sensitive:
        with _INDEX_STATE_LOCK:
            query_surface_ready = bool(
                _INDEX_REBUILD_STATE.get("query_surface_ready", False)
            )
            build_in_progress = bool(
                _INDEX_REBUILD_STATE.get("build_in_progress", False)
            )
            return {
                "surface": "workflow_retrieval",
                "backend": "llamaindex",
                "namespace": WORKFLOW_CAPABILITY_NAMESPACE,
                "size": int(index.size),
                "ready": bool(
                    index.size > 0 and query_surface_ready and not build_in_progress
                ),
                "build_in_progress": build_in_progress,
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
                "last_manifest_status": _INDEX_REBUILD_STATE.get(
                    "last_manifest_status"
                ),
                "last_manifest_detail": _INDEX_REBUILD_STATE.get(
                    "last_manifest_detail"
                ),
                "last_manifest_path": _INDEX_REBUILD_STATE.get("last_manifest_path"),
                "last_manifest_digest": _INDEX_REBUILD_STATE.get(
                    "last_manifest_digest"
                ),
                "last_manifest_checked_at_utc": _INDEX_REBUILD_STATE.get(
                    "last_manifest_checked_at_utc"
                ),
                "auto_rebuild": _snapshot_workflow_capability_auto_rebuild_state_locked(),
                "namespace_state": None,
                "latency_sensitive": True,
            }
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
            "ready": bool(
                index.size > 0 and namespace_compatible and query_surface_ready
            ),
            "build_in_progress": bool(
                _INDEX_REBUILD_STATE.get("build_in_progress", False)
            ),
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
            "last_manifest_status": _INDEX_REBUILD_STATE.get("last_manifest_status"),
            "last_manifest_detail": _INDEX_REBUILD_STATE.get("last_manifest_detail"),
            "last_manifest_path": _INDEX_REBUILD_STATE.get("last_manifest_path"),
            "last_manifest_digest": _INDEX_REBUILD_STATE.get("last_manifest_digest"),
            "last_manifest_checked_at_utc": _INDEX_REBUILD_STATE.get(
                "last_manifest_checked_at_utc"
            ),
            "auto_rebuild": _snapshot_workflow_capability_auto_rebuild_state_locked(),
            "namespace_state": namespace_state,
        }


def get_workflow_capability_index_readiness_report() -> Dict[str, Any]:
    """Return a user-facing readiness report for the capability index."""

    runtime_state = get_workflow_capability_index_runtime_state()
    background_initialisation_event = (
        _maybe_start_workflow_capability_index_background_initialisation(runtime_state)
    )
    if background_initialisation_event.get("started"):
        runtime_state = get_workflow_capability_index_runtime_state()
    auto_rebuild_event = _maybe_start_workflow_capability_index_auto_rebuild(
        runtime_state
    )
    if auto_rebuild_event.get("started"):
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
    auto_rebuild_state = _snapshot_workflow_capability_auto_rebuild_state()
    if auto_rebuild_event.get("checked"):
        auto_rebuild_state["checked_this_report"] = True
    if auto_rebuild_event.get("started"):
        auto_rebuild_state["started_this_report"] = True
    if auto_rebuild_event.get("skipped_reason"):
        auto_rebuild_state["skipped_reason_this_report"] = auto_rebuild_event.get(
            "skipped_reason"
        )

    if ready:
        status = "ready"
        warning_level = "ok"
        summary = "Workflow capability index ready."
        detail = (
            f"The authoritative workflow capability index is available with "
            f"{size} indexed workflow entries."
        )
    elif build_in_progress:
        auto_rebuild_status = str(auto_rebuild_state.get("last_status") or "").strip()
        last_mode = str(runtime_state.get("last_mode") or "").strip()
        if last_mode == "auto_rebuild" and auto_rebuild_status in {
            "starting",
            "started",
        }:
            status = "rebuilding"
            warning_level = "warning"
            summary = "Workflow capability index rebuilding."
            detail = (
                "Von detected an incompatible persisted workflow capability "
                "index and started an automatic background rebuild."
            )
        else:
            status = "building"
            warning_level = "warning"
            summary = "Workflow capability index still building."
            detail = (
                "Workflow discovery is waiting on the authoritative capability "
                "index to finish building."
            )
    elif auto_rebuild_event.get("started"):
        status = "rebuilding"
        warning_level = "warning"
        summary = "Workflow capability index rebuilding."
        detail = (
            "Von detected an incompatible persisted workflow capability index "
            "and started an automatic background rebuild."
        )
    elif background_initialisation_event.get("started"):
        status = "building"
        warning_level = "warning"
        summary = "Workflow capability index initialising."
        detail = (
            "Von started a background load or build of the authoritative "
            "workflow capability index for this server process."
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
        detail = "No authoritative workflow capability snapshot has been built yet."

    with _INDEX_STATE_LOCK:
        startup_report = _INDEX_REBUILD_STATE.get("startup_last_report")

    ollama_auto_pull_state: Dict[str, Any] | None = None
    try:
        from ..languagemodels.llm_interface import get_ollama_auto_pull_state_snapshot

        ollama_auto_pull_state = get_ollama_auto_pull_state_snapshot()
    except Exception:
        ollama_auto_pull_state = None

    report: Dict[str, Any] = {
        **runtime_state,
        "status": status,
        "warning_level": warning_level,
        "workflow_discovery_available": bool(ready),
        "user_visible_blocker": not bool(ready),
        "user_visible_severity": "ok" if ready else "error",
        "footer_red_flag": not bool(ready),
        "summary": summary,
        "detail": detail,
        "background_initialisation": background_initialisation_event,
        "auto_rebuild": auto_rebuild_state,
        "checked_at_utc": _utc_now_iso(),
    }
    if isinstance(ollama_auto_pull_state, dict):
        report["ollama_model_auto_pull"] = ollama_auto_pull_state
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
        loaded_from_manifest = False
        load_from_persisted = getattr(
            index,
            "load_from_persisted_namespace_if_current",
            None,
        )
        agent_test_memory_only = _is_agent_test_instance()
        if agent_test_memory_only:
            count = index.load_entries_from_registry_without_rag_sync(
                registry,
                mode=mode,
            )
        elif not force_refresh and callable(load_from_persisted):
            loaded_from_manifest = bool(load_from_persisted(registry))

        if not agent_test_memory_only:
            if loaded_from_manifest:
                count = index.size
            else:
                try:
                    count = index.index_from_registry(registry, mode=mode)
                except TypeError:
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
        if mode == "auto_rebuild":
            _record_workflow_capability_auto_rebuild_state(
                status="succeeded",
                skipped_reason=None,
                detail=f"Automatic rebuild completed with {count} entries.",
                mark_finished=True,
            )
        logger.info(
            "[workflow_capability_index] %s %s completed with %d entries.",
            mode,
            (
                "agent-test memory load"
                if agent_test_memory_only
                else "manifest load"
                if loaded_from_manifest
                else "build"
            ),
            count,
        )
        return index
    except Exception as exc:
        _set_workflow_capability_rebuild_state(
            build_in_progress=False,
            mode=mode,
            error=str(exc),
        )
        if mode == "auto_rebuild":
            _record_workflow_capability_auto_rebuild_state(
                status="failed",
                skipped_reason=None,
                detail=str(exc),
                mark_finished=True,
            )
        raise
    finally:
        _INDEX_REBUILD_COMPLETED.set()


def _start_background_workflow_capability_index_build(
    *,
    force_refresh: bool = False,
    workflow_registry: Any | None = None,
    mode: str = "background",
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
        _INDEX_REBUILD_STATE["last_mode"] = mode
        _INDEX_REBUILD_STATE["last_error"] = None
        _INDEX_REBUILD_STATE["last_attempt_monotonic"] = now
        _INDEX_REBUILD_COMPLETED.clear()

    def _worker() -> None:
        try:
            with _INDEX_REBUILD_LOCK:
                _perform_workflow_capability_index_build(
                    force_refresh=force_refresh,
                    mode=mode,
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
            mode=mode,
            error=f"thread_start_failed:{exc}",
        )
        if mode == "auto_rebuild":
            _record_workflow_capability_auto_rebuild_state(
                status="failed",
                skipped_reason=None,
                detail=f"thread_start_failed:{exc}",
                mark_finished=True,
            )
        _INDEX_REBUILD_COMPLETED.set()
        logger.warning(
            "workflow_capability_index_background_thread_start_failed: %s", exc
        )
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
    if not block:
        with _INDEX_STATE_LOCK:
            ready = bool(
                index.size > 0
                and _INDEX_REBUILD_STATE.get("query_surface_ready", False)
                and not _INDEX_REBUILD_STATE.get("build_in_progress", False)
            )
        if ready and not force_refresh:
            return index

        _start_background_workflow_capability_index_build(
            force_refresh=force_refresh,
            workflow_registry=workflow_registry,
        )
        wait_seconds = max(0.0, float(max_wait_seconds or 0.0))
        if wait_seconds > 0.0:
            _INDEX_REBUILD_COMPLETED.wait(wait_seconds)
        return get_workflow_capability_index()

    runtime_state = get_workflow_capability_index_runtime_state()
    if index.size > 0 and bool(runtime_state.get("ready", False)) and not force_refresh:
        return index

    if bool(
        get_workflow_capability_index_runtime_state().get("build_in_progress", False)
    ):
        wait_seconds = (
            None if max_wait_seconds is None else max(0.0, float(max_wait_seconds))
        )
        _INDEX_REBUILD_COMPLETED.wait(wait_seconds)
        index = get_workflow_capability_index()
        runtime_state = get_workflow_capability_index_runtime_state()
        if (
            index.size > 0
            and bool(runtime_state.get("ready", False))
            and not force_refresh
        ):
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
        if (
            index.size > 0
            and bool(runtime_state.get("ready", False))
            and not force_refresh
        ):
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
    if non_blocking:
        with _INDEX_STATE_LOCK:
            ready = bool(
                index.size > 0
                and _INDEX_REBUILD_STATE.get("query_surface_ready", False)
                and not _INDEX_REBUILD_STATE.get("build_in_progress", False)
            )
        if not ready:
            return []
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


def resolve_workflow_capabilities_for_contract(
    *,
    required_tools: Sequence[Any] | None = None,
    required_actions: Sequence[Any] | None = None,
    max_results: int = 10,
    workflow_registry: Any | None = None,
    exclude_ids: set[str] | None = None,
    allow_registry_projection: bool = False,
) -> List[WorkflowCapabilityMatch]:
    """Resolve workflows whose represented capability metadata fits a turn contract.

    This is a structural support surface for cold capability-index moments.  It
    compares represented turn-contract tool/action requirements against the same
    compact routing projection used by the capability index; it does not inspect
    prompt prose or encode domain-specific routing choices.
    """

    contract_tools = _normalise_contract_symbol_set(required_tools)
    contract_actions = _normalise_contract_symbol_set(required_actions)
    if not contract_tools and not contract_actions:
        return []

    index = get_workflow_capability_index()
    with index._lock:
        entries = dict(index._entries)

    entry_source = "process_capability_entries"
    if not entries and workflow_registry is not None and allow_registry_projection:
        try:
            entries, _diagnostics = index._entries_from_registry(workflow_registry)
            entry_source = "registry_routing_projection"
        except Exception as exc:
            logger.warning("workflow_contract_capability_projection_failed: %s", exc)
            return []
    elif not entries:
        return []

    excluded = {str(item).strip() for item in (exclude_ids or set()) if str(item).strip()}
    scored_rows: list[tuple[float, str, _CapabilityEntry, dict[str, Any]]] = []
    for workflow_id, entry in entries.items():
        if workflow_id in excluded:
            continue
        scored = _score_contract_capability_entry(
            entry,
            contract_tools=contract_tools,
            contract_actions=contract_actions,
        )
        if scored is None:
            continue
        score, contract_match = scored
        contract_match["entry_source"] = entry_source
        scored_rows.append((score, workflow_id, entry, contract_match))

    scored_rows.sort(
        key=lambda row: (
            -row[0],
            -len(row[3].get("tool_overlap") or []),
            -len(row[3].get("action_overlap") or []),
            -len(row[3].get("tool_surface_family_overlap") or []),
            row[1],
        )
    )

    results: List[WorkflowCapabilityMatch] = []
    for score, workflow_id, entry, contract_match in scored_rows[
        : max(1, int(max_results or 1))
    ]:
        metadata = dict(entry.metadata)
        metadata["contract_capability_match"] = contract_match
        results.append(
            WorkflowCapabilityMatch(
                workflow_id=workflow_id,
                name=str(metadata.get("name") or _workflow_id_to_name(workflow_id)),
                description=_build_workflow_capability_result_description(entry),
                relevance_score=score,
                source="contract_capability_metadata",
                metadata=metadata,
            )
        )
    return results


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
        _INDEX_REBUILD_STATE["last_manifest_status"] = None
        _INDEX_REBUILD_STATE["last_manifest_detail"] = None
        _INDEX_REBUILD_STATE["last_manifest_path"] = None
        _INDEX_REBUILD_STATE["last_manifest_digest"] = None
        _INDEX_REBUILD_STATE["last_manifest_checked_at_utc"] = None
        _INDEX_REBUILD_STATE["auto_rebuild_attempt_count"] = 0
        _INDEX_REBUILD_STATE["auto_rebuild_last_attempt_monotonic"] = 0.0
        _INDEX_REBUILD_STATE["auto_rebuild_last_attempt_at_utc"] = None
        _INDEX_REBUILD_STATE["auto_rebuild_last_started_at_utc"] = None
        _INDEX_REBUILD_STATE["auto_rebuild_last_finished_at_utc"] = None
        _INDEX_REBUILD_STATE["auto_rebuild_last_status"] = None
        _INDEX_REBUILD_STATE["auto_rebuild_last_skipped_reason"] = None
        _INDEX_REBUILD_STATE["auto_rebuild_last_detail"] = None
    _INDEX_REBUILD_COMPLETED.set()


def invalidate_workflow_capability_index(
    *,
    reason: str | None = None,
    reset_backend_namespace: bool = False,
) -> dict[str, Any]:
    """Drop the cached capability index so the next lookup rebuilds it."""

    previous_state = get_workflow_capability_index_runtime_state(
        latency_sensitive=not reset_backend_namespace
    )
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
