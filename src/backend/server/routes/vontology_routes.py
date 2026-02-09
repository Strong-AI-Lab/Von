from flask import Blueprint, jsonify, request, current_app, session
import os
import tracemalloc
import json  # Add json import for file parsing
import threading
import uuid
import time
import logging
from collections import defaultdict
from typing import Any

try:
    from bson import ObjectId  # type: ignore
except Exception:  # Fallback for environments where direct import path differs
    try:
        from bson.objectid import ObjectId  # type: ignore
    except Exception:
        ObjectId = None  # type: ignore
# Import the utility functions
from ...vontology.utils_vontology import (
    get_concept_details_from_db,
    get_vontology_node_and_descendant_ids,
    update_vontology_node_description,
    get_vontology_tree,
    get_vontology_node_content,
    get_vontology_node_parents,
    create_vontology_concept,
    add_upward_closure_nodes,
    get_all_vontology_nodes_with_details,
    is_opencyc_format,
    convert_opencyc_to_von_format,
    import_ontology_nodes,
    get_concept_notes,  # Added import
    to_pascal_case,
    generate_concept_id_from_name,
    get_concept_description,
    get_concept_notes,
    get_concept_display_name_with_names_fallback,
    get_most_salient_type,  # Added import for dynamic computation
    simulate_or_delete_concept,
    record_tree_build_performance,
    record_name_normalization,
    record_frontend_tree_load,
    extract_salient_scope_lists,
    SALIENT_SCOPE_FIELD,
    is_type,
    is_predicate,
)
from ...db.repositories.concepts_repository import ConceptsRepository
from ...services.settings_service import get_setting
from ...services.concept_service import (
    get_concept_by_id,
    get_concept_by_concept_id,
    ConceptNotFoundError,
    update_concept_description,
    enrich_concept_with_text_relations,
)
from ...services.text_value_service import get_texts_for_concept
from ...services.concept_search_service import (
    search_concepts as search_concepts_service,
)
from ...security.access_control import cache_scope_key, bypass_access_control
from ...services.window_session_context_service import get_effective_context
from ...utilities.salient_recompute import recompute_salient_predicates

vontology_bp = Blueprint("vontology", __name__)

# Simple in-memory TTL cache for instance counts: { key: (timestamp, payload) }
# Key is a comma-joined sorted list of type ids. TTL is small to keep values fresh.
_INSTANCE_COUNTS_CACHE = {}
# TTL for instance counts cache; configurable via env VONTOLOGY_COUNTS_TTL (seconds)
_INSTANCE_COUNTS_CACHE_TTL_SECONDS = int(os.environ.get("VONTOLOGY_COUNTS_TTL", "30"))

# Lightweight instrumentation for entity_counts endpoint (best-effort, in-process only)
_ENTITY_COUNTS_STATS = {
    "total_calls": 0,
    "last_ts": None,
    "ema_ms": None,  # exponential moving average of handler duration
}

# Simple TTL cache for full tree
_TREE_CACHE = {  # key -> (timestamp, payload, build_secs, alloc_kb, hits)
    # 'Thing': (...)
}
_TREE_CACHE_TTL_SECONDS = int(os.environ.get("VONTOLOGY_TREE_TTL", "60"))  # default 60s
_TREE_CACHE_LOCK = threading.Lock()
"""
Simple progress cache for asynchronous tree builds.

Structure: {job_id: {total: int, processed: int, result: dict | None, error: str | None}}
"""
_TREE_BUILD_PROGRESS: dict[str, dict] = {}
_TREE_BUILD_PROGRESS_LOCK = threading.Lock()


# NOTE: tracemalloc.start() was previously called here at the module level.
# If memory tracing is required, initialize it at application startup (e.g., in the main entry point).
def _is_salient_split_enabled() -> bool:
    """Return True when type vs instance salient predicate split output is enabled."""
    flag_env = os.getenv("VON_SALIENT_SPLIT_ENABLE", "")
    if flag_env.lower() in ("1", "true", "yes", "on"):
        return True
    try:
        cfg_value = current_app.config.get("VON_SALIENT_SPLIT_ENABLE")
        if isinstance(cfg_value, bool):
            return cfg_value
        if isinstance(cfg_value, str):
            return cfg_value.lower() in ("1", "true", "yes", "on")
    except RuntimeError:
        # Outside an application context; ignore config lookup
        pass
    return False


@vontology_bp.route("/record_name_normalization", methods=["POST"])
def record_name_normalization_route():
    """Record a UI name normalization event.

    Body JSON: { "kind": "instance" | "subtype" | <other string> }
    Increments counters in system_metrics.ui_counters document.
    """
    data = request.get_json(silent=True) or {}
    kind = data.get("kind") if isinstance(data, dict) else None
    if not isinstance(kind, str) or not kind.strip():
        kind = "generic"
    kind = kind.strip().lower().replace(" ", "_")[:40]
    try:
        record_name_normalization(kind)
    except Exception:
        current_app.logger.debug(
            "Failed to record name normalization metric", exc_info=True
        )
    return jsonify({"success": True, "kind": kind}), 200


@vontology_bp.route("/record_frontend_tree_load", methods=["POST"])
def record_frontend_tree_load_route():
    """Record a frontend tree load timing sample.

    Body JSON: { load_ms: number, node_count?: number, cache_hit?: bool }
    """
    data = request.get_json(silent=True) or {}
    load_ms = data.get("load_ms")
    node_count = data.get("node_count")
    cache_hit = data.get("cache_hit")
    try:
        if load_ms is not None:
            record_frontend_tree_load(
                int(load_ms), node_count=node_count, cache_hit=cache_hit
            )
        else:
            return jsonify({"success": False, "error": "load_ms required"}), 400
    except Exception as e:
        current_app.logger.debug("Failed to record frontend tree load metric: %s", e)
        return jsonify({"success": False, "error": "metric record failed"}), 500
    return jsonify({"success": True}), 200


@vontology_bp.route("/tree", methods=["GET"])
def get_tree():
    """Endpoint to fetch the Vontology directory tree structure with TTL caching.

    Query params:
      refresh=1   Force rebuild bypassing cache
    Env vars:
      VONTOLOGY_TREE_TTL (seconds) default 60
    """
    current_app.logger.info("Received request for /api/vontology/tree")
    force_refresh = request.args.get("refresh") in ("1", "true", "True")
    now = time.time()
    cache_scope = cache_scope_key()
    cache_key = f"{cache_scope}|Thing"
    cache_record = None
    with _TREE_CACHE_LOCK:
        cache_record = _TREE_CACHE.get(cache_key)
        if cache_record:
            ts, payload, build_secs, alloc_kb, hits = cache_record
            if force_refresh or (now - ts) > _TREE_CACHE_TTL_SECONDS:
                cache_record = None  # treat as expired
    if cache_record:
        _, payload, build_secs, alloc_kb, hits = cache_record
        # Increment hit counter
        with _TREE_CACHE_LOCK:
            try:
                _TREE_CACHE[cache_key] = (
                    cache_record[0],
                    payload,
                    build_secs,
                    alloc_kb,
                    hits + 1,
                )
                hits += 1
            except Exception:
                pass
        current_app.logger.debug(
            "Tree cache hit (age=%.1fs build=%.2fs alloc=%.1fKB size_nodes=%s)",
            now - cache_record[0],
            build_secs,
            alloc_kb,
            len(payload.get("nodes", [])) if isinstance(payload, dict) else "n/a",
        )
        return jsonify(
            {
                **payload,
                "_cache": {
                    "hit": True,
                    "age_sec": now - cache_record[0],
                    "build_sec": build_secs,
                    "alloc_kb": alloc_kb,
                    "ttl_sec": _TREE_CACHE_TTL_SECONDS,
                    "hits": hits,
                },
            }
        )
    # Build fresh
    current_app.logger.debug("Tree cache miss (refresh=%s); building...", force_refresh)
    t0 = time.time()
    phase_marks = []
    phase_log_enabled = (
        os.getenv("VON_TREE_PHASE_LOG") in ("1", "true", "TRUE", "True")
    ) and (not getattr(current_app, "testing", False))

    def _mark(phase_name):
        if not phase_log_enabled:
            return
        now = time.time()
        phase_marks.append((phase_name, now))

    # Only take snapshots if tracemalloc is running
    snap_before = None
    if tracemalloc.is_tracing():
        snap_before = tracemalloc.take_snapshot()
    _mark("snapshot_before")
    try:
        tree_data = get_vontology_tree("Thing")
        if "error" in tree_data:
            current_app.logger.error(
                "Error getting Vontology tree: %s", tree_data["error"]
            )
            return jsonify({"error": tree_data["error"]}), 500
    except Exception as e:
        current_app.logger.error(
            "Unexpected error building Vontology tree: %s", e, exc_info=True
        )
        return (
            jsonify(
                {
                    "error": "Failed to retrieve Vontology structure due to an internal server error."
                }
            ),
            500,
        )
    _mark("tree_built")
    build_secs = time.time() - t0
    # Only take snapshots if tracemalloc is running
    snap_after = None
    alloc_kb = 0.0
    if tracemalloc.is_tracing() and snap_before is not None:
        snap_after = tracemalloc.take_snapshot()
        _mark("snapshot_after")
        stats = snap_after.compare_to(snap_before, "filename")
        _mark("snapshot_compared")
        alloc_kb = sum([s.size_diff for s in stats]) / 1024.0
    else:
        _mark("snapshot_after")
        stats = []
        _mark("snapshot_compared")

    # Optional verbose allocation diff logging
    try:
        if os.getenv("VON_TRACE_TREE") in ("1", "true", "TRUE", "True") and stats:
            top_n = 15
            try:
                env_n = int(os.getenv("VON_TRACE_TREE_N", "15"))
                if 1 <= env_n <= 100:
                    top_n = env_n
            except Exception:
                pass
            # Sort by absolute size diff descending
            sorted_stats = sorted(stats, key=lambda s: abs(s.size_diff), reverse=True)[
                :top_n
            ]
            for s in sorted_stats:
                if s.size_diff == 0:
                    continue
                current_app.logger.info(
                    "[tree_alloc_diff] %s size_diff=%.1fKB count_diff=%d",
                    s.traceback[0].filename if s.traceback else "unknown",
                    s.size_diff / 1024.0,
                    s.count_diff,
                )
    except Exception:
        pass
    _mark("pre_cache_store")
    with _TREE_CACHE_LOCK:
        _TREE_CACHE[cache_key] = (time.time(), tree_data, build_secs, alloc_kb, 0)
    _mark("post_cache_store")
    # Persist performance metrics (non-blocking best-effort)
    try:
        record_tree_build_performance(build_secs)
    except Exception:
        pass
    current_app.logger.info(
        "Built Vontology tree build=%.2fs alloc_diff=%.1fKB nodes=%s",
        build_secs,
        alloc_kb,
        len(tree_data.get("nodes", [])) if isinstance(tree_data, dict) else "n/a",
    )
    if phase_log_enabled:
        # Emit a compact phase timing summary
        try:
            phases_out = []
            last_t = t0
            for name, ts in phase_marks:
                phases_out.append(f"{name}={1000*(ts-last_t):.1f}ms")
                last_t = ts
            total_ms = (time.time() - t0) * 1000.0
            current_app.logger.info(
                "[tree_build_phase] %s total=%.1fms", " ".join(phases_out), total_ms
            )
        except Exception:
            pass
    return jsonify(
        {
            **tree_data,
            "_cache": {
                "hit": False,
                "build_sec": build_secs,
                "alloc_kb": alloc_kb,
                "ttl_sec": _TREE_CACHE_TTL_SECONDS,
                "hits": 0,
            },
        }
    )


