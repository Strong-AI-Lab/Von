"""Opt-in LLM response cache for fast deterministic retests (JVNAUTOSCI-2506).

Replay and test loops on local models spend most wall-clock prefilling
near-identical prompts. This cache returns previously generated responses for
exactly matching (provider, host, model, messages, options) requests so a
retest of routing/instance/probe behaviour does not pay generation cost.

Strictly test-scoped:
- Enabled only when ``VON_LLM_RESPONSE_CACHE`` is explicitly truthy, or by
  default on agent-test instances (``VON_AGENT_TEST_INSTANCE``); an explicit
  falsy value always disables, including on agent-test instances.
- Measurement runs (model A/B, latency calibration) must disable it via
  ``VON_LLM_RESPONSE_CACHE=off``.
- Every hit is logged and counted; callers can attach
  :func:`get_llm_response_cache_stats` to telemetry.

Phase 1 is exact-match only. Volatile-field normalisation (request IDs,
timestamps embedded in prompts) is deliberately out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

LLM_RESPONSE_CACHE_ENV = "VON_LLM_RESPONSE_CACHE"
_AGENT_TEST_MARKER_ENV = "VON_AGENT_TEST_INSTANCE"
_MAX_ENTRIES_ENV = "VON_LLM_RESPONSE_CACHE_MAX_ENTRIES"
_TTL_SECONDS_ENV = "VON_LLM_RESPONSE_CACHE_TTL_SECONDS"
_DEFAULT_MAX_ENTRIES = 512
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0
_MONGO_COLLECTION_NAME = "llm_response_cache"
_SCHEMA_VERSION = "llm_response_cache_entry.v1"

_TRUTHY = {"1", "true", "yes", "on", "replay"}
_FALSY = {"0", "false", "no", "off"}

_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_STATS = {"hits": 0, "misses": 0, "stores": 0, "persistent_hits": 0}


def is_llm_response_cache_enabled() -> bool:
    raw = str(os.getenv(LLM_RESPONSE_CACHE_ENV) or "").strip().lower()
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    # Default: on for agent-test instances only.
    marker = str(os.getenv(_AGENT_TEST_MARKER_ENV) or "").strip().lower()
    return marker in _TRUTHY


def _max_entries() -> int:
    try:
        return max(8, int(os.getenv(_MAX_ENTRIES_ENV, _DEFAULT_MAX_ENTRIES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ENTRIES


def _ttl_seconds() -> float:
    try:
        return max(60.0, float(os.getenv(_TTL_SECONDS_ENV, _DEFAULT_TTL_SECONDS)))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS


def build_llm_response_cache_key(
    *,
    provider: str,
    host: Optional[str],
    model: str,
    messages: Any,
    options: Optional[Mapping[str, Any]] = None,
) -> str:
    payload = {
        "provider": str(provider or "").strip().lower(),
        "host": str(host or "").strip().lower(),
        "model": str(model or "").strip(),
        "messages": messages,
        "options": dict(options) if isinstance(options, Mapping) else None,
    }
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_llm_prompt_key(
    *,
    messages: Any,
    options: Optional[Mapping[str, Any]] = None,
) -> str:
    """Model-agnostic digest of the request content.

    Entries from different models for the same prompt share this key, so A/B
    tooling can fetch all cached model responses for one prompt side by side
    (JVNAUTOSCI-2504) and reuse cached generations for already-measured arms.
    """

    payload = {
        "messages": messages,
        "options": dict(options) if isinstance(options, Mapping) else None,
    }
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached_responses_by_prompt(prompt_key: str) -> list[dict[str, Any]]:
    """Return all cached entries (across models) for a prompt key."""

    results: list[dict[str, Any]] = []
    seen_cache_keys: set[str] = set()
    with _CACHE_LOCK:
        for entry in _CACHE.values():
            if entry.get("prompt_key") == prompt_key:
                results.append(dict(entry))
                seen_cache_keys.add(str(entry.get("cache_key")))
    coll = _persistent_collection()
    if coll is not None:
        try:
            for doc in coll.find({"prompt_key": prompt_key}, {"_id": 0}):
                if str(doc.get("cache_key")) not in seen_cache_keys:
                    results.append(dict(doc))
        except Exception:
            pass
    results.sort(key=lambda item: str(item.get("model") or ""))
    return results


def _persistent_collection():
    try:
        from ..db.mongo_client import get_db

        db = get_db()
        if db is None:
            return None
        return db[_MONGO_COLLECTION_NAME]
    except Exception:
        return None


def get_cached_llm_response(cache_key: str) -> Optional[dict[str, Any]]:
    """Return a cache entry mapping with at least ``response_text``, or None."""

    if not is_llm_response_cache_enabled():
        return None
    now = time.time()
    ttl = _ttl_seconds()
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is not None:
            if now - float(entry.get("stored_at_epoch") or 0.0) <= ttl:
                _CACHE.move_to_end(cache_key)
                _STATS["hits"] += 1
                logger.info(
                    "[llm_response_cache] hit key=%s model=%s age_s=%d",
                    cache_key[:12],
                    entry.get("model"),
                    int(now - float(entry.get("stored_at_epoch") or 0.0)),
                )
                return dict(entry)
            _CACHE.pop(cache_key, None)

    coll = _persistent_collection()
    if coll is not None:
        try:
            doc = coll.find_one({"cache_key": cache_key}, {"_id": 0})
        except Exception:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("response_text"), str):
            if now - float(doc.get("stored_at_epoch") or 0.0) <= ttl:
                with _CACHE_LOCK:
                    _CACHE[cache_key] = dict(doc)
                    while len(_CACHE) > _max_entries():
                        _CACHE.popitem(last=False)
                    _STATS["hits"] += 1
                    _STATS["persistent_hits"] += 1
                logger.info(
                    "[llm_response_cache] persistent hit key=%s model=%s",
                    cache_key[:12],
                    doc.get("model"),
                )
                return dict(doc)

    with _CACHE_LOCK:
        _STATS["misses"] += 1
    return None


def store_llm_response(
    cache_key: str,
    *,
    provider: str,
    host: Optional[str],
    model: str,
    response_text: str,
    duration_ms: Optional[float] = None,
    prompt_key: Optional[str] = None,
) -> None:
    if not is_llm_response_cache_enabled():
        return
    if not isinstance(response_text, str) or not response_text.strip():
        return
    entry = {
        "schema_version": _SCHEMA_VERSION,
        "cache_key": cache_key,
        "prompt_key": str(prompt_key or "").strip() or None,
        "provider": str(provider or "").strip().lower(),
        "host": str(host or "").strip().lower() or None,
        "model": str(model or "").strip(),
        "response_text": response_text,
        "original_duration_ms": (
            float(duration_ms) if isinstance(duration_ms, (int, float)) else None
        ),
        "stored_at_epoch": time.time(),
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = dict(entry)
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > _max_entries():
            _CACHE.popitem(last=False)
        _STATS["stores"] += 1

    coll = _persistent_collection()
    if coll is not None:
        try:
            coll.update_one(
                {"cache_key": cache_key},
                {"$set": entry},
                upsert=True,
            )
        except Exception as exc:
            logger.debug("[llm_response_cache] persistent store failed: %s", exc)


def get_llm_response_cache_stats() -> dict[str, Any]:
    with _CACHE_LOCK:
        return {
            **dict(_STATS),
            "entries_in_memory": len(_CACHE),
            "enabled": is_llm_response_cache_enabled(),
        }


def clear_llm_response_cache(*, include_persistent: bool = False) -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        for key in _STATS:
            _STATS[key] = 0
    if include_persistent:
        coll = _persistent_collection()
        if coll is not None:
            try:
                coll.delete_many({})
            except Exception:
                pass


__all__ = [
    "LLM_RESPONSE_CACHE_ENV",
    "build_llm_prompt_key",
    "build_llm_response_cache_key",
    "clear_llm_response_cache",
    "get_cached_llm_response",
    "get_cached_responses_by_prompt",
    "get_llm_response_cache_stats",
    "is_llm_response_cache_enabled",
    "store_llm_response",
]
