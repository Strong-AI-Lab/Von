"""Vontology-authored synthesiser context-framing templates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .prompt_template_service import PromptTemplateService
from .workflow_prompt_authority_service import safe_str


SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID = (
    "#V#prompt_synthesiser_context_framing"
)
SYNTHESISER_CONTEXT_FRAMING_PROMPT_INPUT_KEY = (
    "synthesiser_context_framing_prompt_concept_id"
)
SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA = (
    "synthesiser_context_framing_template.v1"
)


class SynthesiserContextFramingTemplateError(ValueError):
    """Raised when represented framing template rendering is invalid."""


@dataclass(frozen=True)
class SynthesiserContextFramingTemplate:
    prompt_concept_id: str
    loaded_prompt_concept_id: str
    schema_version: str
    active_request_template: str
    tool_hints_template: str
    collection_presentation_hint_template: str
    item_summary_hint_template: str
    diagnostics: Mapping[str, Any]


class _StrictTemplateVariables(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def _normalise_template_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _render_template_fragment(
    *,
    template: str,
    variables: Mapping[str, Any],
    field_name: str,
) -> str:
    values = _StrictTemplateVariables(
        {str(key): _normalise_template_value(value) for key, value in variables.items()}
    )
    try:
        rendered = template.format_map(values).strip()
    except KeyError as exc:
        raise SynthesiserContextFramingTemplateError(
            f"synthesiser_context_framing_template_missing_variable:{field_name}:{exc.args[0]}"
        ) from exc
    except Exception as exc:
        raise SynthesiserContextFramingTemplateError(
            f"synthesiser_context_framing_template_render_failed:{field_name}:{exc}"
        ) from exc
    return rendered


def _load_template_payload(raw_text: str) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, f"synthesiser_context_framing_prompt_invalid_json:{exc.msg}"
    if not isinstance(payload, Mapping):
        return None, "synthesiser_context_framing_prompt_not_object"
    return payload, None


def _required_template_field(
    payload: Mapping[str, Any],
    field_name: str,
) -> tuple[str | None, str | None]:
    value = safe_str(payload.get(field_name))
    if not value:
        return None, f"synthesiser_context_framing_prompt_missing_field:{field_name}"
    return value, None


def resolve_synthesiser_context_framing_template(
    *,
    prompt_concept_id: str | None = None,
    max_chars: int = 12000,
) -> tuple[SynthesiserContextFramingTemplate | None, dict[str, Any]]:
    requested_prompt_id = (
        safe_str(prompt_concept_id) or SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID
    )
    diagnostics: dict[str, Any] = {
        "resolved_prompt_concept_id": requested_prompt_id,
        "requested_prompt_concept_id": requested_prompt_id,
        "loaded_prompt_concept_id": None,
        "error": None,
    }
    prompt_service = PromptTemplateService(default_max_chars=max(4000, int(max_chars)))
    try:
        loaded_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
            [requested_prompt_id],
            fallback=None,
            max_chars=max_chars,
        )
    except Exception as exc:
        diagnostics["error"] = f"synthesiser_context_framing_prompt_resolve_failed:{exc}"
        return None, diagnostics

    if not prompt_text:
        diagnostics["error"] = "synthesiser_context_framing_prompt_missing_or_empty"
        return None, diagnostics

    payload, error = _load_template_payload(prompt_text)
    if payload is None:
        diagnostics["error"] = error
        return None, diagnostics

    schema_version = safe_str(payload.get("schema")) or safe_str(
        payload.get("schema_version")
    )
    if schema_version != SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA:
        diagnostics["error"] = "synthesiser_context_framing_prompt_schema_invalid"
        diagnostics["schema_version"] = schema_version
        return None, diagnostics

    fields: dict[str, str] = {}
    for field_name in (
        "active_request_template",
        "tool_hints_template",
        "collection_presentation_hint_template",
        "item_summary_hint_template",
    ):
        field_value, field_error = _required_template_field(payload, field_name)
        if field_error:
            diagnostics["error"] = field_error
            return None, diagnostics
        fields[field_name] = field_value or ""

    loaded_prompt_id = safe_str(loaded_prompt_id) or requested_prompt_id
    diagnostics.update(
        {
            "error": None,
            "loaded_prompt_concept_id": loaded_prompt_id,
            "schema_version": schema_version,
        }
    )
    return (
        SynthesiserContextFramingTemplate(
            prompt_concept_id=requested_prompt_id,
            loaded_prompt_concept_id=loaded_prompt_id,
            schema_version=schema_version,
            active_request_template=fields["active_request_template"],
            tool_hints_template=fields["tool_hints_template"],
            collection_presentation_hint_template=fields[
                "collection_presentation_hint_template"
            ],
            item_summary_hint_template=fields["item_summary_hint_template"],
            diagnostics=diagnostics,
        ),
        diagnostics,
    )


def _base_message_metadata(
    template: SynthesiserContextFramingTemplate,
    *,
    template_field: str,
) -> dict[str, Any]:
    return {
        "role": "system",
        "source": "vontology_prompt_template",
        "requested_prompt_concept_id": template.prompt_concept_id,
        "source_prompt_concept_id": template.loaded_prompt_concept_id,
        "template_schema": template.schema_version,
        "template_field": template_field,
    }


def render_active_request_framing(
    template: SynthesiserContextFramingTemplate,
    *,
    active_user_message: str,
) -> dict[str, Any] | None:
    active_text = _normalise_template_value(active_user_message)
    if not active_text:
        return None
    content = _render_template_fragment(
        template=template.active_request_template,
        variables={"active_user_message": active_text},
        field_name="active_request_template",
    )
    if not content:
        return None
    return {
        **_base_message_metadata(template, template_field="active_request_template"),
        "content": content,
    }


def render_tool_hints_framing(
    template: SynthesiserContextFramingTemplate,
    *,
    tool_concept_id: str,
    collection_presentation_hint: str = "",
    item_summary_hint: str = "",
    hint_predicate_ids: Sequence[str] = (),
) -> dict[str, Any] | None:
    tool_id = _normalise_template_value(tool_concept_id)
    collection_hint = _normalise_template_value(collection_presentation_hint)
    item_hint = _normalise_template_value(item_summary_hint)
    if not tool_id or not collection_hint and not item_hint:
        return None

    section_parts: list[str] = []
    if collection_hint:
        rendered_collection = _render_template_fragment(
            template=template.collection_presentation_hint_template,
            variables={
                "tool_concept_id": tool_id,
                "collection_presentation_hint": collection_hint,
            },
            field_name="collection_presentation_hint_template",
        )
        if rendered_collection:
            section_parts.append(rendered_collection)
    if item_hint:
        rendered_item = _render_template_fragment(
            template=template.item_summary_hint_template,
            variables={
                "tool_concept_id": tool_id,
                "item_summary_hint": item_hint,
            },
            field_name="item_summary_hint_template",
        )
        if rendered_item:
            section_parts.append(rendered_item)

    hint_sections = "\n".join(section_parts).strip()
    if not hint_sections:
        return None

    content = _render_template_fragment(
        template=template.tool_hints_template,
        variables={
            "tool_concept_id": tool_id,
            "collection_presentation_hint": collection_hint,
            "item_summary_hint": item_hint,
            "hint_sections": hint_sections,
        },
        field_name="tool_hints_template",
    )
    if not content:
        return None
    return {
        **_base_message_metadata(template, template_field="tool_hints_template"),
        "content": content,
        "tool_concept_id": tool_id,
        "hint_predicate_ids": [
            predicate_id
            for predicate_id in hint_predicate_ids
            if isinstance(predicate_id, str) and predicate_id.strip()
        ],
    }


__all__ = [
    "SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID",
    "SYNTHESISER_CONTEXT_FRAMING_PROMPT_INPUT_KEY",
    "SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA",
    "SynthesiserContextFramingTemplate",
    "SynthesiserContextFramingTemplateError",
    "render_active_request_framing",
    "render_tool_hints_framing",
    "resolve_synthesiser_context_framing_template",
]
