from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_SESSION_KEY = "client_capabilities_snapshot"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_int(value: Any, *, minimum: int = 0, maximum: int = 10_000) -> int:
    try:
        parsed = int(value)
    except Exception:
        return minimum

    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _normalise_bool(value: Any) -> bool:
    return bool(value)


def _truncate_str(value: Any, *, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars]


def _normalise_lang(value: Any) -> str | None:
    text = _truncate_str(value, max_chars=32)
    if not text:
        return None
    # Keep it permissive: BCP 47-ish; don't over-validate.
    if not re.match(r"^[A-Za-z]{2,3}([\-][A-Za-z0-9]{2,8})*$", text):
        return None
    return text


def _summarise_user_agent(user_agent: str | None) -> dict[str, str | int | None]:
    ua = (user_agent or "").strip()

    def _major(pattern: str) -> int | None:
        match = re.search(pattern, ua)
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    # Order matters.
    if "Edg/" in ua:
        return {"browser_family": "Edge", "major_version": _major(r"Edg/(\d+)")}
    if "Chrome/" in ua and "Chromium" not in ua:
        return {"browser_family": "Chrome", "major_version": _major(r"Chrome/(\d+)")}
    if "Firefox/" in ua:
        return {"browser_family": "Firefox", "major_version": _major(r"Firefox/(\d+)")}
    if "Safari/" in ua and "Chrome/" not in ua:
        return {"browser_family": "Safari", "major_version": _major(r"Version/(\d+)")}

    return {"browser_family": "Other", "major_version": None}


def sanitise_client_capabilities(
    raw: Any,
    *,
    user_agent: str | None = None,
    received_at_utc: str | None = None,
) -> dict[str, Any]:
    """Return a safe, bounded snapshot of client-reported capabilities.

    Security notes:
    - Treat everything as client-reported and non-authoritative.
    - Never store or return secrets.
    - Cap list sizes and string lengths to prevent session bloat.
    """

    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}

    snapshot: dict[str, Any] = {
        "kind": "client_capabilities",
        "client_reported": True,
        "received_at_utc": received_at_utc or _utc_now_iso(),
        "client_reported_timestamp": _truncate_str(
            payload.get("client_reported_timestamp"), max_chars=64
        )
        or None,
        "user_agent_summary": _summarise_user_agent(user_agent),
    }

    speech_raw = payload.get("speech_synthesis")
    speech: dict[str, Any] = speech_raw if isinstance(speech_raw, dict) else {}

    voices_sample_raw = speech.get("voices_sample")
    voices_sample_in = voices_sample_raw if isinstance(voices_sample_raw, list) else []
    voices_sample_out: list[dict[str, Any]] = []

    for item in voices_sample_in[:8]:
        voice = item if isinstance(item, dict) else {}
        name = _truncate_str(voice.get("name"), max_chars=120)
        lang = _normalise_lang(voice.get("lang"))
        local_service = voice.get("localService")

        voices_sample_out.append(
            {
                "name": name or None,
                "lang": lang,
                "localService": _normalise_bool(local_service),
            }
        )

    default_voice_lang = _normalise_lang(speech.get("default_voice_lang"))

    snapshot["speech_synthesis"] = {
        "supported": _normalise_bool(speech.get("supported")),
        "voices_count": _clamp_int(speech.get("voices_count"), minimum=0, maximum=5000),
        "voices_sample": voices_sample_out,
        "default_voice_lang": default_voice_lang,
        "last_error": _truncate_str(speech.get("last_error"), max_chars=200) or None,
        "settings": {
            "rate": (
                float(speech.get("settings", {}).get("rate"))
                if isinstance(speech.get("settings"), dict)
                and isinstance(speech.get("settings", {}).get("rate"), (int, float))
                else None
            ),
            "pitch": (
                float(speech.get("settings", {}).get("pitch"))
                if isinstance(speech.get("settings"), dict)
                and isinstance(speech.get("settings", {}).get("pitch"), (int, float))
                else None
            ),
            "volume": (
                float(speech.get("settings", {}).get("volume"))
                if isinstance(speech.get("settings"), dict)
                and isinstance(speech.get("settings", {}).get("volume"), (int, float))
                else None
            ),
            "voice_name": _truncate_str(
                (
                    speech.get("settings", {})
                    if isinstance(speech.get("settings"), dict)
                    else {}
                ).get("voice_name"),
                max_chars=120,
            )
            or None,
        },
    }

    audio_raw = payload.get("audio_output")
    audio: dict[str, Any] = audio_raw if isinstance(audio_raw, dict) else {}
    snapshot["audio_output"] = {
        "document_muted": (
            bool(audio.get("document_muted"))
            if audio.get("document_muted") is not None
            else None
        ),
        "autoplay_policy_hint": _truncate_str(
            audio.get("autoplay_policy_hint"), max_chars=40
        )
        or None,
        "user_activation": (
            bool(audio.get("user_activation"))
            if audio.get("user_activation") is not None
            else None
        ),
    }

    other_raw = payload.get("other")
    other: dict[str, Any] = other_raw if isinstance(other_raw, dict) else {}
    snapshot["other"] = {
        "speech_recognition_supported": (
            bool(other.get("speech_recognition_supported"))
            if other.get("speech_recognition_supported") is not None
            else None
        ),
        "visibility_state": _truncate_str(other.get("visibility_state"), max_chars=30)
        or None,
    }

    return snapshot


def set_client_capabilities_snapshot(snapshot: dict[str, Any]) -> None:
    from flask import has_request_context, session

    if not has_request_context():
        raise RuntimeError("No request context available")

    session[_SESSION_KEY] = snapshot


def get_client_capabilities_snapshot() -> dict[str, Any] | None:
    from flask import has_request_context, session

    if not has_request_context():
        return None

    value = session.get(_SESSION_KEY)
    return value if isinstance(value, dict) else None