@vontology_bp.route("/ensure_thing", methods=["POST"])
def ensure_thing_route():
    """Endpoint to ensure Thing root concept exists and link any orphan concepts.

    This is called automatically by the frontend when it detects an empty or incomplete ontology.
    It will:
    1. Create Thing if it doesn't exist
    2. Link any orphan concepts (types without parents) to Thing

    Returns:
        JSON with thing_created, thing_concept_id, orphans_linked, orphan_ids
    """
    current_app.logger.info("Received request for /api/vontology/ensure_thing")

    try:
        from ...vontology.utils_vontology import ensure_thing_exists_and_link_orphans

        result = ensure_thing_exists_and_link_orphans()

        if "error" in result:
            current_app.logger.error(f"Error ensuring Thing exists: {result['error']}")
            return jsonify({"success": False, "message": result["error"]}), 500

        # Invalidate tree cache since we modified the ontology structure
        with _TREE_CACHE_LOCK:
            _TREE_CACHE.clear()

        current_app.logger.info(
            f"Thing ensured: created={result['thing_created']}, orphans_linked={result['orphans_linked']}"
        )

        return (
            jsonify(
                {
                    "success": True,
                    "thing_created": result["thing_created"],
                    "thing_concept_id": result["thing_concept_id"],
                    "orphans_linked": result["orphans_linked"],
                    "orphan_ids": result["orphan_ids"],
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(
            f"Unexpected error in ensure_thing: {e}", exc_info=True
        )
        return jsonify({"success": False, "message": f"Internal error: {str(e)}"}), 500


def _build_tree_job(job_id: str) -> None:
    """Background worker that constructs the Vontology tree whilst tracking progress."""
    try:
        t0 = time.time()
        docs = list(ConceptsRepository.find({}, {"concept_id": 1}))
        total = len(docs)
        with _TREE_BUILD_PROGRESS_LOCK:
            job = _TREE_BUILD_PROGRESS.get(job_id)
            if job is not None:
                job["total"] = total
                job["processed"] = 0

        stop_event = threading.Event()

        def ticker():
            """Increment progress periodically so the client sees activity."""
            while not stop_event.is_set():
                time.sleep(0.5)
                with _TREE_BUILD_PROGRESS_LOCK:
                    j = _TREE_BUILD_PROGRESS.get(job_id)
                    if not j or j.get("result") or j.get("error"):
                        break
                    current = j.get("processed", 0)
                    j["processed"] = min(total, current + max(1, total // 20))

        t = threading.Thread(target=ticker, daemon=True)
        t.start()

        tree_data = get_vontology_tree("Thing")
        stop_event.set()
        t.join(timeout=0.1)

        with _TREE_BUILD_PROGRESS_LOCK:
            job = _TREE_BUILD_PROGRESS.get(job_id)
            if job is not None:
                job["processed"] = total
                job["result"] = tree_data

        try:
            if os.getenv("VON_TREE_PHASE_LOG") in ("1", "true", "TRUE", "True"):
                logger = None
                try:
                    logger = current_app.logger
                    if getattr(current_app, "testing", False):
                        logger = None
                except Exception:
                    pass
                if logger is None:
                    logger = logging.getLogger(__name__)
                total_ms = (time.time() - t0) * 1000.0
                node_count = "n/a"
                try:

                    def _count_nodes_list(nodes):
                        c = 0
                        for n in nodes or []:
                            c += 1
                            try:
                                c += _count_nodes_list(n.get("children", []))
                            except Exception:
                                continue
                        return c

                    if isinstance(tree_data, dict):
                        roots = tree_data.get("tree", [])
                        node_count = _count_nodes_list(roots)
                except Exception:
                    pass
                logger.info(
                    "[tree_build_async] total=%.1fms nodes=%s", total_ms, node_count
                )
        except Exception:
            pass
    except Exception as e:  # pragma: no cover
        with _TREE_BUILD_PROGRESS_LOCK:
            job = _TREE_BUILD_PROGRESS.get(job_id)
            if job is not None:
                job["error"] = str(e)


@vontology_bp.route("/tree_async", methods=["POST"])
def build_tree_async_route():
    """Initialise an asynchronous Vontology tree build."""
    job_id = uuid.uuid4().hex
    with _TREE_BUILD_PROGRESS_LOCK:
        _TREE_BUILD_PROGRESS[job_id] = {"total": 0, "processed": 0, "result": None}
    thread = threading.Thread(target=_build_tree_job, args=(job_id,), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id}), 202


@vontology_bp.route("/tree_progress/<job_id>", methods=["GET"])
def tree_build_progress(job_id: str):
    """Return progress for a previously initialised tree build."""
    with _TREE_BUILD_PROGRESS_LOCK:
        status = _TREE_BUILD_PROGRESS.get(job_id)
    if not status:
        return jsonify({"error": "unknown job"}), 404
    total = status.get("total", 0)
    processed = status.get("processed", 0)
    progress = int((processed / total) * 100) if total else 0
    response = {
        "job_id": job_id,
        "progress": progress,
        "done": bool(status.get("result") or status.get("error")),
    }
    if status.get("result") is not None:
        response["result"] = status["result"]
    if status.get("error"):
        response["error"] = status["error"]
    return jsonify(response), 200


## Canonical ontology delete endpoint: use DELETE /api/vontology/node.
## The legacy /api/vontology/delete_node route remains for backwards compatibility and is deprecated.
@vontology_bp.route("/node", methods=["DELETE"])
def unified_delete_node():
    """Unified ontology deletion endpoint.

    Query params:
      concept_id: required concept identifier (e.g. #V#person)
      simulate:   '1' (default) to simulate, '0' to execute

    Headers:
      X-Correlation-ID (optional) - supplied or auto-generated UUID hex
    """
    concept_id = request.args.get("concept_id")
    simulate_param = request.args.get("simulate", "1")
    simulate = simulate_param != "0"

    correlation_id = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex

    if not concept_id:
        resp = {
            "success": False,
            "error": "concept_id required",
            "simulate": True,
            "correlation_id": correlation_id,
        }
        r = jsonify(resp)
        r.headers["X-Correlation-ID"] = correlation_id
        return r, 400

    current_app.logger.info(
        f"[unified_delete:start] corr={correlation_id} simulate={simulate} concept={concept_id}"
    )
    result = simulate_or_delete_concept(concept_id, execute=not simulate)

    status = 200
    if not result.get("success", False):
        if "error" in result and "not found" in result["error"]:
            status = 404
        else:
            status = 400

    response_payload = {
        "success": result.get("success", False),
        "simulate": result.get("simulate", simulate),
        "concept_id": concept_id,
        "parents": result.get("parents", []),
        "children_reparented": result.get(
            "children_reparented", len(result.get("children", []))
        ),
        "children": result.get("children", []),
        "instances_retyped": result.get(
            "instances_retyped", len(result.get("instances", []))
        ),
        "instances": result.get("instances", []),
        "warnings": result.get("warnings", []),
        "protected": result.get("protected", False),
        "executed": result.get("executed", False),
        "correlation_id": correlation_id,
    }
    if "transactional" in result:
        response_payload["transactional"] = result["transactional"]
    if "error" in result:
        response_payload["error"] = result["error"]

    if response_payload.get("success") and not response_payload.get("simulate"):
        try:
            from ...vontology.utils_vontology import invalidate_vontology_caches

            affected = [concept_id] + [
                c.get("child_id") for c in response_payload["children"]
            ]
            invalidate_vontology_caches(affected, correlation_id)
        except Exception:  # pragma: no cover
            pass

    current_app.logger.info(
        f"[unified_delete] corr={correlation_id} simulate={response_payload['simulate']} success={response_payload['success']} concept={concept_id} children={response_payload['children_reparented']} instances={response_payload['instances_retyped']} warnings={len(response_payload['warnings'])}"
    )

    r = jsonify(response_payload)
    r.headers["X-Correlation-ID"] = correlation_id
    return r, status


@vontology_bp.route("/text_relations", methods=["GET"])
def list_text_relations():
    """Return all text relations (text values) for a concept.

    Query params:
      concept_id: required concept identifier (e.g. #V#person)
      limit: optional max number (default 500)
    """
    concept_id = request.args.get("concept_id")
    limit = request.args.get("limit", type=int) or 500
    if not concept_id:
        return jsonify({"error": "Missing concept_id"}), 400
    try:
        texts = get_texts_for_concept(subject_concept_id=concept_id, limit=limit)
        # Shape response: keep essential fields
        simplified = []
        for tv in texts or []:
            simplified.append(
                {
                    "id": tv.get("_id"),
                    "predicate": tv.get("predicate"),
                    "language": tv.get("language"),
                    "text": tv.get("text"),
                    "created_at": tv.get("created_at"),
                    "updated_at": tv.get("updated_at"),
                }
            )
        return jsonify(
            {
                "concept_id": concept_id,
                "count": len(simplified),
                "limit": limit,
                "text_relations": simplified,
            }
        )
    except Exception as e:  # pragma: no cover
        current_app.logger.error(
            "Failed to list text relations for %s: %s", concept_id, e, exc_info=True
        )
        return jsonify({"error": "Failed to load text relations"}), 500


@vontology_bp.route("/node_content", methods=["GET"])
def get_node_content_route():
    """Return rendered markdown + metadata for a node.

    Query params:
      identifier: concept_id (e.g. #V#person), Mongo _id hex string, or legacy path.
      raw_only=1   If present/truthy, return only an un-enriched raw view (raw_doc plus a few basics) without markdown rendering/derived fields.
    """
    identifier = request.args.get("identifier")
    raw_only = request.args.get("raw_only") in ("1", "true", "True")
    soft_missing = request.args.get("soft") in ("1", "true", "True")
    if not identifier:
        return jsonify({"error": "Missing 'identifier' parameter."}), 400
    try:
        data = get_vontology_node_content(identifier)

        # Heuristic: strip trailing punctuation accidentally attached to #V# identifiers.
        # This reduces noisy 404s when IDs appear at sentence boundaries (e.g. "#V#foo.").
        if (
            isinstance(identifier, str)
            and identifier.startswith("#V#")
            and "error" in data
        ):
            stripped = identifier.rstrip(".,:;!?)]}…")
            if stripped != identifier and stripped.startswith("#V#"):
                retry = get_vontology_node_content(stripped)
                if "error" not in retry:
                    data = retry

        # Handle not-found/errors consistently even when raw_only is requested.
        if "error" in data:
            if soft_missing:
                # Some UI flows (e.g., chat history hydration) may probe many concept IDs that
                # no longer exist. Returning 200 avoids loud 404s in the browser network log.
                return jsonify({**data, "not_found": True}), 200
            status = 404 if "not found" in data["error"].lower() else 400
            return jsonify(data), status

        concept_stats = None
        try:
            concept_id = data.get("concept_id")
            if isinstance(concept_id, str) and concept_id.startswith("#V#"):
                from ...services.vontology_concept_stats_service import (
                    get_vontology_concept_stats,
                )

                stats_payload = get_vontology_concept_stats(
                    [concept_id],
                    rebuild_if_needed=False,
                    include_stale_values=False,
                )
                concept_stats_map = (
                    stats_payload.get("concept_stats")
                    if isinstance(stats_payload, dict)
                    else None
                )
                if isinstance(concept_stats_map, dict):
                    stat = concept_stats_map.get(concept_id)
                    if isinstance(stat, dict):
                        concept_stats = stat
        except Exception:
            concept_stats = None

        if raw_only:
            # Provide a trimmed payload emphasizing raw_doc. Keep concept_id and display_name for context.
            # Avoid leaking rendered HTML/derived md_content when raw_only requested.
            trimmed = {
                "concept_id": data.get("concept_id"),
                "display_name": data.get("display_name"),
                "kind": data.get("kind"),
                "computed_kind": data.get("computed_kind"),
                "raw_doc": data.get("raw_doc"),
            }
            if isinstance(concept_stats, dict):
                trimmed["concept_stats"] = concept_stats
            # Preserve description only if it exists inside preserved_fields but not elsewhere
            if "description" in data:
                trimmed["description"] = data["description"]
            # JVNAUTOSCI-944: Enrich raw_doc.names with text relations so cartouches display proper NL names
            concept_id = data.get("concept_id")
            raw_doc = trimmed.get("raw_doc")
            if concept_id and raw_doc is not None and isinstance(raw_doc, dict):
                try:
                    names_from_relations = get_texts_for_concept(
                        subject_concept_id=concept_id, predicate="hasName", limit=100
                    )
                    enriched_names = [
                        {
                            "name": item.get("text", ""),
                            "language": item.get("lang", "en-NZ"),
                            "type": item.get("context", {}).get("name_type", "NL"),
                            "relation_id": item.get("relation_id"),
                        }
                        for item in names_from_relations
                    ]
                    raw_doc["names"] = enriched_names
                    # Also update display_name if we found NL names (prefer en-NZ)
                    if enriched_names:
                        # Priority: NL names in en-NZ, then any NL, then ABBR, then first available
                        nl_names_en = [
                            n
                            for n in enriched_names
                            if n.get("type") == "NL" and n.get("language") == "en-NZ"
                        ]
                        nl_names_any = [
                            n for n in enriched_names if n.get("type") == "NL"
                        ]
                        abbr_names = [
                            n for n in enriched_names if n.get("type") == "ABBR"
                        ]
                        best_name = None
                        if nl_names_en:
                            best_name = nl_names_en[0].get("name")
                        elif nl_names_any:
                            best_name = nl_names_any[0].get("name")
                        elif abbr_names:
                            best_name = abbr_names[0].get("name")
                        elif enriched_names:
                            best_name = enriched_names[0].get("name")
                        if best_name:
                            trimmed["display_name"] = best_name
                except Exception as e:
                    current_app.logger.warning(
                        f"Failed to enrich names for {concept_id}: {e}"
                    )
            return jsonify(trimmed), 200

        if isinstance(concept_stats, dict):
            data["concept_stats"] = concept_stats
        return jsonify(data), 200
    except Exception as e:  # pragma: no cover
        current_app.logger.error(
            f"Error getting node content for '{identifier}': {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "error": "Failed to retrieve node content due to an internal server error."
                }
            ),
            500,
        )


@vontology_bp.route("/parents", methods=["GET"])
def get_parents_route():
    """Return direct parents for a node with most salient parent marking.

    Query params:
      identifier: concept_id or Mongo _id hex string.
    """
    identifier = request.args.get("identifier")
    if not identifier:
        return jsonify({"error": "Missing 'identifier' parameter."}), 400
    try:
        resolved_identifier = identifier
        try:
            if (
                ObjectId
                and hasattr(ObjectId, "is_valid")
                and ObjectId.is_valid(identifier)
            ):
                try:
                    concept_doc = get_concept_by_id(identifier)
                    if concept_doc and concept_doc.get("concept_id"):
                        resolved_identifier = concept_doc.get("concept_id")
                        current_app.logger.debug(
                            f"Resolved mongo _id '{identifier}' to concept_id '{resolved_identifier}' for parents lookup."
                        )
                except ConceptNotFoundError:
                    current_app.logger.debug(
                        f"Identifier looks like ObjectId but no concept found for _id '{identifier}'; using _id directly."
                    )
        except Exception:  # pragma: no cover
            current_app.logger.exception(
                "Error while attempting to resolve ObjectId to concept_id for /parents route; using original identifier."
            )

        parent_data = get_vontology_node_parents(str(resolved_identifier))
        if "error" in parent_data:
            status_code = 404 if "not found" in parent_data["error"].lower() else 400
            return jsonify(parent_data), status_code
        return jsonify(parent_data), 200
    except Exception as e:  # pragma: no cover
        current_app.logger.error(
            f"Unexpected error fetching parents for node '{identifier}': {e}",
            exc_info=True,
        )
        return (
            jsonify(
                {"error": "Failed to retrieve parents due to an internal server error."}
            ),
            500,
        )


@vontology_bp.route("/update_description", methods=["POST"])
def update_description_route():
    """API endpoint to update the description of a specific Vontology node."""
    data = request.get_json()
    identifier = data.get("identifier")
    description = data.get("description")
    current_app.logger.info(
        f"Received request for /api/vontology/update_description with identifier: '{identifier}'"
    )

    if not identifier or description is None:
        current_app.logger.warning(
            "Missing 'identifier' or 'description' in /update_description request."
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Missing 'identifier' or 'description' parameter.",
                }
            ),
            400,
        )

    try:
        # Resolve to concept_id if identifier is a Mongo ObjectId
        resolved_concept_id = identifier
        try:
            if (
                ObjectId
                and hasattr(ObjectId, "is_valid")
                and ObjectId.is_valid(identifier)
            ):
                doc = get_concept_by_id(identifier)
                if doc and doc.get("concept_id"):
                    resolved_concept_id = doc["concept_id"]
        except ConceptNotFoundError:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"Node with identifier '{identifier}' not found.",
                    }
                ),
                404,
            )
        except Exception:  # pragma: no cover
            current_app.logger.exception(
                "Error resolving identifier to concept_id in update_description_route; using original"
            )

        ok = update_concept_description(resolved_concept_id, description)
        if not ok:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"Failed to update description for '{resolved_concept_id}'.",
                    }
                ),
                400,
            )
        return jsonify({"success": True, "concept_id": resolved_concept_id}), 200
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error updating description for node '{identifier}': {e}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Failed to update description due to an internal server error.",
                }
            ),
            500,
        )


@vontology_bp.route("/create_concept", methods=["POST"])
def create_concept_route():
    """Endpoint to create a new Vontology concept."""
    current_app.logger.info("Received request for /api/vontology/create_concept")
    data = request.get_json()
    if data is None:
        current_app.logger.warning(
            "Received non-JSON or empty payload for /create_concept."
        )
        return jsonify({"success": False, "message": "Request body must be JSON."}), 400

    parent_id = data.get("parent_id")  # Optional: None for root concept creation
    new_concept_name = data.get("new_concept_name")
    # ONTOLOGICAL FLAG: create_as_instance determines type vs individual creation
    # - False (default): Creates a TYPE (has is_a_type_of relationships, can be subtype AND instance)
    # - True: Creates an INDIVIDUAL (has is_an_instance_of, NO is_a_type_of relationships)
    # Key insight: Having subtype relationships (is_a_type_of) distinguishes types from individuals
    create_as_instance = data.get("create_as_instance", False)

    current_app.logger.info(
        f"Request details - Parent ID: {parent_id}, New Concept: {new_concept_name}, Create as instance: {create_as_instance}"
    )

    # CHICKEN-AND-EGG FIX: Allow root concept creation without parent_id
    if not new_concept_name:
        current_app.logger.warning(
            "Missing 'new_concept_name' in /create_concept request."
        )
        return (
            jsonify({"success": False, "message": "Missing 'new_concept_name'."}),
            400,
        )

    # parent_id is optional - if omitted, creates a root concept
    if not parent_id:
        current_app.logger.info("No parent_id provided - creating root concept")

    try:
        # Create concept without user attribution for privacy

        # Optional fields
        notes = data.get("notes")
        description = data.get("description")

        result = create_vontology_concept(
            parent_id,
            new_concept_name,
            create_as_instance,
            notes=notes,
            description=description,
        )
        current_app.logger.info(f"Result from create_vontology_concept: {result}")

        status_code = 201 if result.get("success") else 400

        if not result.get("success"):
            message = (result.get("message") or "").lower()
            if "already exists" in message:
                status_code = 409  # Conflict
            elif "invalid" in message:
                status_code = 400  # Bad Request

        # Normalize response for frontend convenience: surface concept_id and id at top-level
        if result.get("success") and isinstance(result.get("concept"), dict):
            concept_doc = result["concept"]
            try:
                from ...vontology.utils_vontology import invalidate_vontology_caches

                affected = []
                created_id = concept_doc.get("concept_id")
                if isinstance(created_id, str):
                    affected.append(created_id)
                if isinstance(parent_id, str) and parent_id.startswith("#V#"):
                    affected.append(parent_id)
                invalidate_vontology_caches(
                    affected,
                    correlation_id=str(uuid.uuid4()),
                )
            except Exception:
                current_app.logger.debug(
                    "Create concept cache invalidation failed",
                    exc_info=True,
                )
            response_body = {
                **result,
                "concept_id": concept_doc.get("concept_id"),
                "id": concept_doc.get("_id") or concept_doc.get("id"),
            }
            return jsonify(response_body), status_code

        return jsonify(result), status_code
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error creating Vontology concept '{new_concept_name}' under '{parent_id}': {e}",
            exc_info=True,
        )
        return (
            jsonify(
                {"success": False, "message": "An internal server error occurred."}
            ),
            500,
        )


@vontology_bp.route("/add_closure", methods=["POST"])
def add_closure_route():
    """API endpoint to add upward closure nodes for a Vontology concept."""
    current_app.logger.info("Received request for /api/vontology/add_closure")
    data = request.get_json()
    if not data:
        current_app.logger.warning("Received empty JSON payload for /add_closure.")
        return jsonify({"success": False, "message": "Request body must be JSON."}), 400

    node_path = data.get("node_path")
    current_app.logger.info(f"Request details - Node Path: {node_path}")

    if not node_path:
        current_app.logger.warning("Missing 'node_path' in /add_closure request.")
        return jsonify({"success": False, "message": "Missing 'node_path'."}), 400

    try:
        # Call the utility function to add closure nodes
        add_upward_closure_nodes(node_path)

        # Since the function doesn't return anything, assume success if no exception
        return (
            jsonify(
                {"success": True, "message": "Upward closure nodes added successfully."}
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error adding closure for node '{node_path}': {e}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "message": "An internal server error occurred while adding closure nodes.",
                }
            ),
            500,
        )


# --- NEW ROUTE ---
@vontology_bp.route("/nodes_details", methods=["GET"])
def get_nodes_details_route():
    """
    API endpoint to get details of all nodes within a specified subtree.
    Expects an 'identifier' query parameter (e.g., ?identifier=Thing/Animal or a concept_id or an _id).
    Defaults to 'Thing' if no identifier is provided.
    """
    identifier = request.args.get("identifier", "Thing")  # Default to 'Thing'

    # Call the utility function using the identifier as the root to scan from
    # We don't need subordinate_node_name here, as we provide the full starting identifier
    nodes_data = get_all_vontology_nodes_with_details(identifier=identifier)

    # Check if the utility function returned an error dictionary
    if isinstance(nodes_data, dict) and "error" in nodes_data:
        return jsonify(nodes_data), 404  # Or appropriate error code like 400

    return jsonify(nodes_data)


# --- END NEW ROUTE ---


