"""Resolve workflow-authored prompt metadata from Vontology concepts.

The workflow runtime needs a deterministic, auditable way to attach prompt
configuration to prompt-bearing workflow steps without drifting into bespoke
Python orchestration. This module resolves:

- which prompt concept is authoritative for a step,
- prompt metadata declared directly on that prompt concept,
- agent-profile defaults linked from the prompt,
- caller-supplied defaults,
- and warn/fail diagnostics for unavailable tools or agent profiles.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.prompt_template_service import PromptTemplateService
from ..services.text_value_service import get_texts_for_concept
from ..vontology.code_concepts_registry import is_code_concept_id

logger = logging.getLogger(__name__)

PROMPT_VALIDATION_POLICY_WARN = "warn"
PROMPT_VALIDATION_POLICY_FAIL = "fail"
PROMPT_VALIDATION_POLICIES: tuple[str, ...] = (
    PROMPT_VALIDATION_POLICY_WARN,
    PROMPT_VALIDATION_POLICY_FAIL,
)

WORKFLOW_STEP_PROMPT_LINK_PREDICATE_ALIASES: tuple[str, ...] = (
    "#V#workflow_step_uses_llm_prompt",
    "workflow_step_uses_llm_prompt",
    "#V#workflowStepUsesLlmPrompt",
    "workflowStepUsesLlmPrompt",
    "#V#uses_prompt",
    "uses_prompt",
    "#V#hasPromptTemplate",
    "hasPromptTemplate",
    "#V#has_prompt_template",
    "has_prompt_template",
)

_PROMPT_NAME_PREDICATES: tuple[str, ...] = (
    "#V#hasPromptName",
    "hasPromptName",
    "#V#has_prompt_name",
    "has_prompt_name",
    "#V#hasName",
    "hasName",
    "has_name",
)
_PROMPT_DESCRIPTION_PREDICATES: tuple[str, ...] = (
    "#V#hasPromptDescription",
    "hasPromptDescription",
    "#V#has_prompt_description",
    "has_prompt_description",
    "#V#hasDescription",
    "hasDescription",
    "has_description",
)
_ARGUMENT_HINT_PREDICATES: tuple[str, ...] = (
    "#V#hasArgumentHint",
    "hasArgumentHint",
    "#V#has_argument_hint",
    "has_argument_hint",
)
_AGENT_PROFILE_PREDICATES: tuple[str, ...] = (
    "#V#usesAgentProfile",
    "usesAgentProfile",
    "#V#uses_agent_profile",
    "uses_agent_profile",
)
_MODEL_PREFERENCE_PREDICATES: tuple[str, ...] = (
    "#V#usesModelPreference",
    "usesModelPreference",
    "#V#uses_model_preference",
    "uses_model_preference",
    "#V#uses_llm_model",
    "uses_llm_model",
)
_MODEL_PROMPT_VARIANT_PREDICATES: tuple[str, ...] = (
    "#V#hasModelPromptVariant",
    "hasModelPromptVariant",
    "#V#has_model_prompt_variant",
    "has_model_prompt_variant",
    "#V#hasModelSpecificPromptVariant",
    "hasModelSpecificPromptVariant",
    "#V#has_model_specific_prompt_variant",
    "has_model_specific_prompt_variant",
    "#V#hasPromptVariant",
    "hasPromptVariant",
    "#V#has_prompt_variant",
    "has_prompt_variant",
)
_MODEL_PROMPT_VARIANT_MODEL_PREDICATES: tuple[str, ...] = (
    "#V#forModel",
    "forModel",
    "#V#for_model",
    "for_model",
    "#V#targetsModel",
    "targetsModel",
    "#V#targets_model",
    "targets_model",
    "#V#matchesModel",
    "matchesModel",
    "#V#matches_model",
    "matches_model",
)
_MODEL_PROMPT_VARIANT_FAMILY_PREDICATES: tuple[str, ...] = (
    "#V#forModelFamily",
    "forModelFamily",
    "#V#for_model_family",
    "for_model_family",
    "#V#targetsModelFamily",
    "targetsModelFamily",
    "#V#targets_model_family",
    "targets_model_family",
    "#V#forModelProvider",
    "forModelProvider",
    "#V#for_model_provider",
    "for_model_provider",
)
_MODEL_PROMPT_VARIANT_CAPABILITY_PREDICATES: tuple[str, ...] = (
    "#V#forModelCapability",
    "forModelCapability",
    "#V#for_model_capability",
    "for_model_capability",
    "#V#forModelCapabilityProfile",
    "forModelCapabilityProfile",
    "#V#for_model_capability_profile",
    "for_model_capability_profile",
    "#V#targetsModelCapability",
    "targetsModelCapability",
    "#V#targets_model_capability",
    "targets_model_capability",
)
_ALLOWED_TOOL_PREDICATES: tuple[str, ...] = (
    "#V#allowsTool",
    "allowsTool",
    "#V#allows_tool",
    "allows_tool",
)
_PROMPT_SCOPE_PREDICATES: tuple[str, ...] = (
    "#V#hasPromptScope",
    "hasPromptScope",
    "#V#has_prompt_scope",
    "has_prompt_scope",
)
_PROMPT_VARIABLES_PREDICATES: tuple[str, ...] = (
    "#V#hasPromptVariables",
    "hasPromptVariables",
    "#V#has_prompt_variables",
    "has_prompt_variables",
)
_PROMPT_SOURCE_PREDICATES: tuple[str, ...] = (
    "#V#hasPromptSource",
    "hasPromptSource",
    "#V#has_prompt_source",
    "has_prompt_source",
)
_TOOL_PRIORITY_PREDICATES: tuple[str, ...] = (
    "#V#hasToolResolutionPriority",
    "hasToolResolutionPriority",
    "#V#has_tool_resolution_priority",
    "has_tool_resolution_priority",
)

_PROMPT_METADATA_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "prompt_name": _PROMPT_NAME_PREDICATES,
    "prompt_description": _PROMPT_DESCRIPTION_PREDICATES,
    "argument_hint": _ARGUMENT_HINT_PREDICATES,
    "agent_profile_ids": _AGENT_PROFILE_PREDICATES,
    "model_preference": _MODEL_PREFERENCE_PREDICATES,
    "model_prompt_variant_ids": _MODEL_PROMPT_VARIANT_PREDICATES,
    "allowed_tools": _ALLOWED_TOOL_PREDICATES,
    "prompt_scope": _PROMPT_SCOPE_PREDICATES,
    "prompt_variables": _PROMPT_VARIABLES_PREDICATES,
    "prompt_source": _PROMPT_SOURCE_PREDICATES,
    "tool_resolution_priority": _TOOL_PRIORITY_PREDICATES,
}

_LIST_METADATA_FIELDS: tuple[str, ...] = (
    "agent_profile_ids",
    "model_prompt_variant_ids",
    "allowed_tools",
    "prompt_variables",
    "tool_resolution_priority",
)
_SCALAR_METADATA_FIELDS: tuple[str, ...] = (
    "prompt_name",
    "prompt_description",
    "argument_hint",
    "model_preference",
    "prompt_scope",
    "prompt_source",
)
_PROMPT_VARIABLE_RE = re.compile(r"{([A-Za-z0-9_]+)}")


@dataclass(frozen=True)
class WorkflowPromptResolution:
    requested_prompt_concept_ids: tuple[str, ...] = ()
    resolved_prompt_concept_id: str | None = None
    prompt_text: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowPromptVariantResolution:
    base_prompt_concept_id: str | None = None
    selected_prompt_concept_id: str | None = None
    prompt_text: str | None = None
    rendered_variables: Mapping[str, Any] = field(default_factory=dict)
    match_reason: str = "base_prompt"
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_predicate_key(value: Any) -> str:
    text = _safe_text(value)
    if text.lower().startswith("#v#"):
        text = text[3:]
    text = re.sub(r"[-\s]+", "_", text)
    return text.lower()


def _normalise_match_token(value: Any) -> str:
    text = _safe_text(value).lower().replace(": ", ":")
    if text.startswith("#v#"):
        text = text[3:]
    text = re.sub(r"\s+", "_", text)
    return text


def _ordered_unique_strings(values: Iterable[Any] | Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _ordered_unique_tokens(values: Iterable[Any] | Any) -> list[str]:
    return [
        token
        for token in (
            _normalise_match_token(value) for value in _ordered_unique_strings(values)
        )
        if token
    ]


def _is_nonempty_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value) > 0
    return value is not None


def _coerce_multi_value_text(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _ordered_unique_strings(value)
    text = _safe_text(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return _ordered_unique_strings(parsed)
    if isinstance(parsed, str):
        return _ordered_unique_strings([parsed])
    if "\n" in text:
        line_values = []
        for line in text.splitlines():
            cleaned = line.strip().lstrip("-").strip()
            if cleaned:
                line_values.append(cleaned)
        if line_values:
            return _ordered_unique_strings(line_values)
    if "," in text:
        return _ordered_unique_strings(part.strip() for part in text.split(","))
    return [text]


def _detect_template_variables(prompt_text: str | None) -> list[str]:
    if not isinstance(prompt_text, str):
        return []
    return _ordered_unique_strings(
        match.group(1) for match in _PROMPT_VARIABLE_RE.finditer(prompt_text)
    )


def _normalise_validation_policy(value: Any) -> str:
    policy = _safe_text(value).lower()
    if policy in PROMPT_VALIDATION_POLICIES:
        return policy
    return PROMPT_VALIDATION_POLICY_FAIL


def normalise_prompt_validation_policy(value: Any) -> str:
    return _normalise_validation_policy(value)


def _build_alias_key_set(aliases: Sequence[str]) -> set[str]:
    return {_normalise_predicate_key(alias) for alias in aliases if _safe_text(alias)}


def _load_text_rows(concept_id: str) -> list[Mapping[str, Any]]:
    try:
        rows = get_texts_for_concept(concept_id)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _load_relationship_index(concept_id: str) -> dict[str, list[str]]:
    doc = ConceptsRepository.find_one({"concept_id": concept_id}) or {}
    relationships = doc.get("relationships") if isinstance(doc, Mapping) else {}
    if not isinstance(relationships, Mapping):
        return {}
    index: dict[str, list[str]] = {}
    for predicate, raw_values in relationships.items():
        predicate_key = _normalise_predicate_key(predicate)
        values = _ordered_unique_strings(raw_values)
        if not values:
            continue
        index[predicate_key] = values
    return index


def _select_text_value(
    rows: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
) -> tuple[str | None, str | None]:
    alias_keys = _build_alias_key_set(aliases)
    for row in rows:
        predicate_key = _normalise_predicate_key(row.get("predicate"))
        if predicate_key not in alias_keys:
            continue
        text = _safe_text(row.get("text"))
        if text:
            return text, _safe_text(row.get("predicate")) or predicate_key
    return None, None


def _select_text_values(
    rows: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
) -> tuple[list[str], list[str]]:
    alias_keys = _build_alias_key_set(aliases)
    values: list[str] = []
    predicates: list[str] = []
    for row in rows:
        predicate_key = _normalise_predicate_key(row.get("predicate"))
        if predicate_key not in alias_keys:
            continue
        raw_values = _coerce_multi_value_text(row.get("text"))
        if raw_values:
            values.extend(raw_values)
            predicate = _safe_text(row.get("predicate")) or predicate_key
            if predicate:
                predicates.append(predicate)
    return _ordered_unique_strings(values), _ordered_unique_strings(predicates)


def _select_relationship_values(
    relationship_index: Mapping[str, Sequence[str]],
    aliases: Sequence[str],
) -> tuple[list[str], list[str]]:
    alias_keys = _build_alias_key_set(aliases)
    values: list[str] = []
    predicates: list[str] = []
    for predicate_key, targets in relationship_index.items():
        if predicate_key not in alias_keys:
            continue
        values.extend(_ordered_unique_strings(targets))
        predicates.append(predicate_key)
    return _ordered_unique_strings(values), _ordered_unique_strings(predicates)


def _load_concept_metadata(concept_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = _load_text_rows(concept_id)
    relationship_index = _load_relationship_index(concept_id)
    prompt_service = PromptTemplateService(default_max_chars=24000)
    resolved_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
        [concept_id],
        fallback=None,
        max_chars=24000,
    )

    metadata: dict[str, Any] = {}
    sources: dict[str, dict[str, Any]] = {}

    for field_name in _SCALAR_METADATA_FIELDS:
        value, predicate = _select_text_value(
            rows,
            _PROMPT_METADATA_ALIAS_GROUPS[field_name],
        )
        if value:
            metadata[field_name] = value
            sources[field_name] = {
                "source": "text_relation",
                "predicate": predicate,
            }

    for field_name in _LIST_METADATA_FIELDS:
        text_values, text_predicates = _select_text_values(
            rows,
            _PROMPT_METADATA_ALIAS_GROUPS[field_name],
        )
        relationship_values, relationship_predicates = _select_relationship_values(
            relationship_index,
            _PROMPT_METADATA_ALIAS_GROUPS[field_name],
        )
        values = _ordered_unique_strings([*text_values, *relationship_values])
        if values:
            metadata[field_name] = values
            predicate_sources = [*text_predicates, *relationship_predicates]
            sources[field_name] = {
                "source": "mixed"
                if text_predicates and relationship_predicates
                else ("text_relation" if text_predicates else "relationship"),
                "predicates": _ordered_unique_strings(predicate_sources),
            }

    if metadata.get("agent_profile_ids"):
        metadata["agent_profile_id"] = metadata["agent_profile_ids"][0]
    if metadata.get("tool_resolution_priority"):
        metadata["tool_resolution_priority"] = _ordered_unique_strings(
            metadata["tool_resolution_priority"]
        )

    diagnostics = {
        "concept_id": concept_id,
        "prompt_text_available": bool(prompt_text),
        "resolved_prompt_concept_id": resolved_prompt_id,
        "detected_template_variables": _detect_template_variables(prompt_text),
        "metadata_sources": sources,
    }
    return metadata, diagnostics


def _select_variant_values(
    *,
    concept_id: str,
    aliases: Sequence[str],
) -> list[str]:
    rows = _load_text_rows(concept_id)
    relationship_index = _load_relationship_index(concept_id)
    text_values, _text_predicates = _select_text_values(rows, aliases)
    relationship_values, _relationship_predicates = _select_relationship_values(
        relationship_index,
        aliases,
    )
    return _ordered_unique_strings([*text_values, *relationship_values])


def _load_model_prompt_variant_candidates(
    base_prompt_concept_id: str,
) -> list[dict[str, Any]]:
    variant_ids = _select_variant_values(
        concept_id=base_prompt_concept_id,
        aliases=_MODEL_PROMPT_VARIANT_PREDICATES,
    )
    candidates: list[dict[str, Any]] = []
    for variant_id in variant_ids:
        if not variant_id:
            continue
        candidates.append(
            {
                "prompt_concept_id": variant_id,
                "model_ids": _select_variant_values(
                    concept_id=variant_id,
                    aliases=_MODEL_PROMPT_VARIANT_MODEL_PREDICATES,
                ),
                "model_families": _select_variant_values(
                    concept_id=variant_id,
                    aliases=_MODEL_PROMPT_VARIANT_FAMILY_PREDICATES,
                ),
                "model_capabilities": _select_variant_values(
                    concept_id=variant_id,
                    aliases=_MODEL_PROMPT_VARIANT_CAPABILITY_PREDICATES,
                ),
            }
        )
    return candidates


def _provider_from_model_token(model: str) -> tuple[str | None, str]:
    cleaned = _safe_text(model).replace(": ", ":")
    if ":" not in cleaned:
        return None, cleaned
    provider, remainder = cleaned.split(":", 1)
    provider_token = _normalise_match_token(provider)
    if provider_token in {
        "openai",
        "openrouter",
        "anthropic",
        "gemini",
        "meta",
        "ollama",
        "deepseek",
    }:
        return provider_token, remainder.strip()
    return None, cleaned


def _model_family_tokens(model: str) -> list[str]:
    _provider, bare_model = _provider_from_model_token(model)
    tokens: list[str] = []
    bare_token = _normalise_match_token(bare_model)
    if bare_token:
        tokens.append(bare_token)
    if ":" in bare_model:
        family = bare_model.split(":", 1)[0]
        family_token = _normalise_match_token(family)
        if family_token:
            tokens.append(family_token)
    if "-" in bare_model:
        parts = [part for part in bare_model.split("-") if part]
        for index in range(len(parts), 0, -1):
            token = _normalise_match_token("-".join(parts[:index]))
            if token:
                tokens.append(token)
    return _ordered_unique_tokens(tokens)


def _registry_model_entries(
    registry_snapshot: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(registry_snapshot, Mapping):
        return []
    models = registry_snapshot.get("models")
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
        return []
    return [entry for entry in models if isinstance(entry, Mapping)]


def _entry_matches_selected_model(
    entry: Mapping[str, Any],
    selected_model: str,
) -> bool:
    selected_tokens = set(_ordered_unique_tokens([selected_model]))
    provider, bare_model = _provider_from_model_token(selected_model)
    selected_tokens.update(_ordered_unique_tokens([bare_model]))
    if provider:
        selected_tokens.add(f"{provider}:{_normalise_match_token(bare_model)}")

    entry_tokens = _ordered_unique_tokens(
        [
            entry.get("model_id"),
            entry.get("concept_id"),
            entry.get("registry_entry_id"),
        ]
    )
    aliases = entry.get("model_aliases")
    if isinstance(aliases, Sequence) and not isinstance(
        aliases,
        (str, bytes, bytearray),
    ):
        entry_tokens.extend(_ordered_unique_tokens(aliases))
    entry_provider = _normalise_match_token(entry.get("provider"))
    for token in list(entry_tokens):
        if entry_provider:
            entry_tokens.append(f"{entry_provider}:{token}")
    return bool(set(entry_tokens) & selected_tokens)


def _model_match_context(
    *,
    selected_model: str | None,
    selected_candidate: Mapping[str, Any] | None = None,
    registry_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    model_tokens = _ordered_unique_tokens([selected_model])
    family_tokens: list[str] = []
    capability_tokens: list[str] = []

    provider, bare_model = _provider_from_model_token(selected_model or "")
    if bare_model and bare_model != selected_model:
        model_tokens.extend(_ordered_unique_tokens([bare_model]))
    if provider:
        model_tokens.extend(_ordered_unique_tokens([f"{provider}:{bare_model}"]))
        family_tokens.extend(_ordered_unique_tokens([provider, f"provider:{provider}"]))
    family_tokens.extend(_model_family_tokens(bare_model or selected_model or ""))

    candidate = selected_candidate if isinstance(selected_candidate, Mapping) else {}
    candidate_provider = _normalise_match_token(candidate.get("provider"))
    candidate_locality = _normalise_match_token(candidate.get("locality"))
    if candidate_provider:
        family_tokens.extend(
            _ordered_unique_tokens([candidate_provider, f"provider:{candidate_provider}"])
        )
    if candidate_locality:
        family_tokens.extend(
            _ordered_unique_tokens([candidate_locality, f"locality:{candidate_locality}"])
        )

    for key in (
        "capability_profile",
        "capability_profiles",
        "capabilities",
        "model_capabilities",
        "api_surface",
    ):
        capability_tokens.extend(_ordered_unique_tokens(candidate.get(key)))

    for entry in _registry_model_entries(registry_snapshot):
        if selected_model and not _entry_matches_selected_model(entry, selected_model):
            continue
        entry_provider = _normalise_match_token(entry.get("provider"))
        entry_locality = _normalise_match_token(entry.get("locality"))
        if entry_provider:
            family_tokens.extend(
                _ordered_unique_tokens([entry_provider, f"provider:{entry_provider}"])
            )
        if entry_locality:
            family_tokens.extend(
                _ordered_unique_tokens([entry_locality, f"locality:{entry_locality}"])
            )
        model_tokens.extend(
            _ordered_unique_tokens(
                [entry.get("model_id"), entry.get("concept_id"), entry.get("registry_entry_id")]
            )
        )
        api_profiles = entry.get("api_profiles")
        if isinstance(api_profiles, Sequence) and not isinstance(
            api_profiles,
            (str, bytes, bytearray),
        ):
            for profile in api_profiles:
                if isinstance(profile, Mapping):
                    capability_tokens.extend(
                        _ordered_unique_tokens(
                            [profile.get("profile_concept_id"), profile.get("api_surface")]
                        )
                    )

    return {
        "model_tokens": _ordered_unique_tokens(model_tokens),
        "family_tokens": _ordered_unique_tokens(family_tokens),
        "capability_tokens": _ordered_unique_tokens(capability_tokens),
    }


def _match_variant_candidate(
    candidate: Mapping[str, Any],
    model_context: Mapping[str, Sequence[str]],
) -> tuple[str | None, list[str]]:
    exact_matches = sorted(
        set(_ordered_unique_tokens(candidate.get("model_ids")))
        & set(_ordered_unique_tokens(model_context.get("model_tokens")))
    )
    if exact_matches:
        return "exact_model", exact_matches

    family_matches = sorted(
        set(_ordered_unique_tokens(candidate.get("model_families")))
        & set(_ordered_unique_tokens(model_context.get("family_tokens")))
    )
    if family_matches:
        return "model_family", family_matches

    capability_matches = sorted(
        set(_ordered_unique_tokens(candidate.get("model_capabilities")))
        & set(_ordered_unique_tokens(model_context.get("capability_tokens")))
    )
    if capability_matches:
        return "model_capability", capability_matches
    return None, []


def resolve_model_prompt_variant(
    *,
    base_prompt_concept_id: str | None,
    base_prompt_text: str | None,
    variables: Mapping[str, Any] | None = None,
    selected_model: str | None = None,
    selected_candidate: Mapping[str, Any] | None = None,
    registry_snapshot: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> WorkflowPromptVariantResolution:
    base_prompt_id = _safe_text(base_prompt_concept_id) or None
    base_text = base_prompt_text if isinstance(base_prompt_text, str) else None
    render_variables = dict(variables) if isinstance(variables, Mapping) else {}
    diagnostics: dict[str, Any] = {
        "base_prompt_concept_id": base_prompt_id,
        "selected_model": _safe_text(selected_model) or None,
        "variant_count": 0,
        "match_reason": "base_prompt",
        "fallback_reason": None,
    }

    if not base_prompt_id:
        diagnostics["fallback_reason"] = "base_prompt_concept_id_missing"
        return WorkflowPromptVariantResolution(
            base_prompt_concept_id=base_prompt_id,
            selected_prompt_concept_id=base_prompt_id,
            prompt_text=base_text,
            rendered_variables=render_variables,
            diagnostics=diagnostics,
        )

    candidates = _load_model_prompt_variant_candidates(base_prompt_id)
    model_context = _model_match_context(
        selected_model=selected_model,
        selected_candidate=selected_candidate,
        registry_snapshot=registry_snapshot,
    )
    diagnostics["variant_count"] = len(candidates)
    diagnostics["model_context"] = model_context
    diagnostics["candidate_prompt_ids"] = [
        candidate.get("prompt_concept_id") for candidate in candidates
    ]

    if not candidates:
        diagnostics["fallback_reason"] = "no_prompt_variants_declared"
        return WorkflowPromptVariantResolution(
            base_prompt_concept_id=base_prompt_id,
            selected_prompt_concept_id=base_prompt_id,
            prompt_text=base_text,
            rendered_variables=render_variables,
            diagnostics=diagnostics,
        )

    matched_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        match_reason, matched_tokens = _match_variant_candidate(candidate, model_context)
        if not match_reason:
            continue
        matched = dict(candidate)
        matched["match_reason"] = match_reason
        matched["matched_tokens"] = matched_tokens
        matched_candidates.append(matched)

    reason_order = {"exact_model": 0, "model_family": 1, "model_capability": 2}
    matched_candidates.sort(key=lambda item: reason_order.get(str(item.get("match_reason")), 99))

    prompt_service = PromptTemplateService(default_max_chars=max_chars)
    render_warnings: list[dict[str, Any]] = []
    for candidate in matched_candidates:
        variant_id = _safe_text(candidate.get("prompt_concept_id"))
        if not variant_id:
            continue
        try:
            rendered = prompt_service.render_prompt(
                [variant_id],
                variables=render_variables,
                fallback=None,
                max_chars=max_chars,
            )
        except Exception as exc:
            render_warnings.append(
                {
                    "prompt_concept_id": variant_id,
                    "error": str(exc),
                }
            )
            continue
        if rendered is None or not rendered.text.strip():
            render_warnings.append(
                {
                    "prompt_concept_id": variant_id,
                    "error": "prompt_text_missing_or_empty",
                }
            )
            continue

        match_reason = _safe_text(candidate.get("match_reason")) or "model_variant"
        diagnostics.update(
            {
                "selected_prompt_concept_id": rendered.prompt_id or variant_id,
                "match_reason": match_reason,
                "matched_tokens": list(candidate.get("matched_tokens") or []),
                "fallback_reason": None,
                "render_warnings": render_warnings,
            }
        )
        return WorkflowPromptVariantResolution(
            base_prompt_concept_id=base_prompt_id,
            selected_prompt_concept_id=rendered.prompt_id or variant_id,
            prompt_text=rendered.text,
            rendered_variables=dict(rendered.variables),
            match_reason=match_reason,
            diagnostics=diagnostics,
        )

    diagnostics["fallback_reason"] = (
        "matched_variants_unrenderable" if matched_candidates else "no_matching_variant"
    )
    diagnostics["render_warnings"] = render_warnings
    return WorkflowPromptVariantResolution(
        base_prompt_concept_id=base_prompt_id,
        selected_prompt_concept_id=base_prompt_id,
        prompt_text=base_text,
        rendered_variables=render_variables,
        diagnostics=diagnostics,
    )


def normalise_prompt_metadata_defaults(
    raw_defaults: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    defaults_map: dict[str, Any] = {}
    if isinstance(raw_defaults, Mapping):
        defaults_map = dict(raw_defaults)
    elif isinstance(raw_defaults, str):
        try:
            parsed = json.loads(raw_defaults)
        except Exception:
            parsed = None
        if isinstance(parsed, Mapping):
            defaults_map = dict(parsed)

    if not defaults_map:
        return {}

    normalised: dict[str, Any] = {}
    for field_name in _SCALAR_METADATA_FIELDS:
        value = defaults_map.get(field_name)
        if isinstance(value, Mapping):
            continue
        if _is_nonempty_value(value):
            normalised[field_name] = value

    for field_name in _LIST_METADATA_FIELDS:
        value = defaults_map.get(field_name)
        if isinstance(value, Mapping):
            continue
        values = _coerce_multi_value_text(value)
        if values:
            normalised[field_name] = values

    agent_profile_ids = _ordered_unique_strings(
        normalised.get("agent_profile_ids")
        or defaults_map.get("agent_profile_ids")
        or defaults_map.get("agent_profile_id")
        or []
    )
    if agent_profile_ids:
        normalised["agent_profile_ids"] = agent_profile_ids
        normalised["agent_profile_id"] = agent_profile_ids[0]
    return normalised


def _merge_prompt_metadata(
    *,
    prompt_metadata: Mapping[str, Any],
    agent_metadata: Mapping[str, Any],
    defaults: Mapping[str, Any],
    detected_template_variables: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged: dict[str, Any] = {}
    overrides: list[dict[str, Any]] = []

    for field_name in _SCALAR_METADATA_FIELDS:
        candidates = [
            ("prompt", prompt_metadata.get(field_name)),
            ("agent_profile", agent_metadata.get(field_name)),
            ("defaults", defaults.get(field_name)),
        ]
        winner_source = None
        winner_value = None
        populated_sources: list[str] = []
        for source_name, value in candidates:
            if not _is_nonempty_value(value):
                continue
            populated_sources.append(source_name)
            if winner_source is None:
                winner_source = source_name
                winner_value = value
        if winner_source is not None and winner_value is not None:
            merged[field_name] = winner_value
            if len(populated_sources) > 1:
                overrides.append(
                    {
                        "field": field_name,
                        "winner_source": winner_source,
                        "overridden_sources": populated_sources[1:],
                    }
                )

    for field_name in _LIST_METADATA_FIELDS:
        candidates = [
            ("prompt", _ordered_unique_strings(prompt_metadata.get(field_name, []))),
            ("agent_profile", _ordered_unique_strings(agent_metadata.get(field_name, []))),
            ("defaults", _ordered_unique_strings(defaults.get(field_name, []))),
        ]
        winner_source = None
        winner_values: list[str] = []
        populated_sources: list[str] = []
        for source_name, values in candidates:
            if not values:
                continue
            populated_sources.append(source_name)
            if winner_source is None:
                winner_source = source_name
                winner_values = values
        if winner_source is not None and winner_values:
            merged[field_name] = winner_values
            if len(populated_sources) > 1:
                overrides.append(
                    {
                        "field": field_name,
                        "winner_source": winner_source,
                        "overridden_sources": populated_sources[1:],
                    }
                )

    if not merged.get("prompt_variables"):
        detected = _ordered_unique_strings(detected_template_variables)
        if detected:
            merged["prompt_variables"] = detected
    if merged.get("agent_profile_ids"):
        merged["agent_profile_id"] = merged["agent_profile_ids"][0]
    return merged, overrides


def _apply_tool_priority(
    *,
    allowed_tools: Sequence[str],
    priority: Sequence[str],
) -> tuple[list[str], list[str]]:
    allowed = _ordered_unique_strings(allowed_tools)
    ordered_priority = _ordered_unique_strings(priority)
    if not allowed or not ordered_priority:
        return allowed, []
    prioritised = [tool for tool in ordered_priority if tool in allowed]
    remainder = [tool for tool in allowed if tool not in prioritised]
    unknown_priority_tools = [tool for tool in ordered_priority if tool not in allowed]
    return [*prioritised, *remainder], unknown_priority_tools


def _concept_exists(concept_id: str) -> bool:
    if not concept_id:
        return False
    if is_code_concept_id(concept_id):
        return True
    return ConceptsRepository.find_one({"concept_id": concept_id}, {"_id": 1}) is not None


@lru_cache(maxsize=1)
def _default_available_tool_names() -> frozenset[str]:
    try:
        from ..integrations.internal_mcp.catalogue import build_default_catalogue

        return frozenset(build_default_catalogue().list_methods())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Unable to build default internal MCP catalogue: %s", exc)
        return frozenset()


def resolve_workflow_prompt_metadata(
    *,
    prompt_concept_ids: Sequence[str],
    defaults: Mapping[str, Any] | None = None,
    validation_policy: str = PROMPT_VALIDATION_POLICY_FAIL,
    available_tools: Iterable[str] | None = None,
) -> WorkflowPromptResolution:
    requested_prompt_ids = tuple(_ordered_unique_strings(prompt_concept_ids))
    defaults_map = dict(defaults) if isinstance(defaults, Mapping) else {}
    policy = _normalise_validation_policy(validation_policy)

    prompt_service = PromptTemplateService(default_max_chars=24000)
    resolved_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
        requested_prompt_ids,
        fallback=None,
        max_chars=24000,
    )
    selected_prompt_id = resolved_prompt_id or (
        requested_prompt_ids[0] if requested_prompt_ids else None
    )

    prompt_metadata: dict[str, Any] = {}
    prompt_diagnostics: dict[str, Any] = {}
    if selected_prompt_id:
        prompt_metadata, prompt_diagnostics = _load_concept_metadata(selected_prompt_id)

    default_agent_profiles = _ordered_unique_strings(
        defaults_map.get("agent_profile_ids")
        or defaults_map.get("agent_profile_id")
        or []
    )
    prompt_agent_profiles = _ordered_unique_strings(
        prompt_metadata.get("agent_profile_ids")
        or prompt_metadata.get("agent_profile_id")
        or []
    )
    agent_profile_ids = prompt_agent_profiles or default_agent_profiles

    agent_metadata: dict[str, Any] = {}
    agent_diagnostics: dict[str, Any] = {}
    if agent_profile_ids:
        primary_agent_id = agent_profile_ids[0]
        if _concept_exists(primary_agent_id):
            agent_metadata, agent_diagnostics = _load_concept_metadata(primary_agent_id)
        else:
            agent_diagnostics = {
                "concept_id": primary_agent_id,
                "error": "agent_profile_unavailable",
            }

    merged_metadata, overrides = _merge_prompt_metadata(
        prompt_metadata=prompt_metadata,
        agent_metadata=agent_metadata,
        defaults=normalise_prompt_metadata_defaults(defaults_map),
        detected_template_variables=_detect_template_variables(prompt_text),
    )

    resolved_allowed_tools, unknown_priority_tools = _apply_tool_priority(
        allowed_tools=merged_metadata.get("allowed_tools", []),
        priority=merged_metadata.get("tool_resolution_priority", []),
    )
    if resolved_allowed_tools:
        merged_metadata["allowed_tools"] = resolved_allowed_tools

    available_tool_set = frozenset(
        _ordered_unique_strings(available_tools)
        if available_tools is not None
        else _default_available_tool_names()
    )
    unavailable_tools = [
        tool
        for tool in resolved_allowed_tools
        if available_tool_set and tool not in available_tool_set
    ]
    unavailable_agents = [
        agent_id
        for agent_id in agent_profile_ids
        if agent_id and not _concept_exists(agent_id)
    ]

    warnings: list[str] = []
    errors: list[str] = []
    if requested_prompt_ids and not prompt_text:
        errors.append("prompt_text_missing_or_empty")
    if unavailable_agents:
        message = "agent_profile_unavailable:" + ",".join(unavailable_agents)
        if policy == PROMPT_VALIDATION_POLICY_FAIL:
            errors.append(message)
        else:
            warnings.append(message)
    if unavailable_tools:
        message = "prompt_allowed_tools_unavailable:" + ",".join(unavailable_tools)
        if policy == PROMPT_VALIDATION_POLICY_FAIL:
            errors.append(message)
        else:
            warnings.append(message)
    if unknown_priority_tools:
        warnings.append(
            "prompt_tool_priority_unknown:" + ",".join(unknown_priority_tools)
        )

    diagnostics: dict[str, Any] = {
        "requested_prompt_concept_ids": list(requested_prompt_ids),
        "resolved_prompt_concept_id": selected_prompt_id,
        "prompt_text_available": bool(prompt_text),
        "validation_policy": policy,
        "status": "error" if errors else ("warning" if warnings else "ok"),
        "warnings": warnings,
        "errors": errors,
        "overrides": overrides,
        "unavailable_tools": unavailable_tools,
        "unavailable_agent_profiles": unavailable_agents,
        "unknown_tool_priority_entries": unknown_priority_tools,
        "prompt_diagnostics": prompt_diagnostics,
        "agent_profile_diagnostics": agent_diagnostics,
    }

    return WorkflowPromptResolution(
        requested_prompt_concept_ids=requested_prompt_ids,
        resolved_prompt_concept_id=selected_prompt_id,
        prompt_text=prompt_text,
        metadata=merged_metadata,
        diagnostics=diagnostics,
    )


def build_workflow_prompt_contract(
    *,
    resolution: WorkflowPromptResolution,
    validation_policy: str,
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "validation_policy": _normalise_validation_policy(validation_policy)
    }
    if resolution.requested_prompt_concept_ids:
        contract["requested_prompt_concept_ids"] = list(
            resolution.requested_prompt_concept_ids
        )
    if resolution.resolved_prompt_concept_id:
        contract["resolved_prompt_concept_id"] = resolution.resolved_prompt_concept_id
    if isinstance(resolution.prompt_text, str) and resolution.prompt_text.strip():
        contract["prompt_text"] = resolution.prompt_text
    if resolution.metadata:
        contract["metadata"] = dict(resolution.metadata)
    return contract


__all__ = [
    "PROMPT_VALIDATION_POLICIES",
    "PROMPT_VALIDATION_POLICY_FAIL",
    "PROMPT_VALIDATION_POLICY_WARN",
    "WORKFLOW_STEP_PROMPT_LINK_PREDICATE_ALIASES",
    "WorkflowPromptResolution",
    "WorkflowPromptVariantResolution",
    "build_workflow_prompt_contract",
    "normalise_prompt_metadata_defaults",
    "normalise_prompt_validation_policy",
    "resolve_model_prompt_variant",
    "resolve_workflow_prompt_metadata",
]
