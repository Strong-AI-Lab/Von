"""Shared helpers for model selection parameters.

Model parameters are transport metadata that travel with a selected model. The
policy deciding which model/parameter combination to prefer remains represented
in workflow or Vontology artefacts; these helpers only normalise, validate, and
map provider-compatible payloads.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

MODEL_PARAMETERS_KEY = "model_parameters"
MODEL_PARAMETER_REASONING_EFFORT = "reasoning_effort"
MODEL_PARAMETER_CAPABILITY_SCHEMA = "model_parameter_capabilities.v1"
OPENAI_REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")

_OPENAI_REASONING_EFFORT_API_MAPPINGS = {
    "responses": {
        "type": "nested_object",
        "path": ["reasoning", "effort"],
    },
    "chat_completions": {
        "type": "top_level",
        "key": "reasoning_effort",
    },
}

_MODEL_PARAMETER_ALIASES = {
    "reasoningeffort": MODEL_PARAMETER_REASONING_EFFORT,
    "reasoning_effort": MODEL_PARAMETER_REASONING_EFFORT,
    "reasoning.effort": MODEL_PARAMETER_REASONING_EFFORT,
}


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalise_provider(value: Any) -> str | None:
    text = _clean_text(value)
    return text.lower() if text else None


def _normalise_api_surface(value: Any) -> str:
    return (_clean_text(value) or "responses").lower()


def _normalise_parameter_key(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    token = text.replace("-", "_").replace(" ", "_")
    token = _MODEL_PARAMETER_ALIASES.get(token.replace("_", "").lower(), token)
    return _MODEL_PARAMETER_ALIASES.get(token.lower(), token)


def _json_safe_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=True))
    except (TypeError, ValueError):
        return None


def _extract_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _extract_model_parameters_payload(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    nested = raw.get(MODEL_PARAMETERS_KEY)
    if not isinstance(nested, Mapping):
        nested = raw.get("modelParameters")
    if isinstance(nested, Mapping):
        return nested
    return raw


def _normalise_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip().lower()
        for item in value
        if isinstance(item, str) and str(item).strip()
    ]


def _openai_reasoning_effort_supported(model: str | None) -> bool:
    model_id = _clean_text(model)
    if not model_id:
        return False
    lowered = model_id.lower()
    if lowered.startswith("openai:"):
        lowered = lowered.split(":", 1)[1].strip()
    return lowered.startswith(("gpt-5", "o1", "o3", "o4"))


def _parameter_capability(
    *,
    parameter_id: str,
    supported: bool,
    allowed_values: list[str],
    fixed_value: str | None,
    read_only: bool,
    source: str,
    sources: list[str] | None = None,
    provider_api_mapping: Mapping[str, Any] | None = None,
    registry_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capability: dict[str, Any] = {
        "parameter_id": parameter_id,
        "supported": supported,
        "allowed_values": allowed_values if supported else [],
        "fixed_value": fixed_value if supported else None,
        "read_only": read_only if supported else False,
        "source": source,
        "sources": sources or [source],
        "ui": {
            "control": "select",
            "empty_option_label": "provider default",
        },
        "provider_api_mapping": _json_safe_clone(provider_api_mapping or {}) or {},
        "cache_policy": {
            "provider_default_metadata": "in_process_static",
            "model_registry": "resolved_when_include_registry_is_true",
        },
    }
    if registry_policy:
        cloned_policy = _json_safe_clone(registry_policy)
        if isinstance(cloned_policy, Mapping):
            capability["registry_policy"] = cloned_policy
    return capability


def _provider_default_reasoning_effort_capability(
    *,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    supported = provider == "openai" and _openai_reasoning_effort_supported(model)
    return _parameter_capability(
        parameter_id=MODEL_PARAMETER_REASONING_EFFORT,
        supported=supported,
        allowed_values=list(OPENAI_REASONING_EFFORT_VALUES) if supported else [],
        fixed_value=None,
        read_only=False,
        source="provider_default_metadata" if supported else "unsupported",
        sources=["provider_default_metadata"] if supported else ["unsupported"],
        provider_api_mapping=(
            _OPENAI_REASONING_EFFORT_API_MAPPINGS if provider == "openai" else {}
        ),
    )


def _registry_parameter_policy(
    *,
    provider: str | None,
    model: str | None,
    parameter: str,
    api_surface: str | None,
    profile_concept_id: str | None = None,
) -> Mapping[str, Any] | None:
    if not model:
        return None
    try:
        from .model_registry_service import resolve_model_parameter_policy
    except Exception:
        return None
    try:
        return resolve_model_parameter_policy(
            model=model,
            provider=provider,
            parameter=parameter,
            api_surface=api_surface,
            profile_concept_id=profile_concept_id,
        )
    except Exception:
        return None


def build_model_parameter_capabilities(
    *,
    provider: str | None,
    model: str | None,
    api_surface: str | None = "responses",
    include_registry: bool = False,
    profile_concept_id: str | None = None,
) -> dict[str, Any]:
    provider_name = _normalise_provider(provider)
    model_id = _clean_text(model)
    surface = _normalise_api_surface(api_surface)
    capability: dict[str, Any] = {
        "schema": MODEL_PARAMETER_CAPABILITY_SCHEMA,
        "provider": provider_name,
        "model": model_id,
        "api_surface": surface,
        "parameters": {},
    }

    reasoning_capability = _provider_default_reasoning_effort_capability(
        provider=provider_name,
        model=model_id,
    )

    if include_registry:
        reasoning_policy = _registry_parameter_policy(
            provider=provider_name,
            model=model_id,
            parameter=MODEL_PARAMETER_REASONING_EFFORT,
            api_surface=surface,
            profile_concept_id=profile_concept_id,
        )
        if isinstance(reasoning_policy, Mapping):
            action = str(reasoning_policy.get("action") or "").strip().lower()
            allowed_values = _normalise_string_list(
                reasoning_policy.get("allowed_values")
            )
            fixed_value = _clean_text(reasoning_policy.get("fixed_value"))
            source = str(reasoning_policy.get("source") or "model_registry")
            supported = action != "omit"
            merged_allowed_values = (
                allowed_values
                if allowed_values
                else _normalise_string_list(reasoning_capability.get("allowed_values"))
            )
            reasoning_capability = _parameter_capability(
                parameter_id=MODEL_PARAMETER_REASONING_EFFORT,
                supported=supported,
                allowed_values=merged_allowed_values,
                fixed_value=fixed_value,
                read_only=bool(fixed_value),
                source=source,
                sources=[
                    *[
                        item
                        for item in reasoning_capability.get("sources", [])
                        if isinstance(item, str) and item
                    ],
                    source,
                ],
                provider_api_mapping=_extract_mapping(
                    reasoning_capability.get("provider_api_mapping")
                ),
                registry_policy=reasoning_policy,
            )

    capability["parameters"][MODEL_PARAMETER_REASONING_EFFORT] = reasoning_capability
    return capability


def _normalise_reasoning_effort(
    value: Any,
    *,
    provider: str | None,
    model: str | None,
    api_surface: str | None,
    include_registry: bool,
    profile_concept_id: str | None = None,
) -> str | None:
    effort = _clean_text(value)
    if not effort:
        return None
    effort = effort.lower()
    if not _normalise_provider(provider) and not _clean_text(model):
        return effort if effort in OPENAI_REASONING_EFFORT_VALUES else None
    capability = build_model_parameter_capabilities(
        provider=provider,
        model=model,
        api_surface=api_surface,
        include_registry=include_registry,
        profile_concept_id=profile_concept_id,
    )
    reasoning = _extract_mapping(capability.get("parameters")).get(
        MODEL_PARAMETER_REASONING_EFFORT
    )
    if not isinstance(reasoning, Mapping) or not reasoning.get("supported"):
        return None
    allowed = reasoning.get("allowed_values")
    allowed_values = set(_normalise_string_list(allowed))
    if not allowed_values:
        allowed_values = set(OPENAI_REASONING_EFFORT_VALUES)
    if effort not in allowed_values:
        return None
    fixed_value = _clean_text(reasoning.get("fixed_value"))
    return fixed_value.lower() if fixed_value else effort


def _apply_provider_mapping(
    kwargs: dict[str, Any],
    *,
    mapping: Mapping[str, Any],
    value: Any,
) -> None:
    mapping_type = str(mapping.get("type") or "").strip()
    if mapping_type == "top_level":
        key = _clean_text(mapping.get("key"))
        if key:
            kwargs[key] = value
        return

    if mapping_type == "nested_object":
        path = mapping.get("path")
        if not isinstance(path, list) or not path:
            return
        cursor: dict[str, Any] = kwargs
        for raw_key in path[:-1]:
            key = _clean_text(raw_key)
            if not key:
                return
            next_cursor = cursor.get(key)
            if not isinstance(next_cursor, dict):
                next_cursor = {}
                cursor[key] = next_cursor
            cursor = next_cursor
        leaf_key = _clean_text(path[-1])
        if leaf_key:
            cursor[leaf_key] = value


def provider_kwargs_from_model_parameters(
    raw: Any,
    *,
    provider: str | None,
    model: str | None,
    api_surface: str | None,
    include_registry: bool = True,
    profile_concept_id: str | None = None,
) -> dict[str, Any]:
    surface = _normalise_api_surface(api_surface)
    params = normalise_model_parameters_for_storage(
        raw,
        provider=provider,
        model=model,
        api_surface=surface,
        include_registry=include_registry,
        profile_concept_id=profile_concept_id,
    )
    if not params:
        return {}

    capabilities = build_model_parameter_capabilities(
        provider=provider,
        model=model,
        api_surface=surface,
        include_registry=include_registry,
        profile_concept_id=profile_concept_id,
    )
    capability_params = _extract_mapping(capabilities.get("parameters"))
    kwargs: dict[str, Any] = {}
    for parameter, value in params.items():
        parameter_capability = _extract_mapping(capability_params.get(parameter))
        if not parameter_capability.get("supported"):
            continue
        mappings = _extract_mapping(parameter_capability.get("provider_api_mapping"))
        mapping = _extract_mapping(mappings.get(surface))
        if not mapping:
            continue
        _apply_provider_mapping(kwargs, mapping=mapping, value=value)
    return kwargs


def normalise_model_parameters_for_storage(
    raw: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_surface: str | None = "responses",
    include_registry: bool = False,
    profile_concept_id: str | None = None,
) -> dict[str, Any]:
    payload = _extract_model_parameters_payload(raw)
    if not payload:
        return {}

    provider_name = _normalise_provider(provider)
    params: dict[str, Any] = {}
    for key, value in payload.items():
        canonical_key = _normalise_parameter_key(key)
        if not canonical_key:
            continue
        if canonical_key == "reasoning":
            continue
        if canonical_key == MODEL_PARAMETER_REASONING_EFFORT:
            continue
        cloned_value = _json_safe_clone(value)
        if cloned_value is not None:
            params[canonical_key] = cloned_value

    reasoning = payload.get(MODEL_PARAMETER_REASONING_EFFORT)
    if reasoning is None:
        reasoning = payload.get("reasoningEffort")
    nested_reasoning = payload.get("reasoning")
    if reasoning is None and isinstance(nested_reasoning, Mapping):
        reasoning = nested_reasoning.get("effort")
    if reasoning is not None:
        effort = _normalise_reasoning_effort(
            reasoning,
            provider=provider_name,
            model=model,
            api_surface=api_surface,
            include_registry=include_registry,
            profile_concept_id=profile_concept_id,
        )
        if effort:
            params[MODEL_PARAMETER_REASONING_EFFORT] = effort
    return params


def stable_model_parameters_key(
    value: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_surface: str | None = "responses",
    include_registry: bool = False,
) -> str:
    params = normalise_model_parameters_for_storage(
        value,
        provider=provider,
        model=model,
        api_surface=api_surface,
        include_registry=include_registry,
    )
    if not params:
        return ""
    return json.dumps(params, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def model_parameters_equal(left: Any, right: Any) -> bool:
    return stable_model_parameters_key(left) == stable_model_parameters_key(right)


def openai_responses_kwargs_from_model_parameters(
    raw: Any,
    *,
    model: str | None,
    profile_concept_id: str | None = None,
) -> dict[str, Any]:
    return provider_kwargs_from_model_parameters(
        raw,
        provider="openai",
        model=model,
        api_surface="responses",
        include_registry=True,
        profile_concept_id=profile_concept_id,
    )


def openai_chat_completions_kwargs_from_model_parameters(
    raw: Any,
    *,
    model: str | None,
    profile_concept_id: str | None = None,
) -> dict[str, Any]:
    return provider_kwargs_from_model_parameters(
        raw,
        provider="openai",
        model=model,
        api_surface="chat_completions",
        include_registry=True,
        profile_concept_id=profile_concept_id,
    )