@vontology_bp.route("/import_nodes", methods=["POST"])
def import_nodes_route():
    """
    API endpoint to import ontology nodes from a JSON structure.
    Handles multiple formats:
    1. Standard format: A JSON array of node objects.
    2. Von format: A JSON object with concept_id keys.
    3. OpenCyc format: A JSON object with "nodes" and "edges" keys.
    """
    current_app.logger.info("Received request for /import_nodes")

    # Check if this is a file upload (FormData) or JSON
    if request.files and "file" in request.files:
        # Handle file upload
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "message": "No file selected."}), 400

        try:
            file_content = file.read().decode("utf-8")
            data = json.loads(file_content)
        except json.JSONDecodeError as e:
            current_app.logger.error(f"Error parsing JSON file: {e}")
            return (
                jsonify({"success": False, "message": f"Invalid JSON file: {str(e)}"}),
                400,
            )
        except Exception as e:
            current_app.logger.error(f"Error reading file: {e}")
            return (
                jsonify({"success": False, "message": f"Error reading file: {str(e)}"}),
                400,
            )
    else:
        # Handle JSON payload
        data = request.get_json()
        if not data:
            current_app.logger.warning("Received empty JSON payload for /import_nodes.")
            return (
                jsonify({"success": False, "message": "Request body must be JSON."}),
                400,
            )

    current_app.logger.info(f"Received data type: {type(data)}")
    if isinstance(data, dict):
        current_app.logger.info(f"Data keys: {list(data.keys())}")
        if "nodes" in data:
            current_app.logger.info(f"Number of nodes in data: {len(data['nodes'])}")

    nodes_to_import = []

    # Check for OpenCyc format using our detection function
    if is_opencyc_format(data):
        current_app.logger.info("Detected OpenCyc format, converting to Von format.")
        try:
            nodes_to_import = convert_opencyc_to_von_format(data)
            current_app.logger.info(
                f"Converted {len(nodes_to_import)} OpenCyc nodes to Von format."
            )
        except Exception as e:
            current_app.logger.error(f"Error converting OpenCyc format: {e}")
            return (
                jsonify(
                    {
                        "success": False,
                        "message": f"Error converting OpenCyc format: {str(e)}",
                    }
                ),
                400,
            )

    # Check for legacy upward closure format (backup for old data)
    elif (
        isinstance(data, dict)
        and "edges" in data
        and "nodes" in data
        and not is_opencyc_format(data)
    ):
        current_app.logger.info(
            "Detected legacy upward closure format, processing with custom logic."
        )

        raw_nodes = data.get("nodes", [])
        edges = data.get("edges", [])

        # --- LEGACY LOGIC TO NORMALIZE IDs ---
        # Pass 1: Create a map from old URL IDs to new, standardized #V# IDs
        url_id_to_v_id_map = {}
        for node_data in raw_nodes:
            source_id = node_data.get("id")
            label = node_data.get("label")
            if not source_id or not label:
                continue
            pascal_name = to_pascal_case(label)
            new_v_id = generate_concept_id_from_name(pascal_name)
            url_id_to_v_id_map[source_id] = new_v_id

        # Build a map of parent relationships using the new #V# IDs
        parent_map = {}
        for edge in edges:
            child_url = edge.get("to")
            parent_url = edge.get("from")

            child_v_id = url_id_to_v_id_map.get(child_url)
            parent_v_id = url_id_to_v_id_map.get(parent_url)

            if child_v_id and parent_v_id:
                if child_v_id not in parent_map:
                    parent_map[child_v_id] = []
                parent_map[child_v_id].append(parent_v_id)

        # Pass 2: Transform nodes to the internal format using the new IDs
        for node_data in raw_nodes:
            source_id = node_data.get("id")
            label = node_data.get("label")
            if not source_id or not label:
                current_app.logger.warning(
                    f"Skipping node with missing id or label in upward closure data: {node_data}"
                )
                continue

            new_v_id = url_id_to_v_id_map.get(source_id)
            if not new_v_id:
                continue  # Should not happen if map is built correctly

            transformed_node = {
                "concept_id": new_v_id,
                "name": to_pascal_case(label),
                "description": node_data.get("comment", ""),
                "is_a_type_of": parent_map.get(new_v_id, []),
                "source_concept": source_id,  # Preserve original ID
            }
            nodes_to_import.append(transformed_node)
        # --- END LEGACY LOGIC ---

    # Check for standard format (payload is a list of nodes)
    elif isinstance(data, list):
        current_app.logger.info(
            "Processing as standard format (payload is a list of nodes)."
        )
        nodes_to_import = data

    # Handle standard format wrapped in a 'nodes' key
    elif isinstance(data, dict) and "nodes" in data and isinstance(data["nodes"], list):
        current_app.logger.info(
            "Processing as standard format (payload is an object with a 'nodes' array)."
        )
        nodes_to_import = data["nodes"]

    else:
        current_app.logger.warning(
            "Missing or invalid 'nodes' array in /import_nodes request."
        )
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Request payload must be a JSON array of nodes, or an object containing a 'nodes' array.",
                }
            ),
            400,
        )

    if not isinstance(nodes_to_import, list):
        current_app.logger.warning(
            f"Processed nodes are not a list, but {type(nodes_to_import)}."
        )
        return (
            jsonify({"success": False, "message": "Internal error processing nodes."}),
            500,
        )

    if len(nodes_to_import) == 0:
        return (
            jsonify(
                {
                    "success": True,
                    "message": "No new nodes to import.",
                    "imported_count": 0,
                }
            ),
            200,
        )

    # Support async mode (?async=true) to allow progress polling
    async_mode = (request.args.get("async") or "").lower() in ("1", "true", "yes", "y")

    if async_mode:
        job_id = str(uuid.uuid4())
        store = current_app.config.setdefault("VONTOLOGY_IMPORT_PROGRESS", {})
        store[job_id] = {
            "processed": 0,
            "total": len(nodes_to_import),
            "imported": 0,
            "updated": 0,
            "skipped": 0,
            "last_concept_id": None,
            "last_concept_name": None,
            "action": None,
            "status": "running",
            "result": None,
        }

        def _run():
            def _cb(p):
                try:
                    store[job_id].update(p)
                except Exception:
                    pass

            try:
                result = import_ontology_nodes(nodes_to_import, progress_callback=_cb)
                store[job_id]["result"] = result
                store[job_id]["status"] = "completed"
                if isinstance(result, dict) and result.get("success"):
                    try:
                        from ...vontology.utils_vontology import (
                            invalidate_vontology_caches,
                        )

                        affected_ids = []
                        for node in nodes_to_import:
                            if not isinstance(node, dict):
                                continue
                            concept_id = node.get("concept_id")
                            if isinstance(concept_id, str) and concept_id.startswith(
                                "#V#"
                            ):
                                affected_ids.append(concept_id)

                        invalidate_vontology_caches(
                            affected_ids,
                            correlation_id=str(uuid.uuid4()),
                        )
                    except Exception:
                        logging.getLogger(__name__).debug(
                            "Async import cache invalidation failed",
                            exc_info=True,
                        )
            except Exception as e:
                store[job_id]["status"] = "error"
                store[job_id]["error"] = str(e)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"success": True, "job_id": job_id, "async": True}), 202

    try:
        # Option A: Support safe_cycles flag (alias to existing minimal cycle breaking behaviour)
        safe_cycles_flag = False
        try:
            safe_cycles_flag = (request.args.get("safe_cycles") or "").lower() in (
                "1",
                "true",
                "yes",
            )
            if not safe_cycles_flag and isinstance(data, dict):
                safe_cycles_flag = bool(data.get("safe_cycles"))
        except Exception:
            pass

        result = import_ontology_nodes(nodes_to_import)
        if isinstance(result, dict) and "error" in result:
            current_app.logger.error(
                f"Error importing ontology nodes: {result['error']}"
            )
            return jsonify(result), 500
        imported = result.get("imported_count", 0)
        updated = result.get("updated_count", 0)
        skipped = result.get("skipped_count", 0)
        converted_from_opencyc = result.get("converted_from_opencyc", False)
        if converted_from_opencyc:
            success_msg = f"OpenCyc import completed! Added: {imported}, Updated: {updated}, Skipped: {skipped} (already existed)"
        else:
            success_msg = f"Import completed! Added: {imported}, Updated: {updated}, Skipped: {skipped}"
        current_app.logger.info(f"Successfully processed import: {success_msg}")
        enhanced_result = result.copy()
        enhanced_result["message"] = success_msg
        try:
            from ...vontology.utils_vontology import invalidate_vontology_caches

            affected_ids = []
            for node in nodes_to_import:
                if not isinstance(node, dict):
                    continue
                concept_id = node.get("concept_id")
                if isinstance(concept_id, str) and concept_id.startswith("#V#"):
                    affected_ids.append(concept_id)

            invalidate_vontology_caches(
                affected_ids,
                correlation_id=str(uuid.uuid4()),
            )
        except Exception:
            current_app.logger.debug(
                "Import cache invalidation failed",
                exc_info=True,
            )
        if safe_cycles_flag:
            # Ensure cycle_info present (utils now always returns but keep defensive mapping)
            cb = enhanced_result.get("cycle_breaking") or {}
            ci = enhanced_result.get("cycle_info") or {
                "has_cycles": bool(cb.get("had_cycles")),
                "strategy": ("minimal_break" if cb.get("had_cycles") else "none"),
                "applied": bool(cb.get("had_cycles")),
                "removed_edge_count": cb.get("removed_edge_count", 0),
                "removed_edges": cb.get("removed_edges", []),
                "unresolved_cycle_count": cb.get("unresolved_cycle_count", 0),
            }
            enhanced_result["cycle_info"] = ci
        else:
            enhanced_result.setdefault(
                "cycle_info",
                {"has_cycles": False, "strategy": "none", "applied": False},
            )
        return jsonify(enhanced_result), 200
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error importing ontology nodes: {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "success": False,
                    "message": "An internal server error occurred while importing nodes.",
                }
            ),
            500,
        )


@vontology_bp.route("/import_progress/<job_id>", methods=["GET"])
def import_progress_route(job_id):
    store = current_app.config.get("VONTOLOGY_IMPORT_PROGRESS", {})
    rec = store.get(job_id)
    if not rec:
        return jsonify({"success": False, "message": "Job not found"}), 404
    return jsonify({"success": True, "job_id": job_id, "progress": rec}), 200


@vontology_bp.route("/import_progress", methods=["GET"])
def list_import_progress_route():
    store = current_app.config.get("VONTOLOGY_IMPORT_PROGRESS", {})
    summary = {
        jid: {
            k: v
            for k, v in d.items()
            if k in ("processed", "total", "imported", "updated", "skipped", "status")
        }
        for jid, d in store.items()
    }
    return jsonify({"success": True, "jobs": summary}), 200


@vontology_bp.route("/import_preview", methods=["POST"])
def import_preview_route():
    """
    API endpoint to preview an import without actually importing.
    Analyzes the data and returns detailed information about what would be imported.
    """
    current_app.logger.info("Received request for /import_preview")

    # Handle file upload or JSON payload (same as import_nodes)
    if request.files and "file" in request.files:
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "message": "No file selected."}), 400

        try:
            file_content = file.read().decode("utf-8")
            data = json.loads(file_content)
        except json.JSONDecodeError as e:
            current_app.logger.error(f"Error parsing JSON file: {e}")
            return (
                jsonify({"success": False, "message": f"Invalid JSON file: {str(e)}"}),
                400,
            )
        except Exception as e:
            current_app.logger.error(f"Error reading file: {e}")
            return (
                jsonify({"success": False, "message": f"Error reading file: {str(e)}"}),
                400,
            )
    else:
        data = request.get_json()
        if not data:
            current_app.logger.warning(
                "Received empty JSON payload for /import_preview."
            )
            return (
                jsonify({"success": False, "message": "Request body must be JSON."}),
                400,
            )

    try:
        # Process the data using the same logic as import_nodes to get normalized nodes
        nodes_to_preview = []

        # Check for OpenCyc format
        if is_opencyc_format(data):
            current_app.logger.info("Detected OpenCyc format for preview.")
            try:
                nodes_to_preview = convert_opencyc_to_von_format(data)
                current_app.logger.info(
                    f"Converted {len(nodes_to_preview)} OpenCyc nodes for preview."
                )
            except Exception as e:
                current_app.logger.error(
                    f"Error converting OpenCyc format for preview: {e}"
                )
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": f"Error converting OpenCyc format: {str(e)}",
                        }
                    ),
                    400,
                )

        # Handle other formats (same logic as import_nodes)
        elif (
            isinstance(data, dict)
            and "edges" in data
            and "nodes" in data
            and not is_opencyc_format(data)
        ):
            # Legacy upward closure format
            raw_nodes = data.get("nodes", [])
            edges = data.get("edges", [])

            url_id_to_v_id_map = {}
            for node_data in raw_nodes:
                source_id = node_data.get("id")
                label = node_data.get("label")
                if not source_id or not label:
                    continue
                pascal_name = to_pascal_case(label)
                new_v_id = generate_concept_id_from_name(pascal_name)
                url_id_to_v_id_map[source_id] = new_v_id

            parent_map = {}
            for edge in edges:
                child_url = edge.get("to")
                parent_url = edge.get("from")

                child_v_id = url_id_to_v_id_map.get(child_url)
                parent_v_id = url_id_to_v_id_map.get(parent_url)

                if child_v_id and parent_v_id:
                    if child_v_id not in parent_map:
                        parent_map[child_v_id] = []
                    parent_map[child_v_id].append(parent_v_id)

            for node_data in raw_nodes:
                source_id = node_data.get("id")
                label = node_data.get("label")
                if not source_id or not label:
                    continue

                new_v_id = url_id_to_v_id_map.get(source_id)
                if not new_v_id:
                    continue

                transformed_node = {
                    "concept_id": new_v_id,
                    "name": to_pascal_case(label),
                    "description": node_data.get("comment", ""),
                    "is_a_type_of": parent_map.get(new_v_id, []),
                    "source_concept": source_id,
                }
                nodes_to_preview.append(transformed_node)

        elif isinstance(data, list):
            nodes_to_preview = data
        elif (
            isinstance(data, dict)
            and "nodes" in data
            and isinstance(data["nodes"], list)
        ):
            nodes_to_preview = data["nodes"]
        else:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Request payload must be a JSON array of nodes, or an object containing a 'nodes' array.",
                    }
                ),
                400,
            )

        # Now analyze the nodes for preview
        preview_analysis = analyze_import_preview(nodes_to_preview)

        # Option A: safe_cycles flag simply aliases to current minimal_break behaviour (auto-break unless strict env)
        safe_cycles_flag = False
        try:
            # Support both query param and JSON boolean field for convenience
            safe_cycles_flag = (request.args.get("safe_cycles") or "").lower() in (
                "1",
                "true",
                "yes",
            )
            if not safe_cycles_flag and isinstance(data, dict):
                safe_cycles_flag = bool(data.get("safe_cycles"))
        except Exception:
            pass

        if safe_cycles_flag:
            circ = preview_analysis.get("circular_references") or {}
            broken = circ.get("broken") or {}
            preview_analysis["cycle_info"] = {
                "has_cycles": bool(circ.get("has_cycles")),
                "strategy": "minimal_break",
                "applied": bool(broken.get("applied")),
                "removed_edge_count": broken.get("removed_edge_count", 0),
                "removed_edges": broken.get("removed_edges", []),
                "unresolved_cycle_count": broken.get("unresolved_cycle_count", 0),
            }
        else:
            # Provide a null-ish alias for consistency
            preview_analysis["cycle_info"] = {
                "has_cycles": False,
                "strategy": "none",
                "applied": False,
            }

        return jsonify({"success": True, "analysis": preview_analysis}), 200

    except Exception as e:
        current_app.logger.error(f"Error generating import preview: {e}", exc_info=True)
        return (
            jsonify(
                {"success": False, "message": f"Error generating preview: {str(e)}"}
            ),
            500,
        )


def analyze_import_preview(nodes_to_import):
    """
    Analyze import data and return detailed preview information.
    """
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...vontology.utils_vontology import (
        detect_circular_references_in_import,
        break_cycles_in_import_nodes,
    )

    analysis = {
        "total_nodes": len(nodes_to_import),
        "new_concepts": [],
        "existing_concepts": [],
        "parent_connections": {},
        "instance_connections": {},
        "type_breakdown": {"types": 0, "instances": 0, "unknown": 0},
        "format_detected": "unknown",
        "circular_references": {"has_cycles": False, "cycles": [], "warnings": []},
    }

    # CYCLE DETECTION & OPTIONAL BREAKING
    current_app.logger.info("Checking for circular references in preview data...")
    strict_env = os.getenv("VON_IMPORT_STRICT_CYCLES") in ("1", "true", "TRUE", "True")
    try:
        cycle_check = detect_circular_references_in_import(nodes_to_import)
        cycle_meta = {
            "has_cycles": cycle_check["has_cycles"],
            "cycles": cycle_check["cycles"],
            "cycle_count": cycle_check["cycle_count"],
            "safe_nodes_count": cycle_check["safe_count"],
            "warnings": [],
        }
        if cycle_check["has_cycles"]:
            current_app.logger.warning(
                f"Preview detected {len(cycle_check['cycles'])} circular references (strict_mode={strict_env})"
            )
            for cycle in cycle_check["cycles"]:
                cycle_str = " → ".join(cycle)
                cycle_meta["warnings"].append(
                    f"Circular reference detected: {cycle_str}"
                )
            if not strict_env:
                breaker = break_cycles_in_import_nodes(nodes_to_import)
                cycle_meta["broken"] = {
                    "applied": True,
                    "original_cycle_count": breaker.get("original_cycle_count"),
                    "removed_edge_count": breaker.get("removed_edge_count"),
                    "removed_edges": breaker.get("removed_edges"),
                    "unresolved_cycle_count": breaker.get("unresolved_cycle_count"),
                }
                if breaker.get("unresolved_cycle_count", 0) > 0:
                    cycle_meta["warnings"].append(
                        f"Unresolved cycles remain: {breaker.get('unresolved_cycle_count')}"
                    )
            else:
                cycle_meta["broken"] = {"applied": False, "reason": "strict_mode"}
        else:
            current_app.logger.info("✅ No circular references detected in preview")
            cycle_meta["broken"] = {"applied": False, "reason": "no_cycles"}
        analysis["circular_references"] = cycle_meta
    except Exception as e:
        current_app.logger.error(
            f"Error during cycle detection/breaking in preview: {e}"
        )
        analysis["circular_references"]["warnings"].append(
            f"Could not perform cycle analysis: {str(e)}"
        )

    # Get all existing concept IDs for comparison
    try:
        # Use the repository as the single concept DB access point.
        # Import preview is an admin-style operation, so explicitly bypass access control.
        with bypass_access_control():
            existing_concepts = ConceptsRepository.find({}, {"concept_id": 1})
            existing_concept_ids = {
                doc.get("concept_id")
                for doc in existing_concepts
                if isinstance(doc, dict) and isinstance(doc.get("concept_id"), str)
            }
    except Exception as e:
        current_app.logger.error(f"Error fetching existing concepts for preview: {e}")
        existing_concept_ids = set()

    # Helper: derive a display name with layered fallbacks + acronym normalization
    ACRONYMS = {
        "AI",
        "NLP",
        "LLM",
        "GPU",
        "CPU",
        "API",
        "HTTP",
        "HTTPS",
        "JSON",
        "SQL",
        "ID",
        "UUID",
        "URL",
        "UI",
        "UX",
        "ML",
        "RL",
        "DL",
    }

    def _normalize_word(w: str) -> str:
        if not w:
            return w
        # Preserve all-caps existing words (likely acronyms) or digits
        if w.upper() in ACRONYMS:
            return w.upper()
        if w.isupper() and len(w) <= 4:
            return w  # already acronym style
        if w.isdigit():
            return w
        return w[0].upper() + w[1:].lower()

    def _humanize_concept_id(cid: str) -> str:
        if not cid:
            return ""
        core = cid
        if core.startswith("#V#"):
            core = core[3:]
        # Replace separators with spaces
        import re as _re

        core = _re.sub(r"[\-_]+", " ", core)
        # Split camelCase / PascalCase boundaries by inserting space before capitals preceded by lowercase
        core = _re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", core)
        words = [w for w in core.strip().split() if w]
        return " ".join(
            _normalize_word(w.upper() if w.upper() in ACRONYMS else w) for w in words
        )

    def derive_display_name(node: dict) -> str:
        # Priority: explicit name -> names[0].name -> label -> concept_id/id humanized
        name = (node.get("name") or "").strip()
        if name:
            return name
        # Check structured names array if present
        names_arr = node.get("names")
        if isinstance(names_arr, list) and names_arr:
            for entry in names_arr:
                if isinstance(entry, dict):
                    n = (entry.get("name") or "").strip()
                    if n:
                        return n
        label = (node.get("label") or node.get("title") or "").strip()
        if label:
            return _humanize_concept_id(
                label
            )  # label might already be fine; still normalize acronyms/case
        # Fall back to concept_id or id
        cid = node.get("concept_id") or node.get("id") or ""
        humanized = _humanize_concept_id(cid)
        return humanized or "Unnamed"

    for node in nodes_to_import:
        concept_id = node.get("concept_id")
        name = derive_display_name(node)

        # Check if concept already exists
        if concept_id in existing_concept_ids:
            analysis["existing_concepts"].append(
                {"concept_id": concept_id, "name": name, "action": "update"}
            )
        else:
            analysis["new_concepts"].append(
                {"concept_id": concept_id, "name": name, "action": "create"}
            )

        # Analyze parent connections
        parents = node.get("is_a_type_of", [])
        if isinstance(parents, list):
            for parent in parents:
                if isinstance(parent, str):
                    if parent not in analysis["parent_connections"]:
                        analysis["parent_connections"][parent] = {
                            "count": 0,
                            "exists": parent in existing_concept_ids,
                        }
                    analysis["parent_connections"][parent]["count"] += 1

        # Analyze instance relationships
        instance_of = node.get("is_an_instance_of")
        if instance_of:
            if isinstance(instance_of, str):
                instance_of = [instance_of]
            if isinstance(instance_of, list):
                for inst in instance_of:
                    if isinstance(inst, str):
                        if inst not in analysis["instance_connections"]:
                            analysis["instance_connections"][inst] = {
                                "count": 0,
                                "exists": inst in existing_concept_ids,
                            }
                        analysis["instance_connections"][inst]["count"] += 1

        # Classify node type
        if instance_of:
            analysis["type_breakdown"]["instances"] += 1
        elif parents:
            analysis["type_breakdown"]["types"] += 1
        else:
            analysis["type_breakdown"]["unknown"] += 1

    # Post-process: ensure sample concept lists reflect derived names (already applied) and set a simple format detection heuristic
    try:
        if nodes_to_import and all(isinstance(n, dict) for n in nodes_to_import):
            # Heuristic for formats
            if any("source_concept" in n for n in nodes_to_import):
                analysis["format_detected"] = "transformed_opencyc"
            elif any(
                "label" in n and "comment" in n for n in nodes_to_import
            ) and not any("concept_id" in n for n in nodes_to_import):
                analysis["format_detected"] = "legacy_closure"
            elif any("names" in n for n in nodes_to_import):
                analysis["format_detected"] = "von_extended"
            elif any(
                n.get("concept_id", "").startswith("#V#") for n in nodes_to_import
            ):
                analysis["format_detected"] = "von_standard"
    except Exception as _e:
        current_app.logger.warning(f"Format detection heuristic failed: {_e}")

    return analysis


