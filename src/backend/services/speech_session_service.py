"""Actor-bound streaming media transport; no agent or tool authority lives here."""

from __future__ import annotations

import json
import os

import httpx

from ..languagemodels.llm_interface import assert_model_execution_allowed
from ..languagemodels.model_defaults import (
    LIVE_TRANSCRIPTION_MODELS,
    SPEECH_OUTPUT_MODELS,
)
from .settings_service import get_openai_env_var, resolve_enabled_llm_settings
from .speech_transcription_service import SpeechUnavailable, normalise_context


def media_model(actor, organisation, candidates):
    enabled = resolve_enabled_llm_settings(
        user_concept_id=actor, org_concept_id=organisation
    )
    names = {
        str(e.get("model", "")).removeprefix("openai:")
        for e in enabled
        if isinstance(e, dict) and e.get("provider") == "openai"
    }
    model = next((name for name in candidates if name in names), None)
    if not model:
        raise SpeechUnavailable(
            "Enable a speech model in Settings: " + ", ".join(candidates) + "."
        )
    if not os.environ.get(get_openai_env_var() or "OPENAI_API_KEY"):
        raise SpeechUnavailable("The server's configured OpenAI key is unavailable.")
    return model


def media_capabilities(actor, organisation):
    result = {}
    for kind, candidates in (
        ("streaming", LIVE_TRANSCRIPTION_MODELS),
        ("speech_output", SPEECH_OUTPUT_MODELS),
    ):
        try:
            result[kind] = {
                "available": True,
                "model": media_model(actor, organisation, candidates),
                "provider": "openai",
            }
        except SpeechUnavailable as exc:
            result[kind] = {"available": False, "reason": str(exc)}
    return result


def _authorise(actor, organisation, candidates):
    model = media_model(actor, organisation, candidates)
    assert_model_execution_allowed(
        provider="openai",
        model=model,
        user_concept_id=actor,
        org_concept_id=organisation,
        allow_ambient_actor_scope=False,
    )
    return model


def create_transcription_connection(
    *, actor, organisation, sdp, context="", vocabulary=None, language=""
):
    # The server selects session type and model; the client cannot mint a general
    # realtime agent credential or choose tools/instructions for an audio model.
    if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > 65536:
        raise ValueError("A bounded WebRTC offer is required.")
    model = _authorise(actor, organisation, LIVE_TRANSCRIPTION_MODELS)
    context, terms = normalise_context(context, vocabulary)
    from .speech_transcription_service import normalise_language

    language = normalise_language(language)
    transcription = {
        "model": model,
        "prompt": context,
        "keywords": terms,
        "delay": "low",
    }
    if language:
        transcription["languages"] = [language]
    config = {
        "type": "transcription",
        "audio": {
            "input": {
                "transcription": transcription,
                "noise_reduction": {"type": "near_field"},
                # gpt-live-transcribe rejects server-side turn detection.
                # The browser commits pauses and explicit Finish actions.
                "turn_detection": None,
            }
        },
    }
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.post(
            "https://api.openai.com/v1/realtime/calls",
            headers={
                "Authorization": "Bearer "
                + os.environ[get_openai_env_var() or "OPENAI_API_KEY"]
            },
            files={"sdp": (None, sdp), "session": (None, json.dumps(config))},
        )
        response.raise_for_status()
        answer = response.text
    if not answer.startswith("v=0") or len(answer) > 65536:
        raise ValueError("The speech provider returned an invalid connection answer.")
    return {
        "sdp": answer,
        "model": model,
        "provider": "openai",
        "vocabulary_count": len(terms),
        "context_chars": len(context),
    }


def open_spoken_audio(*, actor, organisation, text):
    """Open before Flask sends headers, then close on completion/disconnect."""
    import openai

    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("Spoken text must contain 1–4096 characters.")
    model = _authorise(actor, organisation, SPEECH_OUTPUT_MODELS)
    client = openai.OpenAI(
        api_key=os.environ[get_openai_env_var() or "OPENAI_API_KEY"],
        timeout=120,
        max_retries=0,
    )
    manager = client.audio.speech.with_streaming_response.create(
        model=model,
        voice="alloy" if model == "tts-1" else "coral",
        input=text,
        response_format="pcm",
    )
    try:
        response = manager.__enter__()
    except BaseException:
        client.close()
        raise

    closed = False

    def close():
        nonlocal closed
        if not closed:
            closed = True
            try:
                manager.__exit__(None, None, None)
            finally:
                client.close()

    def chunks():
        try:
            yield from response.iter_bytes(chunk_size=8192)
        finally:
            close()

    return chunks(), model, close
