"""Mechanical output affordances and profile-controlled native tool projection.

Generation availability is distinct from rendering an already-authorised result.
The model registry owns the optional tool configuration; no model-name heuristic
or production-setting write enables paid generation here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def native_image_tool(decision: Any) -> dict[str, Any] | None:
    if decision.provider != "openai" or decision.effective_api_surface != "responses":
        return None
    from .model_registry_service import resolve_model_api_profiles

    resolved = resolve_model_api_profiles(
        model=decision.model, provider=decision.provider
    )
    for profile in (resolved or {}).get("api_profiles", []):
        if (
            not decision.profile_concept_id
            or profile.get("profile_concept_id") != decision.profile_concept_id
        ):
            continue
        config = profile.get("image_generation")
        if not isinstance(config, Mapping) or config.get("enabled") is not True:
            return None
        model = config.get("model")
        if not isinstance(model, str) or not model.strip():
            return None
        # Only documented tool options are projected, never arbitrary SDK kwargs.
        tool: dict[str, Any] = {"type": "image_generation", "model": model}
        choices = {
            "quality": {"auto", "low", "medium", "high"},
            "size": {"auto", "1024x1024", "1024x1536", "1536x1024"},
            "output_format": {"png", "jpeg", "webp"},
        }
        for key, allowed in choices.items():
            value = config.get(key)
            if isinstance(value, str) and value in allowed:
                tool[key] = value
        return tool
    return None


def output_affordances() -> dict[str, Any]:
    return {
        "schema_version": "conversation_output_affordances.v1",
        "display_contract": "turn_display_elements_v1",
        "formats": [
            {
                "id": "mermaid",
                "version": 1,
                "source": "fenced mermaid",
                "fallback": "source",
            },
            {
                "id": "latex",
                "version": 1,
                "source": r"\(inline\) or \[display\]; dollars are text",
                "fallback": "source",
            },
            {
                "id": "image",
                "version": 1,
                "source": "retained actor-scoped asset",
                "fallback": "unavailable image notice",
            },
        ],
        "generation": "Native generation is available only when advertised in provider tools; rendering support does not establish generation or vision support.",
    }