@vontology_bp.route("/export_nodes", methods=["GET"])
def export_nodes_route():
    """
    API endpoint to export all ontology nodes in JSON format compatible with import.
    Returns a JSON array of all vontology nodes with the fields needed for import.
    """
    current_app.logger.info("Received request for /api/vontology/export_nodes")

    try:
        # TODO: Implement export_ontology_nodes function or remove this endpoint
        current_app.logger.error("export_ontology_nodes function not implemented")
        return jsonify({"error": "Export functionality not implemented"}), 501

        # Placeholder code removed - function does not exist
        # result = export_ontology_nodes()
        # if isinstance(result, dict) and 'error' in result:
        #     current_app.logger.error(f"Error exporting ontology nodes: {result['error']}")
        #     return jsonify(result), 500
        # current_app.logger.info(f"Successfully exported {len(result)} ontology nodes")
        return jsonify(result), 200

    except Exception as e:
        current_app.logger.error(
            f"Unexpected error exporting ontology nodes: {e}", exc_info=True
        )
        return (
            jsonify(
                {"error": "An internal server error occurred while exporting nodes."}
            ),
            500,
        )


@vontology_bp.route("/search", methods=["GET"])
def search_concepts():
    """Lightweight search over Vontology concepts by names[].name (NL and ABBR types), legacy name, or concept_id.

        Query params:
            - q: search text (required)
            - limit: max results (default 10)
            - include_individuals: when true, include pure instances (default false, but frontend may set to true)
            - filter_kind: optional comma-separated kinds to include (type, predicate, individual)
            - include_predicate_metadata: when true, include is_text_predicate for predicate results (default false)
            - fallback_substring: when true, if few prefix results then widen with substring (default true)

    The search includes:
    - names[].name entries of type "NL" (natural language) and "ABBR" (abbreviations/acronyms)
    - Legacy name field for backward compatibility
    - concept_id field for direct ID matches
    """
    q = (request.args.get("q") or "").strip()
    limit = request.args.get("limit", type=int) or 10
    include_individuals = (request.args.get("include_individuals") or "").lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    include_predicate_metadata = (
        request.args.get("include_predicate_metadata") or ""
    ).lower() in ("1", "true", "yes", "y")
    filter_kind_param = (request.args.get("filter_kind") or "").strip()
    # Allow disabling fallback via query param; default on
    fallback_substring = (request.args.get("fallback_substring") or "true").lower() in (
        "1",
        "true",
        "yes",
        "y",
    )

    if not q:
        return jsonify({"results": []})

    try:
        # Determine filter_kind based on include_individuals parameter or explicit filter_kind.
        filter_kind = None
        if filter_kind_param:
            raw_parts = [part.strip().lower() for part in filter_kind_param.split(",")]
            allowed = {"type", "predicate", "individual"}
            kinds = [part for part in raw_parts if part in allowed]
            if kinds:
                filter_kind = kinds
        if filter_kind is None:
            if include_individuals:
                filter_kind = None  # Include all kinds
            else:
                filter_kind = ["type", "predicate"]  # Exclude pure individuals

        # Call the unified concept_search_service
        search_result = search_concepts_service(
            query=q,
            filter_kind=filter_kind,
            exact_match=False,
            limit=limit,
            use_two_pass=fallback_substring,
        )

        # Transform results to match expected frontend format
        # Frontend expects: [{"id": concept_id, "name": display_name, "kind": kind}, ...]
        # Service returns: [{"concept_id": ..., "name": ..., "kind": ..., "relevance_score": ...}, ...]
        predicate_text_map = {}
        if include_predicate_metadata:
            try:
                predicate_ids = [
                    result["concept_id"]
                    for result in search_result["results"]
                    if result.get("kind") == "predicate"
                ]
                if predicate_ids:
                    docs = ConceptsRepository.find(
                        {"concept_id": {"$in": predicate_ids}},
                        projection={
                            "concept_id": 1,
                            "relationships.is_an_instance_of": 1,
                        },
                    )
                    for doc in docs:
                        cid = doc.get("concept_id")
                        if not isinstance(cid, str):
                            continue
                        instance_of = doc.get("relationships", {}).get(
                            "is_an_instance_of", []
                        )
                        if isinstance(instance_of, str):
                            instance_of = [instance_of]
                        predicate_text_map[cid] = (
                            "#V#binary_text_predicate" in instance_of
                        )
            except Exception:
                predicate_text_map = {}

        results = []
        for result in search_result["results"]:
            entry = {
                "id": result["concept_id"],
                "name": result["name"],
                "kind": result["kind"],
                "relevance_score": result.get("relevance_score"),
            }
            if include_predicate_metadata and entry["kind"] == "predicate":
                entry["is_text_predicate"] = predicate_text_map.get(entry["id"], False)
            results.append(entry)

        return jsonify({"results": results})
    except Exception as e:
        current_app.logger.error(
            f"Error performing Vontology search: {e}", exc_info=True
        )
        return (
            jsonify(
                {"error": "Failed to perform search due to an internal server error."}
            ),
            500,
        )


