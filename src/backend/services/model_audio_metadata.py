"""Recorded-audio transport metadata, supplemented by the represented registry.

The compatibility entries describe existing transport support, not permission or
provider availability. Registry capabilities.audio_transcription may extend or
disable that support; only the implemented OpenAI file transport is eligible.
"""

from ..languagemodels.model_defaults import TRANSCRIPTION_MODELS


def recorded_transcription_supported(provider, model, *, registry_models=None):
    provider = str(provider or "").lower()
    model = str(model or "").removeprefix(provider + ":")
    supported = provider == "openai" and model in TRANSCRIPTION_MODELS
    if registry_models is None:
        from .model_registry_service import get_model_registry_snapshot

        registry_models = get_model_registry_snapshot().get("models", [])
    for entry in registry_models:
        if entry.get("provider") != provider:
            continue
        names = [entry.get("model_id"), *(entry.get("model_aliases") or [])]
        if model not in names and provider + ":" + model not in names:
            continue
        capability = (entry.get("capabilities") or {}).get("audio_transcription")
        if isinstance(capability, bool):
            supported = capability
            break
    return provider == "openai" and supported
