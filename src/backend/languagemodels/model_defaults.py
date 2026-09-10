"""Centralised default model names for all LLM providers.

All code that needs a fallback model name should import from here rather than
hardcoding a model string. Provider defaults are environment-overridable where
the integration supports a model portfolio; single-model provider contracts
remain fixed to their verified model ID.
"""

import os

# Audio/transcriptions models do not implement the chat/tool interface. They
# may be authorised in the shared model pool without becoming chat fallbacks.
TRANSCRIPTION_MODELS = ("gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe")


def is_transcription_model(provider: object, model: object) -> bool:
    return str(provider or "").strip().lower() == "openai" and str(
        model or ""
    ).strip().removeprefix("openai:") in TRANSCRIPTION_MODELS

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
DEFAULT_OLLAMA_MODEL: str = os.getenv("VON_DEFAULT_OLLAMA_MODEL", "gemma4:31b")

# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------
DEFAULT_OPENAI_MODEL: str = os.getenv("VON_DEFAULT_OPENAI_MODEL", "gpt-5.5")

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
DEFAULT_GEMINI_MODEL: str = os.getenv("VON_DEFAULT_GEMINI_MODEL", "gemini-3.7-flash")

# ---------------------------------------------------------------------------
# Meta Model API
# ---------------------------------------------------------------------------
# This bounded provider integration intentionally exposes only Muse Spark 1.3.
# Do not add a second configuration channel for the model ID: the represented
# model registry remains the durable catalogue and selection authority.
DEFAULT_META_MUSE_MODEL: str = "muse-spark-1.3"
META_MUSE_TEMPERATURE: float = 1.0
META_MUSE_TOP_P: float = 1.0
META_MUSE_MAX_OUTPUT_TOKENS: int = 32000
