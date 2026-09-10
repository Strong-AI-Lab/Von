from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, current_app, jsonify, request, session

from ...services.chat_history_service import (
    ChatHistoryServiceError,
    update_llm_debug_data_for_request_id,
)

speech_bp = Blueprint("speech_bp", __name__, url_prefix="/api/speech")


def _speech_actor():
    from ...security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id_with_source,
    )

    actor, source = get_effective_user_concept_id_with_source()
    if not actor or source not in {
        "authenticated_session",
        "authenticated_session_derived",
        "trusted_in_process_actor",
    }:
        return None, None
    return actor, get_effective_organisation_concept_id()


@speech_bp.route("/capabilities", methods=["GET"])
def speech_capabilities():
    from ...services.speech_transcription_service import (
        SpeechUnavailable,
        transcription_capability,
    )

    actor, organisation = _speech_actor()
    if not actor:
        return jsonify(error="authentication_required"), 401
    try:
        data = transcription_capability(actor, organisation)
    except SpeechUnavailable as exc:
        data = {"available": False, "reason": str(exc)}
    except Exception:
        data = {
            "available": False,
            "reason": "Speech configuration is temporarily unavailable.",
        }
    response = jsonify(transcription=data)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@speech_bp.route("/transcribe", methods=["POST"])
def transcribe_speech():
    from ...languagemodels.llm_interface import ModelExecutionEligibilityError
    from ...services.speech_transcription_service import (
        MAX_AUDIO_BYTES,
        SpeechUnavailable,
        transcribe_audio,
    )

    actor, organisation = _speech_actor()
    if not actor:
        return jsonify(error="authentication_required"), 401
    # Bound multipart parsing as well as the file read; audio is never retained.
    request.max_content_length = MAX_AUDIO_BYTES + 65536
    uploaded = request.files.get("audio")
    if uploaded is None:
        return jsonify(error="missing_audio", message="No recording was received."), 400
    try:
        vocabulary = json.loads(request.form.get("vocabulary", "[]"))
        result = transcribe_audio(
            actor=actor,
            organisation=organisation,
            audio=uploaded.read(MAX_AUDIO_BYTES + 1),
            mime_type=uploaded.mimetype or "",
            context=request.form.get("context", ""),
            vocabulary=vocabulary,
            language=request.form.get("language", ""),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return jsonify(error="invalid_audio_request", message=str(exc)), 400
    except (SpeechUnavailable, ModelExecutionEligibilityError) as exc:
        return jsonify(error="speech_unavailable", message=str(exc)), 503
    except Exception as exc:
        # Provider exceptions can contain credentials, URLs and submitted text.
        current_app.logger.warning(
            "Speech transcription failed (%s)", type(exc).__name__
        )
        return (
            jsonify(
                error="transcription_failed",
                message="Transcription failed. Your recording is available to retry.",
            ),
            502,
        )
    response = jsonify(success=True, **result)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except Exception:
            return None
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except Exception:
            return None
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return None


def _coerce_text(value: Any, *, max_chars: int = 120) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_chars:
        return cleaned[:max_chars]
    return cleaned


def _normalise_timestamp(value: Any) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        if value:
            return value
    if isinstance(value, (int, float)):
        try:
            return (
                datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:
            return None
    return None


def _sanitise_playback(payload: Dict[str, Any]) -> Dict[str, Any]:
    playback = payload.get("speech_playback") or {}
    if not isinstance(playback, dict):
        return {}

    settings = playback.get("tts_settings") or {}
    settings = settings if isinstance(settings, dict) else {}

    return {
        "request_id": _coerce_text(
            payload.get("request_id") or playback.get("request_id")
        ),
        "turn_id": _coerce_text(payload.get("turn_id")),
        "conversation_id": _coerce_text(payload.get("conversation_id")),
        "started_at": _normalise_timestamp(playback.get("started_at")),
        "ended_at": _normalise_timestamp(playback.get("ended_at")),
        "actual_duration_ms": _coerce_int(playback.get("actual_duration_ms")),
        "expected_duration_ms": _coerce_int(playback.get("expected_duration_ms")),
        "estimate_method": _coerce_text(playback.get("estimate_method")),
        "duration_threshold_sec": _coerce_float(playback.get("duration_threshold_sec")),
        "duration_suspect_too_long": _coerce_bool(
            playback.get("duration_suspect_too_long")
        ),
        "tts_source": _coerce_text(playback.get("tts_source")),
        "tts_chars": _coerce_int(playback.get("tts_chars")),
        "tts_words": _coerce_int(playback.get("tts_words")),
        "stop_reason": _coerce_text(playback.get("stop_reason")),
        "tts_settings": {
            "language": _coerce_text(settings.get("language")),
            "rate": _coerce_float(settings.get("rate")),
            "pitch": _coerce_float(settings.get("pitch")),
            "volume": _coerce_float(settings.get("volume")),
            "voice_uri": _coerce_text(settings.get("voice_uri"), max_chars=200),
            "voice_name": _coerce_text(settings.get("voice_name"), max_chars=200),
        },
    }


@speech_bp.route("/telemetry", methods=["POST"])
def record_speech_telemetry():
    payload = request.get_json(silent=True) or {}

    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "payload_invalid"}), 400

    playback = _sanitise_playback(payload)
    if not playback:
        return jsonify({"success": False, "error": "missing_speech_playback"}), 400

    suspect = bool(playback.get("duration_suspect_too_long"))
    log_fn = current_app.logger.warning if suspect else current_app.logger.info

    try:
        log_fn(
            "[speech_telemetry] request_id=%s turn=%s duration_ms=%s expected_ms=%s threshold_sec=%s suspect=%s source=%s voice=%s rate=%s pitch=%s volume=%s start=%s end=%s",
            playback.get("request_id"),
            playback.get("turn_id"),
            playback.get("actual_duration_ms"),
            playback.get("expected_duration_ms"),
            playback.get("duration_threshold_sec"),
            playback.get("duration_suspect_too_long"),
            playback.get("tts_source"),
            (playback.get("tts_settings") or {}).get("voice_name"),
            (playback.get("tts_settings") or {}).get("rate"),
            (playback.get("tts_settings") or {}).get("pitch"),
            (playback.get("tts_settings") or {}).get("volume"),
            playback.get("started_at"),
            playback.get("ended_at"),
        )
    except Exception:
        pass

    # Best-effort: attach telemetry to the stored llm_debug_data entry.
    user_concept_id = session.get("user_concept_id")
    session_id = session.get("session_id")
    request_id = playback.get("request_id")

    updated = False
    update_reason = None
    if (
        isinstance(user_concept_id, str)
        and user_concept_id.strip()
        and isinstance(session_id, str)
        and session_id.strip()
        and isinstance(request_id, str)
        and request_id.strip()
    ):
        try:
            update_result = update_llm_debug_data_for_request_id(
                user_id=user_concept_id.strip(),
                session_id=session_id.strip(),
                request_id=request_id.strip(),
                updates={
                    "speech_playback": playback,
                    "speech_playback_updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
            updated = bool(update_result.get("updated"))
            update_reason = update_result.get("reason")
        except ChatHistoryServiceError as exc:
            update_reason = str(exc)
        except Exception as exc:
            update_reason = str(exc)

    return jsonify(
        {
            "success": True,
            "stored": playback,
            "history_update": {"updated": updated, "reason": update_reason},
        }
    )