@vontology_bp.route("/test_imported_nodes", methods=["GET"])
def test_imported_nodes():
    """
    Test endpoint to verify that specific nodes were imported correctly.
    """
    current_app.logger.info("Received request for /api/vontology/test_imported_nodes")

    try:

        # Test concepts that should be present after importing researchorg_von.json
        test_concepts = [
            "#V#research_organization",
            "#V#corporate_research_organisation",
            "#V#organization",
        ]

        results = []

        for concept_id in test_concepts:
            try:
                # Get basic concept info
                concept_data = get_concept_details_from_db(concept_id)

                # Get full concept document for description access via repository
                full_concept_doc = ConceptsRepository.find_one(
                    {"concept_id": concept_id}
                )

                if concept_data and full_concept_doc:
                    # Use accessor function for description (JVNAUTOSCI-320 implementation)
                    description = get_concept_description(full_concept_doc)
                    from ...vontology.utils_vontology import (
                        get_concept_display_name_with_names_fallback,
                    )

                    results.append(
                        {
                            "concept_id": concept_id,
                            "found": True,
                            "name": (
                                get_concept_display_name_with_names_fallback(
                                    concept_data[0]
                                )
                                if concept_data
                                else "Unknown"
                            ),
                            "path": (
                                concept_data[0].get("path")
                                if concept_data
                                else "Unknown"
                            ),
                            "description": (
                                description[:100] + "..."
                                if description and len(description) > 100
                                else description or "No description"
                            ),
                        }
                    )
                else:
                    results.append(
                        {
                            "concept_id": concept_id,
                            "found": False,
                            "error": "Concept not found in database",
                        }
                    )
            except Exception as e:
                results.append(
                    {"concept_id": concept_id, "found": False, "error": str(e)}
                )

        found_count = sum(1 for r in results if r.get("found"))

        return (
            jsonify(
                {
                    "success": True,
                    "total_tested": len(test_concepts),
                    "found_count": found_count,
                    "missing_count": len(test_concepts) - found_count,
                    "results": results,
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(f"Error testing imported nodes: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"Test failed: {str(e)}"}), 500


@vontology_bp.route("/debug_database", methods=["GET"])
def debug_database():
    """
    Debug endpoint to check database contents directly.
    """
    current_app.logger.info("Received request for /api/vontology/debug_database")

    try:
        # Use repository for concepts collection access
        repo = ConceptsRepository

        # Get total count
        total_count = repo.count_documents({})

        # Get all concept_ids
        all_concepts = list(repo.find({}, {"concept_id": 1, "name": 1, "_id": 0}))

        # Search for research-related concepts
        research_concepts = list(
            repo.find(
                {
                    "$or": [
                        {"name": {"$regex": "research", "$options": "i"}},
                        {"concept_id": {"$regex": "research", "$options": "i"}},
                        {"description": {"$regex": "research", "$options": "i"}},
                    ]
                },
                {"concept_id": 1, "name": 1, "path": 1, "_id": 0},
            )
        )

        # Search for organization-related concepts
        org_concepts = list(
            repo.find(
                {
                    "$or": [
                        {"name": {"$regex": "organi", "$options": "i"}},
                        {"concept_id": {"$regex": "organi", "$options": "i"}},
                        {"description": {"$regex": "organi", "$options": "i"}},
                    ]
                },
                {"concept_id": 1, "name": 1, "path": 1, "_id": 0},
            )
        )

        return (
            jsonify(
                {
                    "success": True,
                    "total_nodes": total_count,
                    "research_related": research_concepts,
                    "organization_related": org_concepts,
                    "sample_concepts": all_concepts[:10],  # First 10 for reference
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(f"Error debugging database: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"Debug failed: {str(e)}"}), 500


@vontology_bp.route("/delete_node", methods=["DELETE"])
def delete_node():
    """
    Delete a specific node by concept_id.
    """
    current_app.logger.info("Received request for /api/vontology/delete_node")

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Request body must be JSON."}), 400

    concept_id = data.get("concept_id")
    if not concept_id:
        return (
            jsonify({"success": False, "message": "Missing 'concept_id' parameter."}),
            400,
        )

    try:
        existing_node = ConceptsRepository.find_one({"concept_id": concept_id})
        if not existing_node:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": f"Node with concept_id '{concept_id}' not found.",
                    }
                ),
                404,
            )

        from ...vontology.utils_vontology import simulate_or_delete_concept

        report = simulate_or_delete_concept(concept_id, execute=True)
        if report.get("success") is True:
            current_app.logger.info(f"Successfully deleted node: {concept_id}")
            return (
                jsonify(
                    {
                        "success": True,
                        "deprecated": True,
                        "use_endpoint": "/api/vontology/node",
                        "message": f"Node '{existing_node.get('name', concept_id)}' deleted successfully.",
                        "deleted_concept_id": concept_id,
                        "deleted_name": existing_node.get("name"),
                        "report": report,
                    }
                ),
                200,
            )

        return (
            jsonify(
                {
                    "success": False,
                    "deprecated": True,
                    "use_endpoint": "/api/vontology/node",
                    "message": f"Failed to delete node '{concept_id}'.",
                    "report": report,
                }
            ),
            500,
        )

    except Exception as e:
        current_app.logger.error(
            f"Error deleting node {concept_id}: {e}", exc_info=True
        )
        return jsonify({"success": False, "message": f"Delete failed: {str(e)}"}), 500


@vontology_bp.route("/entity_counts", methods=["GET"])
def get_entity_counts():
    """
    Get entity counts for all vontology nodes to support filtering.

    CRITICAL: This function distinguishes between TYPES and ENTITIES in the unified concepts collection:
    - TYPES: Have is_a_type_of relationships (part of ontology hierarchy)
    - ENTITIES: Have is_an_instance_of relationships and NO subtype relationships

    See docs/design/ENTITY_TYPE_DISTINCTION.md for detailed explanation.
    """
    current_app.logger.info("Received request for /api/vontology/entity_counts")
    start_time = time.time()

    try:
        repo = ConceptsRepository

        # Get all vontology concepts (types in the tree structure)
        # These are concepts that appear in the vontology tree and can have instances
        # Note: We identify tree nodes by #V# prefix; subtype linkage uses relationships.is_a_type_of
        vontology_concepts = list(
            repo.find({"concept_id": {"$regex": "^#V#"}}, {"concept_id": 1, "name": 1})
        )

        # Build a mapping of concept_id to entity count
        entity_counts = {}

        for concept in vontology_concepts:
            concept_id = concept["concept_id"]

            # Count actual entities (instances) that have this concept_id in their is_an_instance_of relationship
            # Entities are identified as documents with NO subtype relationship (is_a_type_of missing or empty)
            # Handle both string and array formats for is_an_instance_of
            count = repo.count_documents(
                {
                    "$and": [
                        {
                            "$or": [
                                {"relationships.is_an_instance_of": concept_id},
                                {
                                    "relationships.is_an_instance_of": {
                                        "$in": [concept_id]
                                    }
                                },
                            ]
                        },
                        # Exclude type/collection documents
                        {
                            "$or": [
                                {"metadata.concept_type": {"$exists": False}},
                                {"metadata.concept_type": {"$ne": "collection"}},
                            ]
                        },
                        # Individuals should not have subtype relationships
                        {
                            "$or": [
                                {"relationships.is_a_type_of": {"$exists": False}},
                                {"relationships.is_a_type_of": []},
                            ]
                        },
                    ]
                }
            )

            entity_counts[concept_id] = {
                "name": concept.get("name", concept_id),
                "entity_count": count,
                "has_entities": count > 0,
            }

        current_app.logger.debug(
            f"Generated entity counts for {len(entity_counts)} concepts"
        )
        # Update lightweight stats
        try:
            duration_ms = int((time.time() - start_time) * 1000)
            _ENTITY_COUNTS_STATS["total_calls"] = (
                int(_ENTITY_COUNTS_STATS.get("total_calls", 0)) + 1
            )
            _ENTITY_COUNTS_STATS["last_ts"] = int(time.time())
            prev = _ENTITY_COUNTS_STATS.get("ema_ms")
            alpha = 0.2
            _ENTITY_COUNTS_STATS["ema_ms"] = (
                duration_ms
                if prev in (None, 0)
                else (alpha * duration_ms + (1 - alpha) * float(prev))
            )
        except Exception:
            pass
        return jsonify({"entity_counts": entity_counts})

    except Exception as e:
        current_app.logger.error(f"Error getting entity counts: {e}", exc_info=True)
    return (
        jsonify(
            {
                "error": "Failed to retrieve entity counts due to an internal server error."
            }
        ),
        500,
    )


@vontology_bp.route("/instance_counts", methods=["GET"])
def get_instance_counts():
    """
    Get instance counts (direct + indirect) for a list of Vontology type concept_ids.

    Query params:
      ids: comma-separated list of concept_id strings (e.g. #V#Person,#V#Organization)
      rebuild: optional bool (default true). If true, rebuild stats snapshot when stale/missing.
      include_stale_values: optional bool (default false). If true, returns stale cached values
          when available, still marked with stats_status=stale.

    Returns JSON mapping concept_id -> {
        direct, indirect, total, descendant_count,
        has_any_instances_in_subtree, direct_subtype_count, total_subtype_count_in_subtree,
        stats_status, stats_generated_at
    }
    """
    current_app.logger.info("Received request for /api/vontology/instance_counts")

    ids_param = request.args.get("ids", "")
    if not ids_param:
        return jsonify({"error": "Missing 'ids' query parameter."}), 400

    try:
        type_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
        if not type_ids:
            return jsonify({"error": "No valid ids provided."}), 400

        rebuild_if_needed = (request.args.get("rebuild") or "1").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        include_stale_values = (request.args.get("include_stale_values") or "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        from ...services.vontology_concept_stats_service import (
            STATS_STATUS_AVAILABLE,
            get_vontology_concept_stats,
        )

        stats_payload = get_vontology_concept_stats(
            type_ids,
            rebuild_if_needed=rebuild_if_needed,
            include_stale_values=include_stale_values,
        )
        concept_stats = stats_payload.get("concept_stats") or {}
        scope_status = stats_payload.get("stats_status")

        results: dict[str, dict[str, Any]] = {}
        for tid in type_ids:
            stat = concept_stats.get(tid) if isinstance(concept_stats, dict) else None
            stat = stat if isinstance(stat, dict) else {}
            kind = stat.get("kind")
            stats_status = stat.get("stats_status", scope_status)

            direct_raw = stat.get("direct_instance_count")
            total_raw = stat.get("total_instance_count_in_subtree")
            subtype_total_raw = stat.get("total_subtype_count_in_subtree")

            direct = int(direct_raw) if isinstance(direct_raw, int) else None
            total = int(total_raw) if isinstance(total_raw, int) else None
            indirect = (total - direct) if (isinstance(total, int) and isinstance(direct, int)) else None
            descendant_count = (
                int(subtype_total_raw) + 1 if isinstance(subtype_total_raw, int) else None
            )

            result_row: dict[str, Any] = {
                "direct": direct,
                "indirect": indirect,
                "total": total,
                "descendant_count": descendant_count,
                "has_any_instances_in_subtree": stat.get("has_any_instances_in_subtree"),
                "direct_subtype_count": stat.get("direct_subtype_count"),
                "total_subtype_count_in_subtree": stat.get(
                    "total_subtype_count_in_subtree"
                ),
                "stats_status": stats_status,
                "stats_generated_at": stat.get("stats_generated_at"),
            }

            if kind != "type":
                result_row["stats_reason"] = (
                    "not_a_type_concept"
                    if stats_status == STATS_STATUS_AVAILABLE
                    else stat.get("stats_reason")
                )
            elif stats_status != STATS_STATUS_AVAILABLE:
                result_row["stats_reason"] = stat.get("stats_reason")
            elif stat.get("stats_unavailable"):
                result_row["stats_reason"] = stat.get("stats_reason")

            if isinstance(stat.get("stats_error"), str):
                result_row["stats_error"] = stat.get("stats_error")

            results[tid] = result_row

        # Retain lightweight endpoint-local cache for diagnostics compatibility.
        try:
            cache_scope = cache_scope_key()
            cache_key = f"{cache_scope}|{','.join(sorted(type_ids))}"
            _INSTANCE_COUNTS_CACHE[cache_key] = (int(time.time()), results)
        except Exception:
            current_app.logger.debug(
                "Failed to write compatibility instance_counts cache entry",
                exc_info=True,
            )

        return jsonify(
            {
                "instance_counts": results,
                "stats_status": stats_payload.get("stats_status"),
                "stats_generated_at": stats_payload.get("stats_generated_at"),
            }
        )
    except Exception as e:
        current_app.logger.error(f"Error in /instance_counts: {e}", exc_info=True)
        return (
            jsonify(
                {
                    "error": "Failed to compute instance counts due to an internal server error."
                }
            ),
            500,
        )


@vontology_bp.route("/concept_stats", methods=["GET"])
def get_concept_stats_route():
    """Return status-aware cached stats for one or more concept IDs.

    Query params:
      ids: comma-separated concept IDs (preferred)
      id: single concept ID (fallback)
      rebuild: optional bool, default false
      include_stale_values: optional bool, default false
    """
    ids_param = request.args.get("ids", "")
    id_param = request.args.get("id", "")
    if not ids_param and not id_param:
        return jsonify({"error": "Missing 'ids' or 'id' query parameter."}), 400

    concept_ids = []
    if ids_param:
        concept_ids.extend([i.strip() for i in ids_param.split(",") if i.strip()])
    if id_param:
        concept_ids.append(id_param.strip())
    concept_ids = list(dict.fromkeys([c for c in concept_ids if c]))
    if not concept_ids:
        return jsonify({"error": "No valid concept IDs provided."}), 400

    rebuild_if_needed = (request.args.get("rebuild") or "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    include_stale_values = (
        (request.args.get("include_stale_values") or "0").lower()
        in ("1", "true", "yes", "on")
    )

    try:
        from ...services.vontology_concept_stats_service import (
            get_vontology_concept_stats,
        )

        payload = get_vontology_concept_stats(
            concept_ids,
            rebuild_if_needed=rebuild_if_needed,
            include_stale_values=include_stale_values,
        )
        return jsonify(payload), 200
    except Exception as exc:
        current_app.logger.error(
            "Error in /concept_stats: %s", exc, exc_info=True
        )
        return (
            jsonify(
                {
                    "error": "Failed to compute concept stats due to an internal server error."
                }
            ),
            500,
        )


@vontology_bp.route("/children", methods=["GET"])
def get_node_children():
    """Endpoint to fetch immediate children of a specific node."""
    node_id = request.args.get("node_id")
    current_app.logger.info(
        f"Received request for /api/vontology/children with node_id: '{node_id}'"
    )

    if not node_id:
        current_app.logger.warning("Missing 'node_id' parameter in /children request.")
        return jsonify({"error": "Missing 'node_id' parameter."}), 400

    try:
        repo = ConceptsRepository

        # Find all nodes that have this node as their parent via relationships.is_a_type_of
        # Handle both array and string representations
        children_cursor = repo.find(
            {
                "$or": [
                    {"relationships.is_a_type_of": node_id},
                    {"relationships.is_a_type_of": {"$in": [node_id]}},
                ]
            },
            {
                "name": 1,
                "names": 1,
                "concept_id": 1,
                "path": 1,
                "concept_data.preserved_fields.description": 1,
                "metadata.description": 1,
                "_id": 0,
            },
            sort=[("name", 1)],
        )

        children = []
        for doc in children_cursor:
            # Compute display name with fallback
            resolved_name = (
                get_concept_display_name_with_names_fallback(doc)
                or doc.get("name", "")
                or (doc.get("concept_id") or "")
            )

            # Opportunistically persist derived NL name if doc lacks top-level name and has no NL entry
            try:
                top_name = doc.get("name")
                names = doc.get("names")
                cid = doc.get("concept_id")
                if cid and (not isinstance(top_name, str) or not top_name.strip()):
                    has_matching_nl = False
                    has_any_nl = False
                    if isinstance(names, list):
                        for entry in names:
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("type") == "NL":
                                has_any_nl = True
                                nm = str(entry.get("name", "")).strip()
                                lang = entry.get("language")
                                if nm == resolved_name and (
                                    lang in (None, "", "en-NZ")
                                ):
                                    has_matching_nl = True
                                    break
                    # Only persist if we actually derived a human name and it's not already present
                    if (
                        (not has_matching_nl)
                        and isinstance(resolved_name, str)
                        and resolved_name
                        and not resolved_name.startswith("#V#")
                        and resolved_name != "Unnamed Concept"
                    ):
                        new_entry = {
                            "name": resolved_name,
                            "language": "en-NZ",
                            "type": "NL",
                        }
                        if (
                            isinstance(names, list)
                            and len(names) > 0
                            and not has_any_nl
                        ):
                            # Append only when there's no NL yet
                            repo.update_one(
                                {"concept_id": cid}, {"$push": {"names": new_entry}}
                            )
                        elif not isinstance(names, list) or len(names) == 0:
                            repo.update_one(
                                {"concept_id": cid}, {"$set": {"names": [new_entry]}}
                            )
            except Exception:
                current_app.logger.debug(
                    "Failed to persist derived NL name for child %s",
                    doc.get("concept_id"),
                    exc_info=True,
                )

            child_data = {
                "id": doc.get("concept_id"),
                "name": resolved_name,
                "path": doc.get("path", ""),
                # 'description' suppressed (JVNAUTOSCI-573 global removal); resolve via relations if needed
            }
            children.append(child_data)

        current_app.logger.debug(f"Found {len(children)} children for node {node_id}")
        return jsonify({"children": children})

    except Exception as e:
        current_app.logger.error(
            f"Error getting children for node {node_id}: {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "error": "Failed to retrieve children due to an internal server error."
                }
            ),
            500,
        )


@vontology_bp.route("/instances", methods=["GET"])
def get_node_instances():
    """Endpoint to fetch instances of a specific concept node."""
    node_id = request.args.get("node_id")
    include_subtypes = request.args.get("include_subtypes", "").lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    current_app.logger.info(
        f"Received request for /api/vontology/instances with node_id: '{node_id}'"
    )

    if not node_id:
        current_app.logger.warning("Missing 'node_id' parameter in /instances request.")
        return jsonify({"error": "Missing 'node_id' parameter."}), 400

    try:
        repo = ConceptsRepository

        # Build filter for instances of this concept, optionally including subtypes
        type_ids = [node_id]
        try:
            if include_subtypes:
                # Resolve this node and all descendant type IDs (concept_id strings)
                from ...vontology.utils_vontology import (
                    get_vontology_node_and_descendant_ids,
                )

                ids = get_vontology_node_and_descendant_ids(node_id) or []
                # Ensure node_id is included
                if node_id not in ids:
                    ids.append(node_id)
                type_ids = ids
        except Exception:
            current_app.logger.warning(
                "Failed to resolve descendant type ids for include_subtypes; defaulting to direct only",
                exc_info=True,
            )

        # Find all nodes that are instances of this concept (or any subtype if requested)
        instances_cursor = repo.find(
            {
                "$and": [
                    {
                        "$or": [
                            {"relationships.is_an_instance_of": {"$in": type_ids}},
                            # Back-compat: handle scalar value too
                            {"relationships.is_an_instance_of": node_id},
                        ]
                    },
                    # Exclude type/collection documents
                    {
                        "$or": [
                            {"metadata.concept_type": {"$exists": False}},
                            {"metadata.concept_type": {"$ne": "collection"}},
                        ]
                    },
                    # Individuals should not have subtype relationships
                    {
                        "$or": [
                            {"relationships.is_a_type_of": {"$exists": False}},
                            {"relationships.is_a_type_of": []},
                        ]
                    },
                ]
            },
            {
                "concept_id": 1,
                "name": 1,
                "names": 1,
                "path": 1,
                "notes": 1,
                "concept_data.preserved_fields.description": 1,
                "metadata.description": 1,
                "_id": 0,
            },
            sort=[("name", 1)],
        )

        instances = []
        for doc in instances_cursor:
            resolved_name = (
                get_concept_display_name_with_names_fallback(doc)
                or doc.get("name", "")
                or (doc.get("concept_id") or "")
            )

            # Opportunistically persist derived NL name if doc lacks top-level name and has no NL entry
            try:
                top_name = doc.get("name")
                names = doc.get("names")
                cid = doc.get("concept_id")
                if cid and (not isinstance(top_name, str) or not top_name.strip()):
                    has_matching_nl = False
                    has_any_nl = False
                    if isinstance(names, list):
                        for entry in names:
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("type") == "NL":
                                has_any_nl = True
                                nm = str(entry.get("name", "")).strip()
                                lang = entry.get("language")
                                if nm == resolved_name and (
                                    lang in (None, "", "en-NZ")
                                ):
                                    has_matching_nl = True
                                    break
                    if (
                        (not has_matching_nl)
                        and isinstance(resolved_name, str)
                        and resolved_name
                        and not resolved_name.startswith("#V#")
                        and resolved_name != "Unnamed Concept"
                    ):
                        new_entry = {
                            "name": resolved_name,
                            "language": "en-NZ",
                            "type": "NL",
                        }
                        if (
                            isinstance(names, list)
                            and len(names) > 0
                            and not has_any_nl
                        ):
                            repo.update_one(
                                {"concept_id": cid}, {"$push": {"names": new_entry}}
                            )
                        elif not isinstance(names, list) or len(names) == 0:
                            repo.update_one(
                                {"concept_id": cid}, {"$set": {"names": [new_entry]}}
                            )
            except Exception:
                current_app.logger.debug(
                    "Failed to persist derived NL name for instance %s",
                    doc.get("concept_id"),
                    exc_info=True,
                )

            instance_data = {
                "id": doc.get("concept_id"),
                "name": resolved_name,
                "notes": get_concept_notes(doc) or "",
                # 'description' suppressed (JVNAUTOSCI-573 global removal); resolve via relations if needed
            }
            instances.append(instance_data)

        # Virtual fallback: surface code-handled concepts as instances where appropriate.
        try:
            from ...vontology.code_concepts_registry import (
                iter_code_concepts,
                build_virtual_concept_doc,
                MENTIONED_IN_VON_CODE_ID,
                PREDICATE_TYPE_ID,
            )

            want_virtual = node_id in {MENTIONED_IN_VON_CODE_ID, PREDICATE_TYPE_ID}
            if want_virtual:
                existing_ids = {
                    inst.get("id")
                    for inst in instances
                    if isinstance(inst, dict) and isinstance(inst.get("id"), str)
                }
                for cc in iter_code_concepts():
                    if cc.concept_id in existing_ids:
                        continue
                    vdoc = build_virtual_concept_doc(cc.concept_id)
                    if not isinstance(vdoc, dict):
                        continue
                    inst_of = (vdoc.get("relationships") or {}).get(
                        "is_an_instance_of"
                    ) or []
                    if isinstance(inst_of, str):
                        inst_list = [inst_of]
                    elif isinstance(inst_of, list):
                        inst_list = [x for x in inst_of if isinstance(x, str)]
                    else:
                        inst_list = []
                    if node_id not in inst_list:
                        continue
                    instances.append(
                        {
                            "id": cc.concept_id,
                            "name": cc.display_name,
                            "notes": "",
                        }
                    )

                instances.sort(key=lambda item: (item.get("name") or "").lower())
        except Exception:
            pass

        current_app.logger.debug(
            f"Found {len(instances)} instances for concept {node_id}"
        )
        return jsonify({"instances": instances, "count": len(instances)})

    except Exception as e:
        current_app.logger.error(
            f"Error getting instances for concept {node_id}: {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "error": "Failed to retrieve instances due to an internal server error."
                }
            ),
            500,
        )


@vontology_bp.route("/relationships", methods=["GET"])
def get_relationships_route():
    """
    Get relationships for a concept by identifier (concept_id like '#V#...' or Mongo _id).
    Returns normalized relationship arrays with optional resolved names for convenience.
    """
    identifier = request.args.get("identifier")
    current_app.logger.info(
        f"Received request for /api/vontology/relationships with identifier: '{identifier}'"
    )

    if not identifier:
        return (
            jsonify({"success": False, "error": "Missing 'identifier' parameter."}),
            400,
        )

    try:
        repo = ConceptsRepository

        def _find_by_identifier(ident: str):
            if ident.startswith("#V#"):
                return repo.find_one({"concept_id": ident})
            # try as ObjectId
            if ObjectId:
                try:
                    oid = ObjectId(ident)
                    return repo.find_one({"_id": oid})
                except Exception:
                    pass
            # try as concept_id string fallback
            return repo.find_one({"concept_id": ident})

        doc = _find_by_identifier(identifier)
        if not doc:
            # Some built-in predicate concepts are handled in code only and may not
            # exist as MongoDB concept documents. Treat those as virtual concepts.
            try:
                from ...vontology.code_concepts_registry import (
                    build_virtual_concept_doc,
                )

                doc = build_virtual_concept_doc(identifier)
            except Exception:
                doc = None

        if not doc:
            return (
                jsonify(
                    {"success": False, "error": f"Concept '{identifier}' not found."}
                ),
                404,
            )

        def _norm_list(val):
            if isinstance(val, list):
                return [v for v in val if isinstance(v, str) and v.strip()]
            if isinstance(val, str) and val.strip():
                return [val]
            return []

        def _normalise_relationship_aliases(rel_map):
            if not isinstance(rel_map, dict):
                return {}
            rel_map = dict(rel_map)
            alias_map = {
                "has_subtypes": "has_subtype",
                "is_a_type_ofs": "is_a_type_of",
                "is_an_instance_ofs": "is_an_instance_of",
                "has_instances": "has_instance",
                "related_tos": "related_to",
            }
            for alias, canonical in alias_map.items():
                alias_vals = _norm_list(rel_map.get(alias))
                if not alias_vals:
                    continue
                canonical_vals = _norm_list(rel_map.get(canonical))
                rel_map[canonical] = sorted(set(canonical_vals + alias_vals))
                rel_map.pop(alias, None)
            return rel_map

        rel = _normalise_relationship_aliases(doc.get("relationships") or {})

        structural_kinds = [
            "is_a_type_of",
            "has_subtype",
            "is_an_instance_of",
            "has_instance",
            "related_to",
        ]
        out = {k: _norm_list(rel.get(k)) for k in structural_kinds}

        # Derive has_subtype entries dynamically from child is_a_type_of edges when missing
        try:
            concept_identifier = doc.get("concept_id")
            if concept_identifier:
                existing_subtypes = set(out.get("has_subtype", []))
                if not existing_subtypes:
                    child_cursor = repo.find(
                        {"relationships.is_a_type_of": concept_identifier},
                        {"concept_id": 1},
                    )
                    derived_children: list[str] = []
                    for child_doc in child_cursor:
                        child_id = child_doc.get("concept_id")
                        if (
                            isinstance(child_id, str)
                            and child_id
                            and child_id not in existing_subtypes
                        ):
                            derived_children.append(child_id)
                    if derived_children:
                        out["has_subtype"] = sorted(
                            set(out.get("has_subtype", [])) | set(derived_children)
                        )
        except Exception as derive_err:
            current_app.logger.debug(
                "[relationships] Failed to derive has_subtype for %s: %s",
                doc.get("concept_id"),
                derive_err,
                exc_info=True,
            )

        # Capture any additional dynamic predicate keys (e.g., #V#salient_binary_predicate_for_type)
        dynamic_raw = {
            k: _norm_list(v)
            for k, v in rel.items()
            if k not in out and k != "most_salient_type"
        }

        # Get most salient type for highlighting (use cached or compute dynamically)
        most_salient_type = rel.get("most_salient_type")
        if not most_salient_type:
            # Compute dynamically if not cached
            try:
                most_salient_type = get_most_salient_type(doc, use_cache=False)
                current_app.logger.debug(
                    f"Computed dynamic most_salient_type for {doc.get('concept_id')}: {most_salient_type}"
                )
            except Exception as e:
                current_app.logger.warning(
                    f"Failed to compute dynamic most_salient_type for {doc.get('concept_id')}: {e}"
                )
                most_salient_type = None

        # Resolve names AND kind for each id (structural + dynamic) for UI convenience
        # This eliminates N frontend metadata fetches for relationship rendering
        ids_to_resolve = sorted(
            {
                cid
                for arr in list(out.values()) + list(dynamic_raw.values())
                for cid in arr
            }
        )
        metadata_map = {}
        if ids_to_resolve:
            # Fetch concept documents for legacy fields and computed_kind
            cursor = repo.find(
                {"concept_id": {"$in": ids_to_resolve}},
                {"concept_id": 1, "name": 1, "names": 1, "computed_kind": 1},
            )
            for r in cursor:
                cid = r.get("concept_id")
                # First try legacy name resolution
                try:
                    nm = get_concept_display_name_with_names_fallback(r)
                except Exception:
                    nm = r.get("name") or cid

                # Get computed_kind, defaulting to "individual" if not set
                kind = r.get("computed_kind") or "individual"
                if cid:
                    metadata_map[cid] = {"name": nm, "kind": kind}

            # For concepts not found or with fallback names, try text_relations
            concepts_needing_text_relations = [
                cid
                for cid in ids_to_resolve
                if cid not in metadata_map or metadata_map[cid]["name"] == cid
            ]

            if concepts_needing_text_relations:
                # Batch fetch text_relations names (hasName predicate)
                from ...db.repositories.text_value_repository import (
                    TextRelationsRepository,
                    TextValuesRepository,
                )

                text_relations = list(
                    TextRelationsRepository.find(
                        {
                            "subject_concept_id": {
                                "$in": concepts_needing_text_relations
                            },
                            "predicate": "hasName",
                        }
                    )
                )

                if text_relations:
                    # Get text values for these relations
                    text_value_ids = [tr["object_text_id"] for tr in text_relations]
                    text_values = list(
                        TextValuesRepository.find(
                            {
                                "_id": {
                                    "$in": [
                                        ObjectId(tid) if ObjectId else tid
                                        for tid in text_value_ids
                                    ]
                                }
                            }
                        )
                    )

                    # Build text_id -> text map
                    text_map = {
                        str(tv["_id"]): tv.get("text", "") for tv in text_values
                    }

                    # Update metadata_map with text_relations names
                    for tr in text_relations:
                        cid = tr["subject_concept_id"]
                        text_id = tr["object_text_id"]
                        text = text_map.get(text_id)
                        if text and cid in concepts_needing_text_relations:
                            if cid not in metadata_map:
                                metadata_map[cid] = {"name": text, "kind": "individual"}
                            else:
                                # Update name if it was a fallback
                                metadata_map[cid]["name"] = text

        # Create enriched relationships with name, kind, and most salient type marking
        enriched = {}
        for k, arr in out.items():
            enriched_items = []
            for cid in arr:
                metadata = metadata_map.get(cid, {"name": cid, "kind": "individual"})
                item = {"id": cid, "name": metadata["name"], "kind": metadata["kind"]}
                # Mark the most salient parent type
                if (
                    k == "is_a_type_of"
                    and most_salient_type
                    and cid == most_salient_type
                ):
                    item["is_most_salient"] = True
                enriched_items.append(item)
            enriched[k] = enriched_items

        # Add enriched dynamic predicate relationship groups with name and kind
        for k, arr in dynamic_raw.items():
            enriched[k] = [
                {
                    "id": cid,
                    "name": metadata_map.get(cid, {"name": cid, "kind": "individual"})[
                        "name"
                    ],
                    "kind": metadata_map.get(cid, {"name": cid, "kind": "individual"})[
                        "kind"
                    ],
                }
                for cid in arr
            ]

        return (
            jsonify(
                {
                    "success": True,
                    "concept_id": doc.get("concept_id"),
                    "relationships": enriched,
                    "most_salient_type": most_salient_type,
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(
            f"Error getting relationships for '{identifier}': {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Failed to retrieve relationships due to an internal server error.",
                }
            ),
            500,
        )


@vontology_bp.route("/relationships/add", methods=["POST"])
def add_relationship_route():
    """
    Add a relationship edge between source_id and target_id.
    Maintains inverse consistency for: is_a_type_of<->has_subtype, is_an_instance_of<->has_instance, related_to<->related_to.
    Body: { source_id: str, kind: str, target_id: str }
    """
    data = request.get_json() or {}
    source_id = data.get("source_id")
    kind = data.get("kind")
    target_id = data.get("target_id")
    current_app.logger.info(
        f"Received request for /api/vontology/relationships/add: {data}"
    )

    if not source_id or not target_id or not kind:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Missing 'source_id', 'target_id', or 'kind'.",
                }
            ),
            400,
        )

    # Normalise structural predicates using the authoritative service (JVNAUTOSCI-986)
    from ...services.relationship_write_service import (
        normalise_structural_predicate,
        is_structural_predicate,
        RELATIONSHIP_KINDS,
    )

    kind = normalise_structural_predicate(kind)

    if source_id == target_id:
        return (
            jsonify(
                {"success": False, "error": "Source and target cannot be the same."}
            ),
            400,
        )

    # Use shared RELATIONSHIP_KINDS (JVNAUTOSCI-986: single authoritative pathway)
    is_dynamic_predicate = False
    if kind not in RELATIONSHIP_KINDS:
        # Allow arbitrary predicate concept ids (starting with #V#) as dynamic, non-inverted edges
        if isinstance(kind, str) and kind.startswith("#V#"):
            is_dynamic_predicate = True
        else:
            return (
                jsonify(
                    {"success": False, "error": f"Invalid relationship kind '{kind}'."}
                ),
                400,
            )

    try:
        repo = ConceptsRepository

        from ...vontology.code_concepts_registry import (
            build_virtual_concept_doc,
            is_code_concept_id,
        )
        from ...vontology.utils_vontology import is_predicate

        if is_dynamic_predicate:
            predicate_doc = repo.find_one(
                {"concept_id": kind}, {"concept_id": 1, "relationships": 1}
            )
            if predicate_doc is None and is_code_concept_id(kind):
                predicate_doc = build_virtual_concept_doc(kind)

            if predicate_doc is None:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": (
                                f"Predicate concept '{kind}' not found. Create it as a predicate concept "
                                "(e.g. instance of #V#predicate) before using it as a relationship."
                            ),
                        }
                    ),
                    400,
                )
            if not is_predicate(predicate_doc):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": (
                                f"Concept '{kind}' exists but is not typed as a predicate. "
                                "Predicates must be instances of #V#predicate (or a predicate subtype)."
                            ),
                        }
                    ),
                    400,
                )

        # Check if this is a binary text predicate (expects text values, not concept references)
        is_text_predicate = False
        if is_dynamic_predicate:
            predicate_doc = repo.find_one(
                {"concept_id": kind}, {"relationships.is_an_instance_of": 1}
            )
            if predicate_doc:
                instance_of = predicate_doc.get("relationships", {}).get(
                    "is_an_instance_of", []
                )
                if isinstance(instance_of, str):
                    instance_of = [instance_of]
                is_text_predicate = "#V#binary_text_predicate" in instance_of

        # Ensure source document exists
        src = repo.find_one({"concept_id": source_id})
        if not src:
            return (
                jsonify(
                    {"success": False, "error": f"Source '{source_id}' not found."}
                ),
                404,
            )

        # For binary text predicates, redirect to text relations API
        if is_text_predicate:
            try:
                from ...services.text_value_service import upsert_text_for_concept
                from ...services.rag_text_relation_change_hook_service import (
                    maybe_sync_concept_text_relations_to_rag,
                )

                namespace = None
                try:
                    window_session_id = request.headers.get("X-Von-Window-Session")
                    user_concept_id = session.get("user_concept_id")
                    effective = get_effective_context(
                        window_session_id, dict(session), user_concept_id
                    )
                    effective_namespace = effective.get("namespace")
                    if (
                        isinstance(effective_namespace, str)
                        and effective_namespace.strip()
                        and effective_namespace.strip().startswith("#V#")
                    ):
                        namespace = effective_namespace.strip()
                    else:
                        session_namespace = session.get("namespace")
                        if (
                            isinstance(session_namespace, str)
                            and session_namespace.strip()
                            and session_namespace.strip().startswith("#V#")
                        ):
                            namespace = session_namespace.strip()
                        else:
                            if (
                                isinstance(user_concept_id, str)
                                and user_concept_id.strip()
                                and user_concept_id.strip().startswith("#V#")
                            ):
                                namespace = user_concept_id.strip()
                except Exception:
                    namespace = None

                result = upsert_text_for_concept(
                    subject_concept_id=source_id,
                    predicate=kind,
                    text=target_id,  # target_id is actually the text value for text predicates
                    lang="en",
                    provenance={"source": "relationship_add_redirect"},
                )

                maybe_sync_concept_text_relations_to_rag(
                    namespace=namespace,
                    concept_id=source_id,
                    predicate=kind,
                )
                current_app.logger.info(
                    f"Redirected binary text predicate {kind} to text relations API: {result}"
                )
                return (
                    jsonify(
                        {
                            "success": True,
                            "redirected_to_text_relations": True,
                            "result": result,
                        }
                    ),
                    200,
                )
            except Exception as e:
                current_app.logger.error(
                    f"Error adding text relation for binary text predicate: {e}",
                    exc_info=True,
                )
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"Failed to add text relation: {str(e)}",
                        }
                    ),
                    500,
                )

        # For non-text predicates, ensure target concept exists
        tgt = repo.find_one({"concept_id": target_id})
        if not tgt:
            return (
                jsonify(
                    {"success": False, "error": f"Target '{target_id}' not found."}
                ),
                404,
            )

        # Helper to normalize to array then add value uniquely using $addToSet
        def _ensure_array_and_add(cid: str, rel_kind: str, rel_target: str):
            # Read current value
            existing = (
                repo.find_one({"concept_id": cid}, {f"relationships.{rel_kind}": 1})
                or {}
            )
            rels = existing.get("relationships") or {}
            curr = rels.get(rel_kind)
            if isinstance(curr, list):
                pass
            elif isinstance(curr, str):
                # Convert string to array containing previous value
                repo.update_one(
                    {"concept_id": cid}, {"$set": {f"relationships.{rel_kind}": [curr]}}
                )
            else:
                # Initialize as empty array
                repo.update_one(
                    {"concept_id": cid}, {"$set": {f"relationships.{rel_kind}": []}}
                )
            # Now safe to add
            repo.update_one(
                {"concept_id": cid},
                {"$addToSet": {f"relationships.{rel_kind}": rel_target}},
            )

        # Add forward relation (normalized)
        _ensure_array_and_add(source_id, kind, target_id)

        # For structural kinds, also add inverse; skip for dynamic predicates
        if not is_dynamic_predicate:
            inverse_map = {
                "is_a_type_of": ("has_subtype", target_id, source_id),
                "has_subtype": ("is_a_type_of", target_id, source_id),
                "is_an_instance_of": ("has_instance", target_id, source_id),
                "has_instance": ("is_an_instance_of", target_id, source_id),
                "related_to": ("related_to", target_id, source_id),
            }
            inv_kind, inv_src, inv_tgt = inverse_map[kind]
            _ensure_array_and_add(inv_src, inv_kind, inv_tgt)

        try:
            from ...vontology.utils_vontology import invalidate_vontology_caches

            invalidate_vontology_caches(
                [source_id, target_id],
                correlation_id=str(uuid.uuid4()),
            )
        except Exception:
            current_app.logger.debug(
                "Relationship add cache invalidation failed",
                exc_info=True,
            )

        return jsonify({"success": True, "message": "Relationship added."}), 200
    except Exception as e:
        current_app.logger.error(f"Error adding relationship: {e}", exc_info=True)
        return (
            jsonify(
                {"success": False, "error": f"Failed to add relationship: {str(e)}"}
            ),
            500,
        )


