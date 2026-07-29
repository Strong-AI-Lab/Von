"""Semantic retrieval for registered ordinary-turn tool capabilities.

The ordinary-turn selector already retrieves represented workflows through the
workflow capability index.  Registered tools need an equivalent semantic
surface so both plan shapes can be ranked on the same model-visible frontier.

This index contains only tool interface metadata (name, description, planner
hint, and declared evidence surface).  Delegation remains authoritative in the
adaptive-turn gateway: retrieval results are always intersected with the
already delegated candidate set and can neither expose nor invoke an
undelegated capability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.backend.services.rag_service import build_rag_retrieval_state

logger = logging.getLogger(__name__)

REGISTERED_TOOL_CAPABILITY_NAMESPACE = "registered_tool_capabilities_v1"
REGISTERED_TOOL_CAPABILITY_RETRIEVAL_SCHEMA_VERSION = (
    "registered_tool_capability_retrieval.v1"
)
REGISTERED_TOOL_CAPABILITY_INDEX_SCHEMA_VERSION = "registered_tool_capability_index.v1"
_DOCUMENT_TYPE = "registered_tool_capability"
_MANIFEST_FILENAME = "registered_tool_capability_manifest.json"
_RETRIEVAL_LIMIT = 30

_index_lock = threading.Lock()
_indexed_digest: str | None = None
_runtime_state: dict[str, Any] = {
    "schema_version": REGISTERED_TOOL_CAPABILITY_INDEX_SCHEMA_VERSION,
    "status": "not_initialised",
    "ready": False,
}
_prewarm_thread: threading.Thread | None = None


def _get_capability_rag_service() -> Any:
    from src.backend.services.rag_service import get_rag_service

    service = get_rag_service("llamaindex")
    if type(service).__name__ != "LlamaIndexRAGService":
        raise RuntimeError(
            "registered tool capability retrieval requires the llamaindex backend"
        )
    return service


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _capability_document(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    name = _clean_text(candidate.get("name"))
    if not name:
        return None
    description = _clean_text(candidate.get("description"))
    planner_hint = _clean_text(candidate.get("planner_hint"))
    surface_family = _clean_text(
        candidate.get("evidence_surface_family") or candidate.get("surface_family")
    )
    # Put the short, planner-authored semantic description first and avoid
    # repeated boilerplate shared by every document; repeated "direct
    # capability" prose dilutes rather than improves embedding discrimination.
    text_parts = [
        f"Capability: {name.replace('_', ' ')}.",
        planner_hint,
        description,
        f"Evidence surface: {surface_family}." if surface_family else "",
    ]
    text = "\n".join(part for part in text_parts if part)
    return {
        "id": f"registered_tool_capability:{name}",
        "text": text,
        "metadata": {
            "type": _DOCUMENT_TYPE,
            "capability_name": name,
            "kind": "registered_tool",
        },
    }


def _normalised_documents(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        document = _capability_document(candidate)
        if document is None:
            continue
        documents[str(document["id"])] = document
    return [documents[key] for key in sorted(documents)]


def _document_digest(documents: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        list(documents),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manifest_path(rag_service: Any) -> Path | None:
    namespace_dir_builder = getattr(rag_service, "_namespace_persist_dir", None)
    if not callable(namespace_dir_builder):
        return None
    try:
        namespace_dir = Path(
            str(namespace_dir_builder(REGISTERED_TOOL_CAPABILITY_NAMESPACE))
        ).resolve()
    except Exception:  # noqa: BLE001
        return None
    return namespace_dir / _MANIFEST_FILENAME


def _read_manifest(rag_service: Any) -> dict[str, Any] | None:
    path = _manifest_path(rag_service)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _write_manifest(
    rag_service: Any,
    *,
    digest: str,
    document_count: int,
) -> None:
    path = _manifest_path(rag_service)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": REGISTERED_TOOL_CAPABILITY_INDEX_SCHEMA_VERSION,
        "namespace": REGISTERED_TOOL_CAPABILITY_NAMESPACE,
        "digest": digest,
        "document_count": int(document_count),
    }
    handle, temporary_path = tempfile.mkstemp(
        prefix=f".{_MANIFEST_FILENAME}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _namespace_is_compatible(rag_service: Any) -> bool:
    get_state = getattr(rag_service, "get_namespace_runtime_state", None)
    if not callable(get_state):
        return False
    try:
        state = get_state(REGISTERED_TOOL_CAPABILITY_NAMESPACE)
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(state, Mapping) and state.get("compatible") is True)


def _set_runtime_state(**values: Any) -> None:
    global _runtime_state
    _runtime_state = {
        "schema_version": REGISTERED_TOOL_CAPABILITY_INDEX_SCHEMA_VERSION,
        **values,
    }


def ensure_registered_tool_capability_index(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Ensure the persisted semantic index matches current tool metadata."""

    global _indexed_digest

    documents = _normalised_documents(candidates)
    digest = _document_digest(documents)
    if _indexed_digest == digest:
        return get_registered_tool_capability_index_state()
    current_state = get_registered_tool_capability_index_state()
    if (
        current_state.get("status") == "building"
        and current_state.get("digest") == digest
    ):
        # Startup prewarming must not turn a first-turn retrieval into a wait
        # for the embedding/index build.  The caller receives a typed,
        # non-authoritative unavailable state and can fall back to the bounded
        # catalogue until the same build becomes ready.
        return current_state

    with _index_lock:
        if _indexed_digest == digest:
            return get_registered_tool_capability_index_state()
        _set_runtime_state(
            status="building",
            ready=False,
            digest=digest,
            document_count=len(documents),
        )
        try:
            rag_service = _get_capability_rag_service()
            manifest = _read_manifest(rag_service)
            if (
                isinstance(manifest, Mapping)
                and manifest.get("digest") == digest
                and _namespace_is_compatible(rag_service)
            ):
                _indexed_digest = digest
                _set_runtime_state(
                    status="ready",
                    ready=True,
                    source="compatible_persisted_index",
                    digest=digest,
                    document_count=len(documents),
                )
                return get_registered_tool_capability_index_state()

            rag_service.reset_namespace(REGISTERED_TOOL_CAPABILITY_NAMESPACE)
            success_count, failure_count = rag_service.upsert_documents(
                documents,
                namespace=REGISTERED_TOOL_CAPABILITY_NAMESPACE,
                allow_partial_failures=False,
            )
            if failure_count or success_count != len(documents):
                raise RuntimeError(
                    "registered tool capability retrieval sync failed "
                    f"(success={success_count}, failed={failure_count}, "
                    f"expected={len(documents)})"
                )
            _write_manifest(
                rag_service,
                digest=digest,
                document_count=len(documents),
            )
            _indexed_digest = digest
            _set_runtime_state(
                status="ready",
                ready=True,
                source="rebuilt",
                digest=digest,
                document_count=len(documents),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "registered_tool_capability_index_build_failed: %s",
                exc,
            )
            _set_runtime_state(
                status="unavailable",
                ready=False,
                digest=digest,
                document_count=len(documents),
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
    return get_registered_tool_capability_index_state()


def retrieve_registered_tool_capability_scores(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    max_results: int = _RETRIEVAL_LIMIT,
    gateway: Any | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return semantic scores only for the supplied delegated candidates."""

    clean_query = _clean_text(query)
    allowed_names = {
        name
        for candidate in candidates
        for name in [_clean_text(candidate.get("name"))]
        if name
    }
    if not clean_query or not allowed_names:
        return {}, {
            "schema_version": REGISTERED_TOOL_CAPABILITY_RETRIEVAL_SCHEMA_VERSION,
            "status": "not_requested",
            "result_count": 0,
        }

    index_candidates = (
        _gateway_capability_candidates(gateway) if gateway is not None else candidates
    )
    index_state = ensure_registered_tool_capability_index(index_candidates)
    if index_state.get("ready") is not True:
        return {}, {
            "schema_version": REGISTERED_TOOL_CAPABILITY_RETRIEVAL_SCHEMA_VERSION,
            "status": "unavailable",
            "result_count": 0,
            "index": index_state,
            "retrieval_state": build_rag_retrieval_state(
                "unavailable",
                result_count=0,
                cause="registered_tool_capability_index_unavailable",
            ),
        }

    requested_results = max(1, min(50, int(max_results)))
    try:
        rag_service = _get_capability_rag_service()
        rag_results = rag_service.query(
            query_text=clean_query,
            top_k=requested_results,
            namespace=REGISTERED_TOOL_CAPABILITY_NAMESPACE,
            hybrid=True,
            permissions_context={
                "type": _DOCUMENT_TYPE,
                "retrieval_candidate_limit": max(
                    requested_results,
                    min(200, len(allowed_names)),
                ),
            },
        )
        retrieval_state = getattr(rag_results, "retrieval_state", None)
        scores: dict[str, float] = {}
        for result in rag_results:
            if not isinstance(result, Mapping):
                continue
            metadata = result.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            name = _clean_text(metadata.get("capability_name"))
            if name not in allowed_names:
                continue
            try:
                score = max(0.0, min(1.0, float(result.get("score") or 0.0)))
            except (TypeError, ValueError):
                continue
            scores[name] = max(score, scores.get(name, 0.0))
        return scores, {
            "schema_version": REGISTERED_TOOL_CAPABILITY_RETRIEVAL_SCHEMA_VERSION,
            "status": "results_available" if scores else "valid_empty",
            "result_count": len(scores),
            "requested_result_count": requested_results,
            "index": index_state,
            **(
                {"retrieval_state": dict(retrieval_state)}
                if isinstance(retrieval_state, Mapping)
                else {}
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("registered_tool_capability_retrieval_failed: %s", exc)
        return {}, {
            "schema_version": REGISTERED_TOOL_CAPABILITY_RETRIEVAL_SCHEMA_VERSION,
            "status": "degraded",
            "result_count": 0,
            "index": index_state,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "retrieval_state": build_rag_retrieval_state(
                "degraded",
                result_count=0,
                cause=f"query_failed:{type(exc).__name__}",
            ),
        }


def get_registered_tool_capability_index_state() -> dict[str, Any]:
    return dict(_runtime_state)


def reset_registered_tool_capability_index_state() -> None:
    """Reset process-local state; intended for tests and explicit rebuilds."""

    global _indexed_digest, _prewarm_thread
    with _index_lock:
        _indexed_digest = None
        _prewarm_thread = None
        _set_runtime_state(status="not_initialised", ready=False)


def _gateway_capability_candidates(gateway: Any) -> list[dict[str, Any]]:
    from src.backend.services.tool_metadata_service import (
        get_tool_description,
        get_tool_dispatch_surface_metadata,
        get_tool_planner_hint,
    )

    candidates: list[dict[str, Any]] = []
    describe_methods = getattr(gateway, "describe_methods", None)
    described_methods = describe_methods() if callable(describe_methods) else {}
    names = (
        sorted(str(name) for name in described_methods)
        if isinstance(described_methods, Mapping)
        else []
    )
    for name in names:
        definition = gateway.get_method_definition(name)
        if definition is None:
            continue
        fallback_description = (
            _clean_text(definition.description)
            or _clean_text(getattr(definition.input_schema, "description", None))
            or f"Use {name}."
        )
        description = (
            get_tool_description(
                name,
                fallback_description=fallback_description,
            )
            or fallback_description
        )
        surface = get_tool_dispatch_surface_metadata(name)
        surface_family = _clean_text(getattr(surface, "surface_family", None))
        evidence_surface_family = _clean_text(
            getattr(surface, "evidence_surface_family", None)
        )
        candidates.append(
            {
                "name": name,
                "description": description,
                "planner_hint": get_tool_planner_hint(name),
                "surface_family": surface_family,
                "evidence_surface_family": evidence_surface_family,
            }
        )
    return candidates


def prewarm_registered_tool_capability_index(gateway: Any) -> bool:
    """Start one best-effort background build after the gateway is available."""

    global _prewarm_thread
    with _index_lock:
        if _prewarm_thread is not None and _prewarm_thread.is_alive():
            return False

        def _run() -> None:
            try:
                ensure_registered_tool_capability_index(
                    _gateway_capability_candidates(gateway)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "registered_tool_capability_index_prewarm_failed: %s",
                    exc,
                )

        _prewarm_thread = threading.Thread(
            target=_run,
            name="registered_tool_capability_index_prewarm",
            daemon=True,
        )
        _prewarm_thread.start()
        return True
