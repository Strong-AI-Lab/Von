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
    "allowed_tools": _ALLOWED_TOOL_PREDICATES,
    "prompt_scope": _PROMPT_SCOPE_PREDICATES,
    "prompt_variables": _PROMPT_VARIABLES_PREDICATES,
    "prompt_source": _PROMPT_SOURCE_PREDICATES,
    "tool_resolution_priority": _TOOL_PRIORITY_PREDICATES,
}

_LIST_METADATA_FIELDS: tuple[str, ...] = (
    "agent_profile_ids",
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


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_predicate_key(value: Any) -> str:
    text = _safe_text(value)
    if text.lower().startswith("#v#"):
        text = text[3:]
    text = re.sub(r"[-\s]+", "_", text)
    return text.lower()


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
    return _ordered_unique_strings(match.group(1) for match in _PROMPT_VARIABLE_RE.finditer(prompt_text))


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
        _ordered_unique_strings(available_tools) if available_tools is not None else _default_available_tool_names()
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
    "build_workflow_prompt_contract",
    "normalise_prompt_metadata_defaults",
    "normalise_prompt_validation_policy",
    "resolve_workflow_prompt_metadata",
]