@vontology_bp.route("/predicates/elicitation", methods=["GET"])
def list_elicitation_predicates():
    """List predicate concepts that are instances of the knowledge elicitation predicate meta-type or binary text predicate.

    Used by UI to extend the relationship kind dropdown with domain predicates (e.g. #V#supervisor, #V#has_email).
    Includes metadata about whether each predicate expects text values vs concept references.
    """
    try:
        repo = ConceptsRepository
        predicate_types = [
            "#V#knowledge_elicitation_predicate",
            "#V#binary_text_predicate",
        ]
        cursor = repo.find(
            {"relationships.is_an_instance_of": {"$in": predicate_types}},
            {
                "concept_id": 1,
                "name": 1,
                "names": 1,
                "relationships.is_an_instance_of": 1,
            },
        )
        from ...vontology.utils_vontology import (
            get_concept_display_name_with_names_fallback,
        )

        preds = []
        for d in cursor:
            cid = d.get("concept_id")
            if not cid:
                continue
            try:
                label = get_concept_display_name_with_names_fallback(d)
            except Exception:
                label = d.get("name") or cid

            # Determine if this is a binary text predicate
            instance_of = d.get("relationships", {}).get("is_an_instance_of", [])
            if isinstance(instance_of, str):
                instance_of = [instance_of]
            is_text_predicate = "#V#binary_text_predicate" in instance_of

            preds.append(
                {"id": cid, "label": label, "is_text_predicate": is_text_predicate}
            )
        preds.sort(key=lambda x: x["label"].lower())
        return jsonify({"success": True, "predicates": preds}), 200
    except Exception as e:
        current_app.logger.error(
            f"Error listing elicitation predicates: {e}", exc_info=True
        )
        return (
            jsonify(
                {"success": False, "error": "Failed to list elicitation predicates."}
            ),
            500,
        )


