from flask import Blueprint, request, jsonify, current_app
from typing import Any, Dict

# Reverted to relative imports (original style) to satisfy editor heuristics
from ...models.annotation_schema import validate_annotation
from ...services import concept_service
from ...services.annotation_extraction_service import (
    extract_annotations,
    build_prompt_preview,
    build_prompt_preview_with_output,
    get_last_llm_io,
)
from ...db.mongo_client import get_text_relations_collection
from datetime import datetime, timezone

annotations_bp = Blueprint("annotations", __name__)


def _extract_user_context(data):
    """Extract user context from request payload for request-scoped identity.

    Returns dict with user_id, org_id, language. All fields optional.
    Frontend should send: {context: {user_id, org_id, language}} with each request.
    """
    ctx = data.get("context", {}) if isinstance(data, dict) else {}
    return {
        "user_id": ctx.get("user_id"),
        "org_id": ctx.get("org_id") or ctx.get("organisation_id"),
        "language": ctx.get("language") or ctx.get("preferred_language"),
    }


@annotations_bp.route("/prompt_preview", methods=["POST"])
def prompt_preview_route():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    include_output = bool(payload.get("include_output"))
    try:
        if include_output:
            data = build_prompt_preview_with_output(text)
            return jsonify({"status": "ok", **data}), 200
        else:
            preview = build_prompt_preview(text)
            return jsonify({"status": "ok", "preview": preview}), 200
    except Exception as e:
        current_app.logger.warning(f"Prompt preview error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 400


@annotations_bp.route("/turn", methods=["POST"])
def annotate_turn_route():
    """Receive a per-turn annotation payload and perform minimal processing.

    Expected path when registered: /api/annotations/turn
    """
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify(error="Request body must be JSON"), 400

    # Extract user context for request-scoped identity
    user_ctx = _extract_user_context(payload)

    err = validate_annotation(payload)
    if err:
        current_app.logger.debug(f"Annotation payload validation failed: {err}")
        return jsonify(error=err), 400

    # Log user context for visibility
    current_app.logger.debug(
        f"[annotations/turn] user={user_ctx.get('user_id')} org={user_ctx.get('org_id')} lang={user_ctx.get('language')} turn={payload.get('turn_id')}"
    )

    # Minimal acceptance behavior for now: validate and optionally call concept lookup/upsert
    response: Dict[str, Any] = {"status": "accepted", "suggestions": []}

    # If spans are provided, attempt to search for candidate concepts using concept_service
    spans = payload.get("spans") or []
    suggestions = []
    auto_generated = False
    metadata = payload.get("metadata") or {}

    def _norm_flag(val):
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes", "on")
        return False

    llm_flag = metadata.get("llm_enrich")
    llm_flag = (
        _norm_flag(llm_flag) if llm_flag is not None else None
    )  # None preserves auto-env behavior
    match_flag = metadata.get("match") if metadata else None
    heuristic_flag = (
        metadata.get("heuristic") if metadata else None
    )  # backward compatibility
    # Prefer explicit match flag; fall back to old heuristic flag; default True
    chosen_flag = match_flag if match_flag is not None else heuristic_flag
    match_enabled = _norm_flag(chosen_flag) if chosen_flag is not None else True
    if not spans:
        # Auto-extract spans if none provided
        try:
            extracted = extract_annotations(
                payload.get("text", ""),
                use_llm=llm_flag,
                use_match=match_enabled,
                return_timings=True,
            )
            if isinstance(extracted, tuple):
                spans, timings = extracted
            else:  # backward safety
                spans, timings = extracted, {}
            suggestions.extend(spans)
            if timings:
                response["timings"] = {k: round(v, 2) for k, v in timings.items()}
            auto_generated = True
            try:
                current_app.logger.debug(
                    f"annotations.auto_extraction turn_id={payload.get('turn_id')} spans={len(extracted)} text_len={len(payload.get('text',''))}"
                )
            except Exception:
                pass
            # Attach last LLM IO for transparency if LLM enabled (explicit or via env)
            try:
                if (llm_flag or (llm_flag is None)) and metadata.get(
                    "llm_enrich", True
                ):
                    io = get_last_llm_io()
                    if io and io.get("prompt") and io.get("output"):
                        out = io.get("output")
                        truncated = False
                        if isinstance(out, str) and len(out) > 12000:
                            out = out[:12000] + "... [truncated]"
                            truncated = True
                        response["llm_io"] = {
                            "prompt": io.get("prompt"),
                            "output": out,
                            "truncated": truncated,
                            "ts": io.get("ts"),
                        }
            except Exception:
                pass
        except Exception as e:
            current_app.logger.exception(f"Auto extraction failed: {e}")
    if not auto_generated and spans:
        try:
            for span in spans:
                text = span.get("text")
                if not text:
                    continue
                if hasattr(concept_service, "suggest_concepts_for_text"):
                    candidates = concept_service.suggest_concepts_for_text(text)
                elif hasattr(concept_service, "search_concepts"):
                    candidates = concept_service.search_concepts(text)
                else:
                    candidates = []
                try:
                    if isinstance(candidates, dict):
                        candidates = [candidates]
                    elif isinstance(candidates, str):
                        import json

                        try:
                            parsed = json.loads(candidates)
                            if isinstance(parsed, list):
                                candidates = parsed
                            elif isinstance(parsed, dict):
                                candidates = [parsed]
                            else:
                                candidates = []
                        except Exception:
                            candidates = []
                    elif not isinstance(candidates, list):
                        candidates = []
                except Exception as e:
                    current_app.logger.exception(
                        f"Error normalizing candidates for span '{text}': {e}"
                    )
                    candidates = []
                if not isinstance(candidates, list):
                    current_app.logger.debug(
                        f"annotations: unexpected candidates type {type(candidates)} for text={text}; coercing to list"
                    )
                    candidates = []
                suggestions.append({"span": span, "candidates": candidates})
            try:
                current_app.logger.debug(
                    f"annotations.manual turn_id={payload.get('turn_id')} spans={len(spans)} suggestions={len(suggestions)}"
                )
            except Exception:
                pass
        except Exception as e:
            current_app.logger.exception(
                f"Error calling concept service for suggestions: {e}"
            )

    response["suggestions"] = suggestions

    # Optionally, the caller can request auto-upsert via a flag in metadata
    if metadata.get("auto_upsert"):
        # For now, only attempt to upsert if a single span and no candidates
        try:
            for s in suggestions:
                if not s.get("candidates"):
                    span = s.get("span")
                    # Build a basic concept record
                    concept_payload = {
                        "name": span.get("text"),
                        "concept_id": None,
                        "vontology_path": metadata.get("vontology_path"),
                    }
                    if hasattr(concept_service, "create_concept"):
                        created = concept_service.create_concept(**concept_payload)
                        s["created"] = created
        except Exception as e:
            current_app.logger.exception(f"Error during auto_upsert: {e}")

    return jsonify(response), 200


@annotations_bp.route("/accept", methods=["POST"])
def accept_annotation_route():
    """Persist a selected candidate for a span/turn as a text relation.

    Expected JSON: { turn_id: str, span: {start, end, text}, candidate: {id|concept_id|name}, user_id?: str }
    """
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify(error="Request body must be JSON"), 400

    # Extract user context for request-scoped identity
    user_ctx = _extract_user_context(payload)

    # Basic validation
    turn_id = payload.get("turn_id")
    span = payload.get("span")
    candidate = payload.get("candidate")
    # Use context user_id if available, fallback to legacy user_id field
    user_id = user_ctx.get("user_id") or payload.get("user_id") or "unknown"
    if not turn_id or not span or not candidate:
        return jsonify(error="turn_id, span and candidate are required"), 400

    # Log context for audit trail
    current_app.logger.info(
        f"[annotations/accept] user={user_id} org={user_ctx.get('org_id')} turn={turn_id} concept={candidate.get('concept_id')}"
    )

    # Build a relation doc to store in text_relations collection
    try:
        coll = get_text_relations_collection()
        if coll is None:
            return jsonify(error="Database not available"), 500

        relation = {
            "subject_concept_id": candidate.get("concept_id")
            or candidate.get("id")
            or candidate.get("conceptId"),
            "predicate": "mentioned_in_text",
            "object_text": span.get("text"),
            "object_span": {"start": span.get("start"), "end": span.get("end")},
            "source": {
                "turn_id": turn_id,
                "user_id": user_id,
            },
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        coll.insert_one(relation)
    except Exception as e:
        current_app.logger.exception(f"Error persisting annotation accept: {e}")
        return jsonify(error="failed to persist annotation"), 500

    return jsonify({"status": "accepted"}), 200


@annotations_bp.route("/revoke", methods=["POST"])
def revoke_annotation_route():
    """Revoke previously accepted annotation(s) for a turn or object text.

    Expected JSON: { turn_id?: str, candidate_id?: str, object_text?: str }
    """
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify(error="Request body must be JSON"), 400

    turn_id = payload.get("turn_id")
    candidate_id = payload.get("candidate_id")
    object_text = payload.get("object_text")

    if not (turn_id or candidate_id or object_text):
        return (
            jsonify(
                error="must provide turn_id or candidate_id or object_text to revoke"
            ),
            400,
        )

    try:
        coll = get_text_relations_collection()
        if coll is None:
            return jsonify(error="Database not available"), 500

        query = {}
        if turn_id:
            query["source.turn_id"] = turn_id
        if candidate_id:
            query["subject_concept_id"] = str(candidate_id)
        if object_text and not candidate_id:
            # if object_text provided and no candidate specified, match by object_text
            query["object_text"] = object_text

        if not query:
            return jsonify(error="no valid filter for revoke"), 400

        result = coll.delete_many(query)
        return (
            jsonify({"status": "revoked", "deleted_count": result.deleted_count}),
            200,
        )
    except Exception as e:
        current_app.logger.exception(f"Error revoking annotation: {e}")
        return jsonify(error="failed to revoke annotation"), 500


@annotations_bp.route("/manual_instance", methods=["POST"])
def manual_instance_log_route():
    """Record a manual instance creation event from annotation UI (JVNAUTOSCI-552).

    Expected JSON: { span_text: str, instance_concept_id: str, parent_type_id: str, turn_id?: str }
    Stores as lightweight log line (no new collection) for now; future: persist for model training.
    """
    payload = request.get_json(silent=True) or {}
    span_text = (payload.get("span_text") or "").strip()
    instance_cid = (payload.get("instance_concept_id") or "").strip()
    parent_type = (payload.get("parent_type_id") or "").strip()
    turn_id = (payload.get("turn_id") or "").strip()
    if not (span_text and instance_cid and parent_type):
        return (
            jsonify(
                success=False,
                error="span_text, instance_concept_id, parent_type_id required",
            ),
            400,
        )
    try:
        current_app.logger.info(
            f"[manual_instance] turn={turn_id or '-'} span='{span_text[:80]}' instance={instance_cid} parent={parent_type}"
        )
    except Exception:
        pass
    return jsonify(success=True), 200


@annotations_bp.route("/manual_type", methods=["POST"])
def manual_type_log_route():
    """Record a manual type (subtype) creation event from annotation UI (JVNAUTOSCI-553).

    Expected JSON: { span_text: str, type_concept_id: str, parent_type_id: str, turn_id?: str }
    Mirrors manual_instance telemetry but for newly created TYPE concepts.
    """
    payload = request.get_json(silent=True) or {}
    span_text = (payload.get("span_text") or "").strip()
    type_cid = (payload.get("type_concept_id") or "").strip()
    parent_type = (payload.get("parent_type_id") or "").strip()
    turn_id = (payload.get("turn_id") or "").strip()
    if not (span_text and type_cid and parent_type):
        return (
            jsonify(
                success=False,
                error="span_text, type_concept_id, parent_type_id required",
            ),
            400,
        )
    try:
        current_app.logger.info(
            f"[manual_type] turn={turn_id or '-'} span='{span_text[:80]}' type={type_cid} parent={parent_type}"
        )
    except Exception:
        pass
    return jsonify(success=True), 200


@annotations_bp.route("/llm_history", methods=["GET"])
def llm_history_route():
    """Return the last recorded LLM prompt/output used for extraction (transparency helper).

    Path when registered: /api/annotations/llm_history
    Does NOT trigger a new LLM call; simply surfaces cached values.
    Large outputs are truncated (same policy as /turn embedding) to limit payload size.
    """
    try:
        io = get_last_llm_io()
        if not io or not (io.get("prompt") or io.get("output")):
            return jsonify({"status": "empty"}), 200
        out = io.get("output")
        truncated = False
        if isinstance(out, str) and len(out) > 12000:
            out = out[:12000] + "... [truncated]"
            truncated = True
        return (
            jsonify(
                {
                    "status": "ok",
                    "llm_io": {
                        "prompt": io.get("prompt"),
                        "output": out,
                        "truncated": truncated
                        or bool(io.get("truncated")),  # preserve original flag if set
                        "ts": io.get("ts"),
                    },
                }
            ),
            200,
        )
    except Exception as e:
        try:
            current_app.logger.warning(f"llm_history retrieval error: {e}")
        except Exception:
            pass
        return jsonify({"status": "error", "error": "failed to retrieve history"}), 500
