"""Bounded audio transport. Context is supplied from the visible composer;
it is transcription evidence, never an instruction or authority to use tools.
No recording or context is persisted by this service.
"""

from __future__ import annotations

import os
import re
import time

from ..languagemodels.llm_interface import assert_model_execution_allowed
from ..languagemodels.model_defaults import TRANSCRIPTION_MODELS
from .settings_service import get_openai_env_var, resolve_enabled_llm_settings
from .model_audio_metadata import recorded_transcription_supported

MAX_AUDIO_BYTES = 24_000_000
AUDIO_TYPES = {
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


class SpeechUnavailable(Exception):
    pass


def transcription_model(
    actor: str, organisation: str | None, requested_model=""
) -> str:
    enabled = resolve_enabled_llm_settings(
        user_concept_id=actor, org_concept_id=organisation
    )
    models = {
        str(e.get("model", "")).removeprefix("openai:")
        for e in enabled
        if isinstance(e, dict) and e.get("provider") == "openai"
    }
    preferred = str(
        requested_model or os.environ.get("VON_TRANSCRIPTION_MODEL", "")
    ).strip()
    candidates = (preferred,) if preferred else (*TRANSCRIPTION_MODELS, *sorted(models))
    for model in candidates:
        if model in models and recorded_transcription_supported("openai", model):
            return model
    raise SpeechUnavailable(
        (
            f"OpenAI {preferred} is not an enabled recorded-transcription model. "
            if preferred
            else ""
        )
        + "Enable an OpenAI transcription model in Settings: "
        + ", ".join(TRANSCRIPTION_MODELS)
        + "."
    )


def transcription_capability(
    actor: str, organisation: str | None, requested_model=""
) -> dict:
    model = transcription_model(actor, organisation, requested_model)
    if not os.environ.get(get_openai_env_var() or "OPENAI_API_KEY"):
        raise SpeechUnavailable("The server's configured OpenAI key is unavailable.")
    return {
        "available": True,
        "provider": "openai",
        "model": model,
        "max_audio_bytes": MAX_AUDIO_BYTES,
        "mime_types": list(AUDIO_TYPES),
    }


def normalise_context(context, vocabulary) -> tuple[str, list[str]]:
    context = context[-6000:] if isinstance(context, str) else ""
    terms = []
    for value in vocabulary[:80] if isinstance(vocabulary, list) else []:
        if not isinstance(value, str):
            continue
        term = re.sub(r"[<>\r\n]", " ", value).strip()[:100]
        if term and term.casefold() not in {t.casefold() for t in terms}:
            terms.append(term)
    return context, terms


def normalise_language(language):
    language = language.strip() if isinstance(language, str) else ""
    if language and not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", language):
        raise ValueError(
            "Use a language tag such as en-NZ, mi or zh-CN, or leave it empty."
        )
    return language.split("-", 1)[0].lower()


def transcribe_audio(
    *,
    actor,
    organisation,
    audio,
    mime_type,
    context="",
    vocabulary=None,
    language="",
    model="",
):
    import openai

    mime_type = mime_type.split(";", 1)[0].lower().strip()
    if mime_type not in AUDIO_TYPES:
        raise ValueError("Unsupported recording format. Use MP4, WebM, MP3 or WAV.")
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("Recording is empty or exceeds the 24 MB upload limit.")
    capability = transcription_capability(actor, organisation, model)
    model = capability["model"]
    assert_model_execution_allowed(
        provider="openai",
        model=model,
        user_concept_id=actor,
        org_concept_id=organisation,
        allow_ambient_actor_scope=False,
    )
    context, terms = normalise_context(context, vocabulary)
    # Only visible context and literal spellings; no corrective LLM pass that
    # could silently rewrite the user's intended message.
    prompt = "\n".join(filter(None, [", ".join(terms), context]))[:8000]
    options = {
        "model": model,
        "file": ("dictation." + AUDIO_TYPES[mime_type], audio, mime_type),
        "response_format": "json",
        "prompt": prompt,
    }
    language = language.strip() if isinstance(language, str) else ""
    provider_language = normalise_language(language)
    if model == "gpt-transcribe":
        options["extra_body"] = {
            "keywords": terms,
            # The provider accepts language codes, not browser locales: a live
            # en-NZ request is rejected while the otherwise identical en works.
            **({"languages": [provider_language]} if provider_language else {}),
        }
    elif language:
        options["language"] = provider_language
    start = time.monotonic()
    # Explicit transport bound protects occupied HTTP workers; no client timer
    # discards a usable result. Users can cancel or retry the retained recording.
    with openai.OpenAI(
        api_key=os.environ[get_openai_env_var() or "OPENAI_API_KEY"],
        timeout=120,
        max_retries=0,
    ) as client:
        result = client.audio.transcriptions.create(**options)
    text = result.text.strip()
    return {
        "text": text,
        "provider": "openai",
        "model": model,
        "language_hint": language or None,
        "context_chars": len(context),
        "vocabulary_count": len(terms),
        "audio_bytes": len(audio),
        "elapsed_ms": round((time.monotonic() - start) * 1000),
    }