@vontology_bp.route("/predicates/salient", methods=["GET"])
def list_salient_predicates_for_instance():
    """Return salient binary predicates aggregated from ALL of an individual's types (direct + inherited).

    We collect:
      1. Direct types in instance.relationships.is_an_instance_of
      2. All ancestor types reachable via chains of is_a_type_of
    For every discovered type we gather its values of '#V#salient_binary_predicate_for_type'.
    The union (deduped) is returned, sorted alphabetically by display label.

    Query params:
        instance_id: concept_id of the individual concept.
    """
    instance_id = request.args.get("instance_id")
    if not instance_id:
        return jsonify({"success": False, "error": "Missing instance_id"}), 400

    # Check for force_bfs parameter to bypass precomputed mode
    force_bfs = request.args.get("force_bfs", "").lower() in ("true", "1", "yes")
    split_enabled = _is_salient_split_enabled()

    # Simple in-process cache (keyed by sorted direct types). Minimal TTL to avoid stale results after edits.
    # Structure: _SALIENT_CACHE = { key: (ts, predicates_list, types_list[, scope_payload]) }
    # Instrumentation counters stored in _SALIENT_STATS.
    global _SALIENT_CACHE  # type: ignore
    global _SALIENT_STATS  # type: ignore
    # Initialize caches/stats if missing (avoid unused-expression lint warnings)
    if "_SALIENT_CACHE" not in globals() or not isinstance(_SALIENT_CACHE, dict):  # type: ignore[name-defined]
        _SALIENT_CACHE = {}
    # Stats: high-level call & cache counters
    if "_SALIENT_STATS" not in globals() or not isinstance(_SALIENT_STATS, dict):  # type: ignore[name-defined]
        _SALIENT_STATS = {"total_calls": 0, "cache_hits": 0, "cache_misses": 0}
    # Metrics: per-mode counters + EMA timings
    if "_SALIENT_METRICS" not in globals() or not isinstance(_SALIENT_METRICS, dict):  # type: ignore[name-defined]
        _SALIENT_METRICS = {
            "fast_path_calls": 0,
            "bfs_calls": 0,
            "fast_path_predicates_total": 0,
            "bfs_predicates_total": 0,
            "ema_fast_ms": None,
            "ema_bfs_ms": None,
            "ema_alpha": 0.2,
        }
    CACHE_TTL_SECONDS = 30
    try:
        repo = ConceptsRepository
        inst = repo.find_one({"concept_id": instance_id})
        if not inst:
            return (
                jsonify(
                    {"success": False, "error": f"Instance '{instance_id}' not found"}
                ),
                404,
            )
        rels = inst.get("relationships") or {}
        direct_types = rels.get("is_an_instance_of") or []
        # Normalize direct_types: may be stored as a single string; ensure list[str]
        if isinstance(direct_types, str):
            direct_types = [direct_types]
        elif not isinstance(direct_types, list):
            direct_types = []
        if not direct_types:
            return (
                jsonify({"success": True, "predicates": [], "types_considered": []}),
                200,
            )

        # Ensure all entries are strings
        direct_types = [t for t in direct_types if isinstance(t, str) and t]

        # DEBUG: Log what direct types we found
        current_app.logger.info(
            f"[SALIENT_DEBUG] instance_id={instance_id} direct_types={direct_types}"
        )

        scope_sets = {
            "instance": set(),
            "type": set(),
            "unclassified": set(),
        }
        scope_origins = {scope_name: defaultdict(set) for scope_name in scope_sets}

        cache_key = ",".join(sorted(direct_types))
        if split_enabled:
            cache_key = f"{cache_key}|split"
        now = time.time()
        start = now
        cached = _SALIENT_CACHE.get(cache_key)
        if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
            _SALIENT_STATS["total_calls"] += 1
            _SALIENT_STATS["cache_hits"] += 1
            duration_ms = int((time.time() - start) * 1000)
            current_app.logger.info(
                f"/predicates/salient cache HIT instance={instance_id} key={cache_key} duration_ms={duration_ms} types={len(cached[2])} preds={len(cached[1])}"
            )
            debug_flag = request.args.get("debug") == "true"
            scope_payload = cached[3] if len(cached) >= 4 else None
            payload = {
                "success": True,
                "predicates": cached[1],
                "types_considered": cached[2],
                "cache": True,
            }
            if split_enabled:
                scope_payload = scope_payload or {}
                payload["predicates_by_scope"] = scope_payload.get(
                    "predicates_by_scope", {}
                )
                payload["predicate_origins"] = scope_payload.get(
                    "predicate_origins", {}
                )
            if debug_flag:
                payload["metrics"] = {
                    "stats": _SALIENT_STATS,
                    "counters": _SALIENT_METRICS,
                }
            resp = jsonify(payload)
            resp.headers["X-Salient-Cache"] = "hit"
            return resp, 200

        # Attempt fast precomputed path: all direct type docs have inherited_salient_binary_predicates
        precomputed_mode = False
        fast_predicates: list[str] = []
        type_docs = []
        if direct_types and not force_bfs:
            type_docs = list(
                repo.find(
                    {
                        "concept_id": {
                            "$in": [t for t in direct_types if isinstance(t, str)]
                        }
                    },
                    {"concept_id": 1, "inherited_salient_binary_predicates": 1},
                )
            )
            doc_map = {
                d.get("concept_id"): d
                for d in type_docs
                if isinstance(d.get("concept_id"), str)
            }
            all_have = True
            ordered_union_ids: list[str] = []
            seen_ids = set()
            for t in direct_types:
                if not isinstance(t, str):
                    all_have = False
                    break
                d = doc_map.get(t)
                vals = None
                if d:
                    vals = d.get("inherited_salient_binary_predicates")
                if not isinstance(vals, list):
                    all_have = False
                    break
                for v in vals:
                    if isinstance(v, str) and v not in seen_ids:
                        seen_ids.add(v)
                        ordered_union_ids.append(v)
            if all_have:
                precomputed_mode = True
                fast_predicates = ordered_union_ids
                current_app.logger.info(
                    f"[SALIENT_DEBUG] Fast path SUCCESS for {instance_id}: predicates={fast_predicates}"
                )
            else:
                current_app.logger.info(
                    f"[SALIENT_DEBUG] Fast path FAILED for {instance_id}: all_have={all_have}, ordered_union_ids={ordered_union_ids}, type_docs={[d.get('concept_id') for d in type_docs]}"
                )

        def _collect_bfs_salience(seed_types: list[str]) -> tuple[list[str], list[str]]:
            queue = list(seed_types)
            visited: set[str] = set()
            ordered_types_local: list[str] = []
            salient_ids_local: list[str] = []
            salient_set_local: set[str] = set()
            expansions = 0
            max_expansions = 200
            while queue and expansions < max_expansions:
                t = queue.pop(0)
                if not isinstance(t, str) or not t or t in visited:
                    continue
                visited.add(t)
                ordered_types_local.append(t)
                expansions += 1
                doc = (
                    repo.find_one(
                        {"concept_id": t},
                        {
                            "concept_id": 1,
                            "relationships": 1,
                            SALIENT_SCOPE_FIELD: 1,
                            "salient_binary_predicates_for_type": 1,
                        },
                    )
                    or {}
                )
                trels = doc.get("relationships") or {}
                scope_data = extract_salient_scope_lists(doc)
                for pid in scope_data.get("instance", []):
                    if isinstance(pid, str) and pid:
                        if pid not in salient_set_local:
                            salient_set_local.add(pid)
                            salient_ids_local.append(pid)
                        scope_sets["instance"].add(pid)
                        scope_origins["instance"][pid].add(t)
                for pid in scope_data.get("type", []):
                    if isinstance(pid, str) and pid:
                        scope_sets["type"].add(pid)
                        scope_origins["type"][pid].add(t)
                for pid in scope_data.get("unclassified", []):
                    if isinstance(pid, str) and pid:
                        scope_sets["unclassified"].add(pid)
                        scope_origins["unclassified"][pid].add(t)
                parents = trels.get("is_a_type_of") or []
                if isinstance(parents, list):
                    for p in parents:
                        if isinstance(p, str) and p not in visited and p not in queue:
                            queue.append(p)
            return ordered_types_local, salient_ids_local

        run_bfs_for_scope = split_enabled or not precomputed_mode
        ordered_types: list[str] = []
        salient_ids: list[str] = []
        if run_bfs_for_scope:
            ordered_types, salient_ids = _collect_bfs_salience(direct_types)

        from ...vontology.utils_vontology import (
            get_concept_display_name_with_names_fallback,
        )

        predicates_out = []
        resolved_ids = (
            fast_predicates if (precomputed_mode and fast_predicates) else salient_ids
        )
        if not resolved_ids and salient_ids:
            resolved_ids = salient_ids

        lookup_ids = set(resolved_ids)
        if split_enabled:
            for scope_name in scope_sets:
                lookup_ids.update(scope_sets[scope_name])

        name_map = {}
        text_predicate_map = {}
        if lookup_ids:
            cursor = repo.find(
                {"concept_id": {"$in": list(lookup_ids)}},
                {
                    "concept_id": 1,
                    "name": 1,
                    "names": 1,
                    "relationships.is_an_instance_of": 1,
                },
            )
            for d in cursor:
                cid = d.get("concept_id")
                if not cid:
                    continue
                try:
                    disp = get_concept_display_name_with_names_fallback(d)
                except Exception:
                    disp = d.get("name") or cid
                name_map[cid] = disp

                instance_of = d.get("relationships", {}).get("is_an_instance_of", [])
                if isinstance(instance_of, str):
                    instance_of = [instance_of]
                text_predicate_map[cid] = "#V#binary_text_predicate" in instance_of

        for pid in resolved_ids:
            predicates_out.append(
                {
                    "id": pid,
                    "label": name_map.get(pid, pid),
                    "is_text_predicate": text_predicate_map.get(pid, False),
                }
            )
        predicates_out.sort(key=lambda x: x["label"].lower())
        ordered_types_final = direct_types if precomputed_mode else ordered_types

        scope_cache_entry = None
        if split_enabled:
            predicates_by_scope: dict[str, list[dict]] = {}
            for scope_name in ("instance", "type", "unclassified"):
                scope_ids = scope_sets[scope_name]
                if not scope_ids:
                    predicates_by_scope[scope_name] = []
                    continue
                sorted_ids = sorted(
                    scope_ids, key=lambda pid: (name_map.get(pid, pid).lower(), pid)
                )
                scope_items = []
                for pid in sorted_ids:
                    origin_types = sorted(scope_origins[scope_name].get(pid, set()))
                    scope_items.append(
                        {
                            "id": pid,
                            "label": name_map.get(pid, pid),
                            "is_text_predicate": text_predicate_map.get(pid, False),
                            "origin_types": origin_types,
                        }
                    )
                predicates_by_scope[scope_name] = scope_items
            predicate_origins = {
                scope_name: {
                    pid: sorted(list(types))
                    for pid, types in scope_origins[scope_name].items()
                }
                for scope_name in scope_origins
            }
            raw_scope_map = {
                "instance": sorted(scope_sets["instance"]),
                "type": sorted(scope_sets["type"]),
                "unclassified": sorted(scope_sets["unclassified"]),
            }
            scope_cache_entry = {
                "predicates_by_scope": predicates_by_scope,
                "predicate_origins": predicate_origins,
                "raw_scope_map": raw_scope_map,
            }

        cache_tuple = (now, predicates_out, ordered_types_final)
        if scope_cache_entry is not None:
            cache_tuple = cache_tuple + (scope_cache_entry,)
        _SALIENT_CACHE[cache_key] = cache_tuple
        _SALIENT_STATS["total_calls"] += 1
        _SALIENT_STATS["cache_misses"] += 1
        duration_ms = int((time.time() - start) * 1000)
        mode = "precomputed" if precomputed_mode else "bfs"
        # Update metrics
        if precomputed_mode:
            _SALIENT_METRICS["fast_path_calls"] += 1
            _SALIENT_METRICS["fast_path_predicates_total"] += len(predicates_out)
            prev = _SALIENT_METRICS.get("ema_fast_ms")
            alpha = _SALIENT_METRICS["ema_alpha"]
            _SALIENT_METRICS["ema_fast_ms"] = (
                duration_ms
                if prev is None
                else (alpha * duration_ms + (1 - alpha) * prev)
            )
        else:
            _SALIENT_METRICS["bfs_calls"] += 1
            _SALIENT_METRICS["bfs_predicates_total"] += len(predicates_out)
            prev = _SALIENT_METRICS.get("ema_bfs_ms")
            alpha = _SALIENT_METRICS["ema_alpha"]
            _SALIENT_METRICS["ema_bfs_ms"] = (
                duration_ms
                if prev is None
                else (alpha * duration_ms + (1 - alpha) * prev)
            )

        current_app.logger.info(
            f"/predicates/salient cache MISS instance={instance_id} key={cache_key} mode={mode} duration_ms={duration_ms} types={len(ordered_types_final)} preds={len(predicates_out)}"
        )
        debug_flag = request.args.get("debug") == "true"
        payload = {
            "success": True,
            "predicates": predicates_out,
            "types_considered": ordered_types_final,
            "cache": False,
            "mode": mode,
        }
        if split_enabled:
            payload["predicates_by_scope"] = (scope_cache_entry or {}).get(
                "predicates_by_scope", {}
            )
            payload["predicate_origins"] = (scope_cache_entry or {}).get(
                "predicate_origins", {}
            )
            payload["raw_scope_map"] = (scope_cache_entry or {}).get(
                "raw_scope_map", {}
            )
        if debug_flag:
            payload["metrics"] = {
                "stats": _SALIENT_STATS,
                "counters": _SALIENT_METRICS,
                "duration_ms": duration_ms,
            }
        resp = jsonify(payload)
        resp.headers["X-Salient-Cache"] = "miss"
        resp.headers["X-Salient-Mode"] = mode
        return resp, 200
    except Exception as e:
        current_app.logger.error(
            f"Error listing salient predicates: {e}", exc_info=True
        )
        return (
            jsonify({"success": False, "error": "Failed to list salient predicates."}),
            500,
        )


@vontology_bp.route("/predicates/salient/cache/invalidate", methods=["POST"])
def invalidate_salient_cache():
    """Invalidate the in-process salient predicate cache.

    Returns counts of previous entries and current instrumentation stats.
    """
    global _SALIENT_CACHE  # type: ignore
    global _SALIENT_STATS  # type: ignore
    try:
        prev_entries = len(_SALIENT_CACHE) if isinstance(_SALIENT_CACHE, dict) else 0
        _SALIENT_CACHE = {}
    except NameError:
        prev_entries = 0
        _SALIENT_CACHE = {}
    try:
        stats = _SALIENT_STATS
    except NameError:
        stats = {"total_calls": 0, "cache_hits": 0, "cache_misses": 0}
        _SALIENT_STATS = stats
    payload = {
        "success": True,
        "cleared_entries": prev_entries,
        "stats": stats,
        "cleared_at": int(time.time()),
    }
    current_app.logger.info(
        f"/predicates/salient/cache/invalidate cleared={prev_entries} stats={stats}"
    )
    return jsonify(payload), 200


@vontology_bp.route("/predicates/salient/recompute_inherited", methods=["POST"])
def recompute_inherited_salient():
    """Trigger batch recomputation of inherited salient predicates.

    Body JSON (optional): { "force": bool, "limit": int, "dry_run": bool }
    """
    params = request.get_json(silent=True) or {}
    force = bool(params.get("force"))
    limit_param = params.get("limit")
    # Accept multiple forms for dry_run: true boolean, "true" string, 1, "1"
    raw_dry = params.get("dry_run")
    dry_run = False
    if isinstance(raw_dry, bool):
        dry_run = raw_dry
    elif isinstance(raw_dry, (int, float)):
        dry_run = raw_dry != 0
    elif isinstance(raw_dry, str):
        dry_run = raw_dry.strip().lower() in ("1", "true", "yes", "y")
    current_app.logger.info(
        f"/predicates/salient/recompute_inherited request params force={force} limit={limit_param} raw_dry_run={raw_dry} parsed_dry_run={dry_run}"
    )

    limit_value = None
    if isinstance(limit_param, (int, float)):
        if int(limit_param) > 0:
            limit_value = int(limit_param)
    elif isinstance(limit_param, str):
        try:
            parsed = int(limit_param)
            if parsed > 0:
                limit_value = parsed
        except ValueError:
            current_app.logger.warning(
                "Ignoring non-integer limit parameter: %s", limit_param
            )

    start = time.time()
    try:
        summary = recompute_salient_predicates(
            force=force, limit=limit_value, dry_run=dry_run
        )
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        current_app.logger.error(
            "recompute_inherited_salient failed force=%s dry_run=%s limit=%s error=%s",
            force,
            dry_run,
            limit_value,
            exc,
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": str(exc),
                    "invocation_duration_ms": duration_ms,
                    "dry_run_requested": dry_run,
                    "limit_requested": limit_param,
                }
            ),
            500,
        )

    duration_ms = int((time.time() - start) * 1000)
    summary["invocation_duration_ms"] = duration_ms
    summary["dry_run_requested"] = dry_run
    summary["limit_requested"] = limit_param
    current_app.logger.info(
        f"/predicates/salient/recompute_inherited force={force} dry_run={dry_run} limit={limit_param} summary={summary}"
    )
    return jsonify(summary), 200 if summary.get("success") else 500


@vontology_bp.route("/relationships/remove", methods=["POST"])
def remove_relationship_route():
    """
    Remove a relationship edge between source_id and target_id.
    Maintains inverse consistency for: is_a_type_of<->has_subtype, is_an_instance_of<->has_instance, related_to<->related_to.
    Body: { source_id: str, kind: str, target_id: str }
    """
    data = request.get_json() or {}
    source_id = data.get("source_id")
    kind = data.get("kind")
    target_id = data.get("target_id")
    current_app.logger.info(
        f"Received request for /api/vontology/relationships/remove: {data}"
    )

    if not source_id or not target_id or not kind:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Missing 'source_id', 'target_id', or 'kind'.",
                }
            ),
            400,
        )

    if source_id == target_id:
        return (
            jsonify(
                {"success": False, "error": "Source and target cannot be the same."}
            ),
            400,
        )

    structural_allowed = {
        "is_a_type_of",
        "has_subtype",
        "is_an_instance_of",
        "has_instance",
        "related_to",
    }
    is_dynamic_predicate_kind = (
        kind.startswith("#V#") and kind not in structural_allowed
    )
    if kind not in structural_allowed and not is_dynamic_predicate_kind:
        return (
            jsonify(
                {"success": False, "error": f"Invalid relationship kind '{kind}'."}
            ),
            400,
        )

    try:
        repo = ConceptsRepository

        # Check if this is a binary text predicate (expects text values, not concept references)
        is_text_predicate = False
        if is_dynamic_predicate_kind:
            predicate_doc = repo.find_one(
                {"concept_id": kind}, {"relationships.is_an_instance_of": 1}
            )
            if predicate_doc:
                instance_of = predicate_doc.get("relationships", {}).get(
                    "is_an_instance_of", []
                )
                if isinstance(instance_of, str):
                    instance_of = [instance_of]
                is_text_predicate = "#V#binary_text_predicate" in instance_of

        # For binary text predicates, redirect to text relations API
        if is_text_predicate:
            try:
                from ...services.text_value_service import (
                    delete_text_relation_by_predicate_and_text,
                )
                from ...services.rag_text_relation_change_hook_service import (
                    maybe_delete_text_relation_doc_from_rag,
                )

                namespace = None
                try:
                    window_session_id = request.headers.get("X-Von-Window-Session")
                    user_concept_id = session.get("user_concept_id")
                    effective = get_effective_context(
                        window_session_id, dict(session), user_concept_id
                    )
                    effective_namespace = effective.get("namespace")
                    if (
                        isinstance(effective_namespace, str)
                        and effective_namespace.strip()
                        and effective_namespace.strip().startswith("#V#")
                    ):
                        namespace = effective_namespace.strip()
                    else:
                        session_namespace = session.get("namespace")
                        if (
                            isinstance(session_namespace, str)
                            and session_namespace.strip()
                            and session_namespace.strip().startswith("#V#")
                        ):
                            namespace = session_namespace.strip()
                        else:
                            if (
                                isinstance(user_concept_id, str)
                                and user_concept_id.strip()
                                and user_concept_id.strip().startswith("#V#")
                            ):
                                namespace = user_concept_id.strip()
                except Exception:
                    namespace = None

                result = delete_text_relation_by_predicate_and_text(
                    subject_concept_id=source_id,
                    predicate=kind,
                    text=target_id,  # target_id is actually the text value for text predicates
                )

                maybe_delete_text_relation_doc_from_rag(
                    namespace=namespace,
                    relation_id=result.get("relation_id"),
                )
                current_app.logger.info(
                    f"Redirected binary text predicate {kind} removal to text relations API: {result}"
                )
                return (
                    jsonify(
                        {
                            "success": True,
                            "redirected_to_text_relations": True,
                            "result": result,
                        }
                    ),
                    200,
                )
            except Exception as e:
                current_app.logger.warning(
                    f"Text relation removal failed, checking concept relationships for legacy data: {e}"
                )
                # Fallback: Check if this binary text predicate is stored as a concept relationship (legacy)
                try:
                    src = repo.find_one({"concept_id": source_id})
                    if src and kind in src.get("relationships", {}):
                        relationships = src.get("relationships", {})
                        if target_id in relationships.get(kind, []):
                            # Remove from concept relationships (legacy cleanup)
                            repo.update_one(
                                {"concept_id": source_id},
                                {"$pull": {f"relationships.{kind}": target_id}},
                            )
                            current_app.logger.info(
                                f"Removed legacy binary text predicate {kind} from concept relationships"
                            )
                            return (
                                jsonify(
                                    {
                                        "success": True,
                                        "legacy_cleanup": True,
                                        "message": "Removed from concept relationships (legacy data)",
                                    }
                                ),
                                200,
                            )

                    # Neither location had the relationship
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": f"Failed to remove text relation: {str(e)}",
                            }
                        ),
                        500,
                    )
                except Exception as fallback_e:
                    current_app.logger.error(
                        f"Fallback removal also failed: {fallback_e}", exc_info=True
                    )
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": f"Failed to remove relationship from both text relations and concept relationships: {str(e)}",
                            }
                        ),
                        500,
                    )

        # For non-text predicates, ensure both documents exist
        src = repo.find_one({"concept_id": source_id})
        tgt = repo.find_one({"concept_id": target_id})
        if not src:
            return (
                jsonify(
                    {"success": False, "error": f"Source '{source_id}' not found."}
                ),
                404,
            )
        if not tgt:
            return (
                jsonify(
                    {"success": False, "error": f"Target '{target_id}' not found."}
                ),
                404,
            )

        # Helper to normalize to array then remove value using $pull
        def _ensure_array_and_remove(cid: str, rel_kind: str, rel_target: str):
            existing = (
                repo.find_one({"concept_id": cid}, {f"relationships.{rel_kind}": 1})
                or {}
            )
            rels = existing.get("relationships") or {}
            curr = rels.get(rel_kind)
            if isinstance(curr, list):
                pass
            elif isinstance(curr, str):
                # Convert to array representation to safely pull
                repo.update_one(
                    {"concept_id": cid}, {"$set": {f"relationships.{rel_kind}": [curr]}}
                )
            else:
                # Nothing to remove; ensure array exists
                repo.update_one(
                    {"concept_id": cid}, {"$set": {f"relationships.{rel_kind}": []}}
                )
            repo.update_one(
                {"concept_id": cid},
                {"$pull": {f"relationships.{rel_kind}": rel_target}},
            )

        # Remove forward relation (normalized)
        _ensure_array_and_remove(source_id, kind, target_id)

        # Maintain inverse only for structural kinds
        if not is_dynamic_predicate_kind:
            inverse_map = {
                "is_a_type_of": ("has_subtype", target_id, source_id),
                "has_subtype": ("is_a_type_of", target_id, source_id),
                "is_an_instance_of": ("has_instance", target_id, source_id),
                "has_instance": ("is_an_instance_of", target_id, source_id),
                "related_to": ("related_to", target_id, source_id),
            }
            inv_kind, inv_src, inv_tgt = inverse_map[kind]
            _ensure_array_and_remove(inv_src, inv_kind, inv_tgt)

        try:
            from ...vontology.utils_vontology import invalidate_vontology_caches

            invalidate_vontology_caches(
                [source_id, target_id],
                correlation_id=str(uuid.uuid4()),
            )
        except Exception:
            current_app.logger.debug(
                "Relationship remove cache invalidation failed",
                exc_info=True,
            )

        return jsonify({"success": True, "message": "Relationship removed."}), 200
    except Exception as e:
        current_app.logger.error(f"Error removing relationship: {e}", exc_info=True)
        return (
            jsonify(
                {"success": False, "error": f"Failed to remove relationship: {str(e)}"}
            ),
            500,
        )


