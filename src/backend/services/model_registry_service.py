"""Model registry access for workflow-driven model routing."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from .settings_service import get_active_llm_setting

logger = logging.getLogger(__name__)


def _parse_registry_json(raw_text: str) -> Optional[Mapping[str, Any]]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    try:
        parsed = json.loads(raw_text)
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _load_registry_from_vontology(
    *, preferred_language: str | None = None
) -> Optional[Mapping[str, Any]]:
    try:
        from .concept_resolution_service import resolve_concept_by_name
        from .text_value_service import get_texts_for_concept
    except Exception:
        return None

    registry_name = "default_model_registry"
    predicate_name = "has_model_registry_json"

    registry_id = resolve_concept_by_name(
        name=registry_name,
        preferred_languages=[preferred_language] if preferred_language else None,
        match_code_strings=True,
    )
    if not isinstance(registry_id, Mapping) or registry_id.get("status") != "resolved":
        return None

    concept_id = registry_id.get("resolved_concept_id")
    if not isinstance(concept_id, str):
        return None

    predicate_resolution = resolve_concept_by_name(
        name=predicate_name,
        preferred_languages=[preferred_language] if preferred_language else None,
        match_code_strings=True,
    )
    if not isinstance(predicate_resolution, Mapping):
        return None

    predicate_id = predicate_resolution.get("resolved_concept_id")
    if not isinstance(predicate_id, str):
        return None

    try:
        rows = get_texts_for_concept(concept_id, predicate=predicate_id, limit=5)
    except Exception:
        return None

    if not isinstance(rows, list):
        return None

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_text = row.get("text")
        parsed = _parse_registry_json(raw_text if isinstance(raw_text, str) else "")
        if parsed is not None:
            return parsed

    return None


def _build_registry_from_settings() -> Mapping[str, Any]:
    active = get_active_llm_setting() or {}
    provider = active.get("provider") if isinstance(active, dict) else None
    model = active.get("model") if isinstance(active, dict) else None
    locality = "external"
    if isinstance(provider, str) and provider.lower() == "ollama":
        locality = "local"

    models = []
    if isinstance(model, str) and model.strip():
        models.append(
            {
                "model_id": model.strip(),
                "provider": provider or "unknown",
                "locality": locality,
                "source": "settings",
            }
        )

    return {
        "source": "settings",
        "models": models,
    }


def get_model_registry_snapshot(
    *, preferred_language: str | None = None
) -> Mapping[str, Any]:
    registry = _load_registry_from_vontology(preferred_language=preferred_language)
    if registry is not None:
        return {
            "source": "vontology",
            **registry,
        }

    return _build_registry_from_settings()
