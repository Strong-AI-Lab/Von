"""Turn-scoped memoisation for workflow discovery.

Workflow discovery is a support surface over Vontology/workflow authority.  This
module only remembers the result of an already-authorised discovery query within
the same turn and authority-version fingerprint; it does not rank, filter, or
select workflows.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional

_CACHE_SCHEMA_VERSION = "turn_workflow_discovery_memo.v1"
_DEFAULT_TTL_SECONDS = 900.0
_DEFAULT_MAX_ENTRIES = 256
_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, _MemoEntry]" = OrderedDict()


@dataclass(frozen=True)
class _MemoEntry:
    created_monotonic: float
    payload: dict[str, Any]
    uncached_elapsed_ms: float
    candidate_count: int
    capability_index_version: str


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)
    return parsed if parsed > 0.0 else float(default)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def clear_turn_workflow_discovery_memo() -> None:
    """Clear process-local turn workflow discovery memo entries."""

    with _CACHE_LOCK:
        _CACHE.clear()


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_for_digest(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_for_digest(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalise_for_digest(item) for item in value]
    return str(value)


def _digest_payload(value: Any) -> str:
    normalised = _normalise_for_digest(value)
    payload = json.dumps(
        normalised,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _capability_index_version() -> str:
    try:
        from .workflow_capability_service import (
            get_workflow_capability_index_runtime_state,
        )

        state = get_workflow_capability_index_runtime_state(latency_sensitive=True)
    except Exception:
        state = {}

    if not isinstance(state, Mapping):
        state = {}
    fields = {
        "schema_version": "workflow_capability_index_version.v1",
        "surface": state.get("surface"),
        "namespace": state.get("namespace"),
        "size": state.get("size"),
        "ready": state.get("ready"),
        "last_manifest_digest": state.get("last_manifest_digest"),
        "last_success_monotonic": state.get("last_success_monotonic"),
        "last_invalidated_at_utc": state.get("last_invalidated_at_utc"),
        "last_mode": state.get("last_mode"),
    }
    return _digest_payload(fields)


def _registry_fingerprint(workflow_registry: Any | None) -> str | None:
    if workflow_registry is None:
        return None
    # The registry object is a support-surface dependency.  Include identity so
    # tests and isolated agent backends do not share entries across registries.
    return f"{type(workflow_registry).__name__}:{id(workflow_registry)}"


def _candidate_count(payload: Mapping[str, Any]) -> int:
    for key in ("candidate_count", "match_count"):
        raw_value = payload.get(key)
        if raw_value is None:
            continue
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    for key in ("matches", "routing_matches", "candidates"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _discovery_ranking_input(
    *,
    user_input: str,
    requested_query: str | None,
) -> str:
    """Choose the text used for retrieval ranking."""

    return _safe_text(user_input)


def _build_cache_key_payload(
    *,
    turn_scope: str | None,
    namespace: str | None,
    user_input: str,
    requested_query: str | None,
    expected_outcome_contract: Mapping[str, Any] | None,
    relevance_threshold: float,
    max_results: int,
    timeout_seconds: float | str | None,
    allow_non_executable: Optional[bool],
    workflow_registry: Any | None,
    capability_index_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "turn_scope": _safe_text(turn_scope),
        "namespace": _safe_text(namespace),
        "user_input_digest": _digest_payload(_safe_text(user_input)),
        "requested_query_digest": _digest_payload(_safe_text(requested_query)),
        "expected_outcome_contract_digest": _digest_payload(
            expected_outcome_contract or {}
        ),
        "relevance_threshold": float(relevance_threshold),
        "max_results": int(max_results),
        "timeout_seconds": _safe_text(timeout_seconds),
        "allow_non_executable": allow_non_executable,
        "workflow_registry": _registry_fingerprint(workflow_registry),
        "capability_index_version": capability_index_version,
    }


def _annotate_payload(
    payload: Mapping[str, Any],
    *,
    cache_hit: bool,
    cache_key_digest: str,
    turn_scope: str | None,
    candidate_count: int,
    lookup_elapsed_ms: float,
    capability_index_version: str,
    uncached_elapsed_ms: float | None = None,
) -> dict[str, Any]:
    annotated = copy.deepcopy(dict(payload))
    prior_origin = annotated.get("discovery_payload_origin")
    if isinstance(prior_origin, str) and prior_origin.strip():
        annotated.setdefault("discovery_payload_origin_prior", prior_origin)
    annotated["discovery_payload_origin"] = (
        "turn_scoped_workflow_discovery_memo_hit"
        if cache_hit
        else "turn_scoped_workflow_discovery_memo_miss"
    )
    telemetry = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "cache_hit": bool(cache_hit),
        "cache_key_digest": cache_key_digest,
        "turn_scope": _safe_text(turn_scope) or None,
        "candidate_count": int(candidate_count),
        "elapsed_ms": round(max(0.0, lookup_elapsed_ms), 3),
        "capability_index_version": capability_index_version,
    }
    if uncached_elapsed_ms is not None:
        telemetry["uncached_elapsed_ms"] = round(max(0.0, uncached_elapsed_ms), 3)
        if cache_hit:
            telemetry["saved_elapsed_ms"] = round(
                max(0.0, uncached_elapsed_ms - lookup_elapsed_ms), 3
            )
    annotated["workflow_discovery_cache"] = telemetry
    return annotated


def _prune_locked(now: float, *, ttl_seconds: float, max_entries: int) -> None:
    expired_keys = [
        key
        for key, entry in _CACHE.items()
        if now - entry.created_monotonic > ttl_seconds
    ]
    for key in expired_keys:
        _CACHE.pop(key, None)
    while len(_CACHE) > max_entries:
        _CACHE.popitem(last=False)


def discover_workflows_for_turn_memoized(
    user_input: str,
    *,
    namespace: Optional[str] = None,
    turn_scope: str | None = None,
    requested_query: str | None = None,
    expected_outcome_contract: Mapping[str, Any] | None = None,
    relevance_threshold: float = 0.70,
    max_results: int = 3,
    timeout_seconds: float | str | None = None,
    allow_non_executable: Optional[bool] = None,
    workflow_registry: Any | None = None,
    discovery_func: Callable[..., Optional[dict[str, Any]]] | None = None,
) -> Optional[dict[str, Any]]:
    """Run workflow discovery with same-turn memoisation and telemetry."""

    if not user_input or len(str(user_input).strip()) < 5:
        return None

    capability_version = _capability_index_version()
    key_payload = _build_cache_key_payload(
        turn_scope=turn_scope,
        namespace=namespace,
        user_input=user_input,
        requested_query=requested_query,
        expected_outcome_contract=expected_outcome_contract,
        relevance_threshold=relevance_threshold,
        max_results=max_results,
        timeout_seconds=timeout_seconds,
        allow_non_executable=allow_non_executable,
        workflow_registry=workflow_registry,
        capability_index_version=capability_version,
    )
    cache_key_digest = _digest_payload(key_payload)
    ttl_seconds = _positive_float_env(
        "VON_TURN_WORKFLOW_DISCOVERY_MEMO_TTL_SECONDS",
        _DEFAULT_TTL_SECONDS,
    )
    max_entries = _positive_int_env(
        "VON_TURN_WORKFLOW_DISCOVERY_MEMO_MAX_ENTRIES",
        _DEFAULT_MAX_ENTRIES,
    )
    lookup_started = time.perf_counter()
    now = time.perf_counter()
    with _CACHE_LOCK:
        _prune_locked(now, ttl_seconds=ttl_seconds, max_entries=max_entries)
        entry = _CACHE.get(cache_key_digest)
        if entry is not None:
            _CACHE.move_to_end(cache_key_digest)
            lookup_elapsed_ms = (time.perf_counter() - lookup_started) * 1000.0
            return _annotate_payload(
                entry.payload,
                cache_hit=True,
                cache_key_digest=cache_key_digest,
                turn_scope=turn_scope,
                candidate_count=entry.candidate_count,
                lookup_elapsed_ms=lookup_elapsed_ms,
                capability_index_version=entry.capability_index_version,
                uncached_elapsed_ms=entry.uncached_elapsed_ms,
            )

    if discovery_func is None:
        from . import workflow_discovery_service

        discovery_func = workflow_discovery_service.discover_workflows_for_turn

    discovery_input = _discovery_ranking_input(
        user_input=str(user_input),
        requested_query=requested_query,
    )
    uncached_started = time.perf_counter()
    raw_result = discovery_func(
        discovery_input,
        namespace=namespace,
        relevance_threshold=relevance_threshold,
        max_results=max_results,
        timeout_seconds=timeout_seconds,
        allow_non_executable=allow_non_executable,
        workflow_registry=workflow_registry,
        expected_outcome_contract=(
            expected_outcome_contract
            if isinstance(expected_outcome_contract, Mapping)
            else None
        ),
        requested_query=_safe_text(requested_query) or None,
    )
    uncached_elapsed_ms = (time.perf_counter() - uncached_started) * 1000.0
    if not isinstance(raw_result, Mapping):
        return raw_result

    payload = copy.deepcopy(dict(raw_result))
    clean_user_input = _safe_text(user_input)
    clean_discovery_input = _safe_text(discovery_input)
    clean_requested_query = _safe_text(requested_query)
    discovery_input_differs_from_user = bool(
        clean_user_input and clean_discovery_input != clean_user_input
    )
    discovery_input_differs_from_requested = bool(
        clean_requested_query and clean_discovery_input != clean_requested_query
    )
    if discovery_input_differs_from_user or discovery_input_differs_from_requested:
        payload["ranking_query_input"] = (
            _safe_text(payload.get("query")) or clean_discovery_input
        )
        contract_projection = payload.get("contract_projection")
        contract_fields_used = (
            contract_projection.get("fields_used")
            if isinstance(contract_projection, Mapping)
            else ()
        )
        payload["ranking_query_input_source"] = (
            "expected_outcome_contract"
            if (
                isinstance(contract_fields_used, list)
                and contract_fields_used
                or (
                    isinstance(expected_outcome_contract, Mapping)
                    and bool(expected_outcome_contract)
                    and discovery_input_differs_from_requested
                )
            )
            else "requested_query"
        )
        if clean_user_input:
            payload["query"] = clean_user_input
    candidate_count = _candidate_count(payload)
    with _CACHE_LOCK:
        _CACHE[cache_key_digest] = _MemoEntry(
            created_monotonic=time.perf_counter(),
            payload=copy.deepcopy(payload),
            uncached_elapsed_ms=uncached_elapsed_ms,
            candidate_count=candidate_count,
            capability_index_version=capability_version,
        )
        _CACHE.move_to_end(cache_key_digest)
        _prune_locked(
            time.perf_counter(),
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
        )

    lookup_elapsed_ms = (time.perf_counter() - lookup_started) * 1000.0
    return _annotate_payload(
        payload,
        cache_hit=False,
        cache_key_digest=cache_key_digest,
        turn_scope=turn_scope,
        candidate_count=candidate_count,
        lookup_elapsed_ms=lookup_elapsed_ms,
        capability_index_version=capability_version,
        uncached_elapsed_ms=uncached_elapsed_ms,
    )


__all__ = [
    "clear_turn_workflow_discovery_memo",
    "discover_workflows_for_turn_memoized",
]