@vontology_bp.route("/concept/flag", methods=["POST"])
def toggle_concept_flag():
    """Toggle the flagged status of a concept for analysis purposes."""
    try:
        data = request.get_json()
        if not data or "concept_id" not in data:
            return jsonify({"success": False, "error": "concept_id is required"}), 400

        concept_id = data["concept_id"]
        flagged = data.get("flagged", True)  # Default to True if not specified
        from ...db.repositories.concepts_repository import ConceptsRepository

        # Update the concept with the flagged status
        result = ConceptsRepository.update_one(
            {"concept_id": concept_id}, {"$set": {"flagged": flagged}}
        )

        if result.matched_count == 0:
            return jsonify({"success": False, "error": "Concept not found"}), 404

        return (
            jsonify(
                {
                    "success": True,
                    "message": f"Concept {'flagged' if flagged else 'unflagged'} successfully",
                    "flagged": flagged,
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(f"Error toggling concept flag: {e}", exc_info=True)
        return (
            jsonify({"success": False, "error": f"Failed to toggle flag: {str(e)}"}),
            500,
        )


@vontology_bp.route("/concept/organization-relation", methods=["POST"])
def toggle_organization_relation():
    """Toggle the specific_to_organisation relationship for a concept."""
    try:
        data = request.get_json() or {}
        concept_id = data.get("concept_id")
        if not concept_id:
            return jsonify({"success": False, "error": "concept_id is required"}), 400

        # Client localStorage is sole authority for organisation context
        organisation_concept_id = (
            data.get("organisation_concept_id")
            or data.get("organization_concept_id")
            or data.get("organisation_id")
            or data.get("organization_id")
        )
        action = data.get("action", "add")
        # Administrative override for cleanup scenarios
        force = False
        raw_force = data.get("force")
        if isinstance(raw_force, bool):
            force = raw_force
        elif isinstance(raw_force, (int, float)):
            force = raw_force != 0
        elif isinstance(raw_force, str):
            force = raw_force.strip().lower() in ("1", "true", "yes", "y", "force")

        from ...db.repositories.concepts_repository import ConceptsRepository

        concept = ConceptsRepository.find_one({"concept_id": concept_id})
        if not concept:
            return jsonify({"success": False, "error": "Concept not found"}), 404

        relationships = concept.get("relationships") or {}
        org_relations = relationships.get("specific_to_organisation") or []
        if isinstance(org_relations, str):
            org_relations = [org_relations]
        elif not isinstance(org_relations, list):
            org_relations = []

        changed = False
        if action == "add":
            if not organisation_concept_id:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "organisation_concept_id required for add",
                            "action": action,
                        }
                    ),
                    400,
                )
            if organisation_concept_id not in org_relations:
                org_relations.append(organisation_concept_id)
                changed = True
        else:  # remove
            if force:
                if org_relations:
                    org_relations = []
                    changed = True
            else:
                if organisation_concept_id and organisation_concept_id in org_relations:
                    org_relations.remove(organisation_concept_id)
                    changed = True

        relationships["specific_to_organisation"] = org_relations
        ConceptsRepository.update_one(
            {"concept_id": concept_id}, {"$set": {"relationships": relationships}}
        )

        base_payload = {
            "action": action,
            "changed": changed,
            "specific_to_organisation": org_relations,
            "organisation_concept_id": organisation_concept_id,
            "forced": bool(force and action == "remove"),
        }
        if not changed:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "No change applied (nothing to update)",
                        **base_payload,
                    }
                ),
                200,
            )

        return jsonify({"success": True, **base_payload}), 200

    except Exception as e:
        current_app.logger.error(
            f"Error toggling organization relation: {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Failed to toggle organization relation: {str(e)}",
                }
            ),
            500,
        )


@vontology_bp.route("/concept/user-relation", methods=["POST"])
def toggle_user_relation():
    """Toggle the specific_to_user relationship for a concept."""
    try:
        data = request.get_json() or {}
        concept_id = data.get("concept_id")
        if not concept_id:
            return jsonify({"success": False, "error": "concept_id is required"}), 400

        # Client localStorage is sole authority for user context
        user_concept_id = data.get("user_concept_id") or data.get("user_id")
        # Optional administrative override for cleanup scenarios
        force = False
        raw_force = data.get("force")
        if isinstance(raw_force, bool):
            force = raw_force
        elif isinstance(raw_force, (int, float)):
            force = raw_force != 0
        elif isinstance(raw_force, str):
            force = raw_force.strip().lower() in ("1", "true", "yes", "y", "force")
        action = data.get("action", "add")

        from ...db.repositories.concepts_repository import ConceptsRepository

        concept = ConceptsRepository.find_one({"concept_id": concept_id})
        if not concept:
            return jsonify({"success": False, "error": "Concept not found"}), 404

        relationships = concept.get("relationships") or {}
        # Read from both legacy and predicate-style fields for current state
        from ...security.access_control import _get_specific_to_user_values

        user_relations = _get_specific_to_user_values(relationships)
        if not isinstance(user_relations, list):
            user_relations = list(user_relations) if user_relations else []

        changed = False
        if action == "add":
            if not user_concept_id:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "user_concept_id required for add",
                            "action": action,
                        }
                    ),
                    400,
                )
            if user_concept_id not in user_relations:
                user_relations.append(user_concept_id)
                changed = True
        else:  # remove
            if force:
                # Administrative force removal: clear ALL user-specific relations
                if user_relations:
                    user_relations = []
                    changed = True
            else:
                if user_concept_id and user_concept_id in user_relations:
                    user_relations.remove(user_concept_id)
                    changed = True

        relationships["specific_to_user"] = user_relations
        ConceptsRepository.update_one(
            {"concept_id": concept_id}, {"$set": {"relationships": relationships}}
        )

        base_payload = {
            "action": action,
            "changed": changed,
            "specific_to_user": user_relations,
            "user_concept_id": user_concept_id,
            "forced": bool(force and action == "remove"),
        }
        if not changed:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "No change applied (nothing to update)",
                        **base_payload,
                    }
                ),
                200,
            )

        return jsonify({"success": True, **base_payload}), 200

    except Exception as e:
        current_app.logger.error(f"Error toggling user relation: {e}", exc_info=True)
        return (
            jsonify(
                {"success": False, "error": f"Failed to toggle user relation: {str(e)}"}
            ),
            500,
        )


@vontology_bp.route("/concept/key-concept", methods=["POST"])
def toggle_key_concept():
    """Toggle key concept marking by adding/removing is_an_instance_of relationship."""
    try:
        data = request.get_json()
        concept_id = data.get("concept_id")
        user_concept_id = data.get("user_concept_id")
        action = data.get("action", "add")

        if not concept_id:
            return jsonify({"success": False, "error": "concept_id required"}), 400
        if not user_concept_id:
            return jsonify({"success": False, "error": "user_concept_id required"}), 400
        if action not in ["add", "remove"]:
            return (
                jsonify(
                    {"success": False, "error": "action must be 'add' or 'remove'"}
                ),
                400,
            )

        from ...db.repositories.concepts_repository import ConceptsRepository

        # Verify target concept exists
        target_concept = ConceptsRepository.find_one({"concept_id": concept_id})
        if not target_concept:
            return (
                jsonify(
                    {"success": False, "error": f"Concept '{concept_id}' not found"}
                ),
                404,
            )

        # Verify user concept exists
        user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
        if not user_concept:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"User concept '{user_concept_id}' not found",
                    }
                ),
                404,
            )

        if action == "add":
            # Add #V#vontologykeyconcept to is_an_instance_of relationship
            result = ConceptsRepository.update_one(
                {"concept_id": concept_id},
                {
                    "$addToSet": {
                        "relationships.is_an_instance_of": "#V#vontologykeyconcept"
                    }
                },
            )

            return (
                jsonify(
                    {
                        "success": True,
                        "action": "add",
                        "concept_id": concept_id,
                        "user_concept_id": user_concept_id,
                        "modified": result.modified_count > 0,
                    }
                ),
                200,
            )

        else:  # remove
            # Remove #V#vontologykeyconcept from is_an_instance_of relationship
            result = ConceptsRepository.update_one(
                {"concept_id": concept_id},
                {
                    "$pull": {
                        "relationships.is_an_instance_of": "#V#vontologykeyconcept"
                    }
                },
            )

            return (
                jsonify(
                    {
                        "success": True,
                        "action": "remove",
                        "concept_id": concept_id,
                        "user_concept_id": user_concept_id,
                        "modified": result.modified_count > 0,
                    }
                ),
                200,
            )

    except Exception as e:
        current_app.logger.error(f"Error toggling key concept: {e}", exc_info=True)
        return (
            jsonify(
                {"success": False, "error": f"Failed to toggle key concept: {str(e)}"}
            ),
            500,
        )


@vontology_bp.route("/key-concepts", methods=["GET"])
def get_key_concepts():
    """Get all concepts marked as key by a user (concepts with is_an_instance_of #V#vontologykeyconcept)."""
    try:
        user_concept_id = request.args.get("user_concept_id")

        if not user_concept_id:
            return jsonify({"success": False, "error": "user_concept_id required"}), 400

        from ...db.repositories.concepts_repository import ConceptsRepository

        # Find all concepts that are instances of #V#vontologykeyconcept
        # Note: For now, we return all such concepts regardless of user
        # Future enhancement: Add user-specific filtering via a relationship
        # Use projection to only fetch concept_id (avoids loading full documents)
        key_concepts = ConceptsRepository.find(
            {"relationships.is_an_instance_of": "#V#vontologykeyconcept"},
            projection={"concept_id": 1, "_id": 0},
        )

        # Extract concept IDs
        concept_ids = [
            doc.get("concept_id") for doc in key_concepts if doc.get("concept_id")
        ]

        return (
            jsonify(
                {
                    "success": True,
                    "user_concept_id": user_concept_id,
                    "concept_ids": concept_ids,
                    "count": len(concept_ids),
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(f"Error getting key concepts: {e}", exc_info=True)
        return (
            jsonify(
                {"success": False, "error": f"Failed to get key concepts: {str(e)}"}
            ),
            500,
        )


# ---------------- Propagation (subtree recompute) -----------------
def _recompute_inherited_for_subtree(
    repo, root_type_id: str, limit: int | None = None, dry_run: bool = False
):
    """Recompute inherited_salient_binary_predicates for a type subtree.

    Strategy:
      1. BFS descendants from root_type_id following relationships.is_a_type_of reverse edges.
      2. Collect type node documents discovered (bounded by limit if provided).
      3. For each type, recompute ordered union of its parents' inherited lists + its own direct salient list.
      4. Persist updates unless dry_run.
    Assumes ancestors of root_type_id already have correct inherited fields.
    Returns summary dict.
    """
    start_time = time.time()
    visited: set[str] = set()
    queue: list[str] = [root_type_id]
    order: list[str] = []

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        order.append(current)
        if limit and len(order) >= limit:
            break
        try:
            children_cursor = repo.find(
                {"relationships.is_a_type_of": current}, {"concept_id": 1}
            )
            for c in children_cursor:
                cid = c.get("concept_id")
                if isinstance(cid, str) and cid not in visited:
                    queue.append(cid)
        except Exception:
            current_app.logger.debug(
                "Failed fetching children for subtree propagation node %s",
                current,
                exc_info=True,
            )

    docs = list(
        repo.find(
            {"concept_id": {"$in": order}},
            {
                "concept_id": 1,
                "relationships": 1,
                "salient_binary_predicates_for_type": 1,
                "inherited_salient_binary_predicates": 1,
                SALIENT_SCOPE_FIELD: 1,
            },
        )
    )
    doc_by_id = {d.get("concept_id"): d for d in docs}

    updated: list[str] = []
    skipped_missing_parent: list[str] = []

    def get_direct(d):
        scope_data = extract_salient_scope_lists(d or {})
        instance_values = scope_data.get("instance") or []
        out: list[str] = []
        seen: set[str] = set()
        for value in instance_values:
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    def ordered_union(lists: list[list[str]]):
        out = []
        seen = set()
        for lst in lists:
            for x in lst:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
        return out

    for cid in order:
        d = doc_by_id.get(cid)
        if not d:
            continue
        rel = d.get("relationships") or {}
        parent_list = []
        top_level_parents = d.get("is_a_type_of") or []
        if isinstance(top_level_parents, str):
            top_level_parents = [top_level_parents]
        rel_parents = rel.get("is_a_type_of") or []
        if isinstance(rel_parents, str):
            rel_parents = [rel_parents]
        parent_list.extend([p for p in top_level_parents if isinstance(p, str)])
        parent_list.extend([p for p in rel_parents if isinstance(p, str)])
        parent_inherited_lists: list[list[str]] = []
        missing_parent = False
        for p in parent_list:
            pd = doc_by_id.get(p)
            if not pd:
                try:
                    pd_full = repo.find_one(
                        {"concept_id": p},
                        {
                            "inherited_salient_binary_predicates": 1,
                            "salient_binary_predicates_for_type": 1,
                        },
                    )
                except Exception:
                    pd_full = None
                if not pd_full:
                    missing_parent = True
                    continue
                parent_inherited_lists.append(
                    pd_full.get("inherited_salient_binary_predicates")
                    or get_direct(pd_full)
                )
            else:
                parent_inherited_lists.append(
                    pd.get("inherited_salient_binary_predicates") or []
                )
        if missing_parent:
            skipped_missing_parent.append(cid)
            continue
        recomputed = ordered_union(parent_inherited_lists + [get_direct(d)])
        existing = d.get("inherited_salient_binary_predicates") or []
        if recomputed != existing:
            updated.append(cid)
            if not dry_run:
                try:
                    repo.update_one(
                        {"concept_id": cid},
                        {"$set": {"inherited_salient_binary_predicates": recomputed}},
                    )
                except Exception:
                    current_app.logger.exception(
                        "Failed updating inherited salient for %s", cid
                    )

    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "root_type_id": root_type_id,
        "count_scanned": len(order),
        "updated": updated,
        "skipped_missing_parent": skipped_missing_parent,
        "duration_ms": duration_ms,
        "dry_run": dry_run,
    }


@vontology_bp.route("/predicates/salient/propagate", methods=["POST"])
def propagate_salient_subtree():
    """Trigger targeted recomputation of inherited salient predicates for a subtree.

    Body JSON: {"root_type_id": str, "limit": int (optional), "dry_run": bool (optional)}
    Clears in-memory salient cache entries (full flush) after recompute to avoid stale results.
    """
    payload = request.get_json(force=True, silent=True) or {}
    root_type_id = payload.get("root_type_id")
    if not root_type_id:
        return jsonify({"success": False, "error": "root_type_id required"}), 400
    limit = payload.get("limit")
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except Exception:
            limit = None
    dry_run = bool(payload.get("dry_run"))
    repo = current_app.config.get("concepts_repo") or ConceptsRepository
    try:
        summary = _recompute_inherited_for_subtree(
            repo, root_type_id, limit=limit, dry_run=dry_run
        )
    except Exception as e:
        current_app.logger.exception("Propagation failed")
        return jsonify({"success": False, "error": str(e)}), 500
    # Cache flush
    try:
        if "_SALIENT_CACHE" in globals():  # type: ignore[name-defined]
            _SALIENT_CACHE.clear()  # type: ignore[name-defined]
        cache_cleared = True
    except Exception:
        cache_cleared = False
    summary["cache_cleared"] = cache_cleared
    return jsonify({"success": True, "summary": summary}), 200
