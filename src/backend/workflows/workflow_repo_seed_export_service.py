"""Generated repo-seed workflow bundle exports from authoritative Vontology state.

These exports are deterministic snapshots for bootstrap/review purposes only.
They must never become a separate authored workflow-maintenance surface.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from ..services import concept_service
from ..services.text_value_service import (
    get_preferred_text_for_concept,
    get_texts_for_concept,
)
from . import workflow_concept_authority_service as authority_service
from .static_input_binding_utils import stable_static_input_bindings
from .subworkflow_contracts import WORKFLOW_SUBWORKFLOW_ACTION_ID
from .vontology_loader import (
    WORKFLOW_LAUNCH_INPUT_CONTRACT_TEXT_PREDICATE_PRECEDENCE,
    load_workflow_definition_from_vontology,
    resolve_workflow_launch_input_contract,
)
from .workflow_definition_identity_service import collect_workflow_action_ids

_DESCRIPTION_PREDICATE_PRECEDENCE: tuple[tuple[str, ...], ...] = (
    ("hasDescription", "#V#hasDescription"),
)
_CONTENT_PREDICATE_PRECEDENCE: tuple[tuple[str, ...], ...] = (
    ("hasContent", "#V#hasContent"),
)
_NOTE_PREDICATE_ALIASES = {"hasNote", "#V#hasNote"}
_SKIPPED_TEXT_RELATION_PREDICATES = {
    "hasName",
    "#V#hasName",
    "hasDescription",
    "#V#hasDescription",
    "hasContent",
    "#V#hasContent",
    "hasNote",
    "#V#hasNote",
    *{
        predicate
        for aliases in WORKFLOW_LAUNCH_INPUT_CONTRACT_TEXT_PREDICATE_PRECEDENCE
        for predicate in aliases
    },
}


def _load_raw_repo_seed_bundle_payload(
    *,
    asset_path: str | Path,
) -> tuple[Path, dict[str, Any], dict[str, Mapping[str, Any]]]:
    target_path = Path(asset_path).expanduser().resolve()
    raw_payload = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, Mapping):
        raise ValueError("repo_seed_workflow_bundle_not_mapping")
    workflows_payload = raw_payload.get("workflows")
    if not isinstance(workflows_payload, Sequence) or isinstance(
        workflows_payload,
        str,
    ):
        raise ValueError("repo_seed_workflow_bundle_workflows_missing")
    raw_workflow_entry_by_id = {
        str(item.get("workflow_id") or "").strip(): item
        for item in workflows_payload
        if isinstance(item, Mapping) and str(item.get("workflow_id") or "").strip()
    }
    if not raw_workflow_entry_by_id:
        raise ValueError("repo_seed_workflow_source_workflow_id_missing")
    return target_path, dict(raw_payload), raw_workflow_entry_by_id


def _stable_json_like(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _stable_json_like(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _stable_json_like(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_json_like(item) for item in value]
    if isinstance(value, set):
        return sorted(_stable_json_like(item) for item in value)
    return str(value)


def _compact_mapping(
    payload: Mapping[str, Any],
    *,
    keep_empty_keys: Sequence[str] = (),
) -> dict[str, Any]:
    keep = {str(item) for item in keep_empty_keys}
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key in keep:
            compacted[str(key)] = value
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, Mapping) and not value:
            continue
        if isinstance(value, Sequence) and not isinstance(value, str) and not value:
            continue
        compacted[str(key)] = value
    return compacted


def _normalise_type_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        type_id = str(item or "").strip()
        if not type_id or type_id in seen:
            continue
        seen.add(type_id)
        ordered.append(type_id)
    return ordered


def _resolve_preferred_relation_text(
    workflow_id: str,
    *,
    predicate_precedence: Sequence[str | Sequence[str]],
) -> str | None:
    preferred_row = get_preferred_text_for_concept(
        workflow_id,
        predicate_precedence=predicate_precedence,
        preferred_languages=("en-NZ", "en"),
        limit=200,
    )
    if not isinstance(preferred_row, Mapping):
        return None
    text = str(preferred_row.get("text") or "").strip()
    return text or None


def _resolve_workflow_notes(workflow_id: str) -> list[str]:
    rows = get_texts_for_concept(workflow_id, limit=250)
    note_values = sorted(
        {
            str(row.get("text") or "").strip()
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("predicate") or "").strip() in _NOTE_PREDICATE_ALIASES
            and str(row.get("lang") or "en-NZ").strip() in {"en-NZ", "en"}
            and str(row.get("text") or "").strip()
        }
    )
    return note_values


def _resolve_workflow_text_relations(workflow_id: str) -> list[dict[str, Any]]:
    relation_specs: list[dict[str, Any]] = []
    for row in get_texts_for_concept(workflow_id, limit=250):
        if not isinstance(row, Mapping):
            continue
        predicate = str(row.get("predicate") or "").strip()
        text = str(row.get("text") or "").strip()
        lang = str(row.get("lang") or "en-NZ").strip() or "en-NZ"
        if not predicate or not text or predicate in _SKIPPED_TEXT_RELATION_PREDICATES:
            continue
        relation_specs.append(
            {
                "predicate": predicate,
                "text": text,
                "lang": lang,
            }
        )
    relation_specs.sort(
        key=lambda item: (
            str(item.get("predicate") or ""),
            str(item.get("lang") or ""),
            str(item.get("text") or ""),
        )
    )
    return relation_specs


def _resolve_step_notes(
    *,
    workflow_id: str,
    definition: Any,
) -> dict[str, list[str]]:
    step_notes: dict[str, list[str]] = {}
    states = getattr(definition, "states", None)
    if not isinstance(states, Mapping):
        return step_notes
    for step_concept_id in states.keys():
        step_concept_id_text = str(step_concept_id or "").strip()
        if not step_concept_id_text:
            continue
        state_id = _state_reference_to_bundle_state_id(
            workflow_id=workflow_id,
            state_ref=step_concept_id_text,
        )
        if not state_id:
            continue
        notes = sorted(
            {
                str(row.get("text") or "").strip()
                for row in get_texts_for_concept(step_concept_id_text, limit=100)
                if isinstance(row, Mapping)
                and str(row.get("predicate") or "").strip() in _NOTE_PREDICATE_ALIASES
                and str(row.get("lang") or "en-NZ").strip() in {"en-NZ", "en"}
                and str(row.get("text") or "").strip()
            }
        )
        if notes:
            step_notes[state_id] = notes
    return step_notes


def _state_reference_to_bundle_state_id(
    *,
    workflow_id: str,
    state_ref: Any,
) -> str | None:
    state_text = str(state_ref or "").strip()
    if not state_text:
        return None
    prefix = f"#V#workflow_step_{authority_service._workflow_slug(workflow_id)}_"
    if state_text.startswith(prefix):
        suffix = state_text[len(prefix) :].strip()
        return suffix or state_text
    return state_text


def _serialise_context_input_mapping_specs(
    mapping_specs: Sequence[Any],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in mapping_specs:
        concept_id = str(getattr(item, "concept_id", "") or "").strip()
        context_key = str(getattr(item, "context_key", "") or "").strip()
        tool_param = str(getattr(item, "tool_param", "") or "").strip()
        if not concept_id or not context_key or not tool_param:
            continue
        payloads.append(
            {
                "concept_id": concept_id,
                "context_key": context_key,
                "required": bool(getattr(item, "required", True)),
                "tool_param": tool_param,
            }
        )
    payloads.sort(
        key=lambda item: (
            str(item.get("tool_param") or ""),
            str(item.get("context_key") or ""),
            str(item.get("concept_id") or ""),
        )
    )
    return payloads


def _serialise_tool_output_mapping_specs(
    mapping_specs: Sequence[Any],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in mapping_specs:
        concept_id = str(getattr(item, "concept_id", "") or "").strip()
        tool_output_field = str(
            getattr(item, "tool_output_field", "") or ""
        ).strip()
        context_key = str(getattr(item, "context_key", "") or "").strip()
        if not concept_id or not tool_output_field or not context_key:
            continue
        payloads.append(
            {
                "concept_id": concept_id,
                "context_key": context_key,
                "tool_output_field": tool_output_field,
            }
        )
    payloads.sort(
        key=lambda item: (
            str(item.get("tool_output_field") or ""),
            str(item.get("context_key") or ""),
            str(item.get("concept_id") or ""),
        )
    )
    return payloads


def _serialise_static_input_bindings(
    bindings: Sequence[tuple[str, Any]],
) -> list[list[Any]]:
    return [
        [key, _stable_json_like(value)]
        for key, value in stable_static_input_bindings(bindings)
        if isinstance(key, str) and key.strip()
    ]


def _build_publication_spec_payload_from_definition(
    *,
    workflow_id: str,
    definition: Any,
) -> dict[str, Any]:
    states = getattr(definition, "states", None)
    if not isinstance(states, Mapping) or not states:
        raise RuntimeError(f"repo_seed_authority_states_missing:{workflow_id}")

    steps: list[dict[str, Any]] = []
    for state_key, state_spec in states.items():
        state_key_text = str(state_key or "").strip()
        if not state_key_text:
            continue
        state_id = _state_reference_to_bundle_state_id(
            workflow_id=workflow_id,
            state_ref=state_key_text,
        )
        if state_id is None:
            continue
        concept_id: str | None = None
        if state_id == state_key_text and state_key_text.startswith("#V#"):
            concept_id = state_key_text

        actions = tuple(getattr(state_spec, "actions", ()) or ())
        action = actions[0] if actions else None
        action_id = (
            str(getattr(action, "action_id", "") or "").strip() or None
            if action is not None
            else None
        )
        action_concept_id = (
            str(getattr(action, "contract_concept_id", "") or "").strip() or None
            if action is not None
            else None
        )
        runtime_details = authority_service._extract_runtime_step_publication_details(
            workflow_id=workflow_id,
            state_id=state_key_text,
            registration_definition=definition,
        )
        invoked_workflow_id = (
            str(runtime_details.invoked_workflow_id or "").strip() or None
        )
        if invoked_workflow_id and not action_id:
            action_id = WORKFLOW_SUBWORKFLOW_ACTION_ID
        execution_mode = (
            str(runtime_details.execution_mode or "").strip()
            or (
                str(getattr(action, "execution_mode", "") or "").strip()
                if action is not None
                else ""
            )
            or None
        )
        prompt_concept_ids = (
            list(authority_service._extract_publication_prompt_concept_ids(action))
            if action is not None
            else []
        )
        context_input_mapping_specs = _serialise_context_input_mapping_specs(
            runtime_details.context_input_mapping_specs
        )
        shadowed_tool_params = {
            str(item.get("tool_param") or "").strip()
            for item in context_input_mapping_specs
            if str(item.get("tool_param") or "").strip()
        }
        static_input_bindings = _serialise_static_input_bindings(
            [
                binding
                for binding in runtime_details.static_input_bindings
                if binding[0] not in shadowed_tool_params
                and binding[0] != "__prompt_resolution_diagnostics"
            ]
        )

        next_state: str | None = None
        on_true_state: str | None = None
        on_false_state: str | None = None
        on_failure_state: str | None = None
        on_unknown_state: str | None = None
        on_approval_required_state: str | None = None
        on_break_state: str | None = None
        on_continue_state: str | None = None
        conditional_transitions: list[dict[str, Any]] = []
        for transition in tuple(getattr(state_spec, "transitions", ()) or ()):
            to_state = _state_reference_to_bundle_state_id(
                workflow_id=workflow_id,
                state_ref=getattr(transition, "to_state", None),
            )
            if not to_state:
                continue
            reason = str(getattr(transition, "reason", "") or "").strip()
            condition_spec = getattr(transition, "condition_spec", None)
            if not isinstance(condition_spec, Mapping):
                condition_spec = {"kind": "always"}
            if reason == "next_step":
                next_state = to_state
            elif reason == "on_true":
                on_true_state = to_state
            elif reason == "on_false":
                on_false_state = to_state
            elif reason == "on_failure":
                on_failure_state = to_state
            elif reason == "on_unknown":
                on_unknown_state = to_state
            elif reason == "on_approval_required":
                on_approval_required_state = to_state
            elif reason == "on_break":
                on_break_state = to_state
            elif reason == "on_continue":
                on_continue_state = to_state
            else:
                conditional_transitions.append(
                    {
                        "condition_spec": _stable_json_like(condition_spec),
                        "reason": reason or None,
                        "to_state": to_state,
                    }
                )

        steps.append(
            _compact_mapping(
                {
                    "action_concept_id": action_concept_id,
                    "action_id": action_id,
                    "concept_id": concept_id,
                    "conditional_transitions": conditional_transitions,
                    "context_input_mapping_specs": context_input_mapping_specs,
                    "context_input_mappings": [],
                    "effects": [],
                    "execution_mode": execution_mode,
                    "invoked_workflow_id": invoked_workflow_id,
                    "llm_policy": _stable_json_like(runtime_details.llm_policy),
                    "mutation_authority": _stable_json_like(
                        runtime_details.mutation_authority
                    ),
                    "next_state": next_state,
                    "on_approval_required_state": on_approval_required_state,
                    "on_break_state": on_break_state,
                    "on_continue_state": on_continue_state,
                    "on_failure_state": on_failure_state,
                    "on_false_state": on_false_state,
                    "on_true_state": on_true_state,
                    "on_unknown_state": on_unknown_state,
                    "prompt_concept_ids": prompt_concept_ids,
                    "state_id": state_id,
                    "static_input_bindings": static_input_bindings,
                    "tool_output_context_mappings": [],
                    "tool_output_mapping_specs": _serialise_tool_output_mapping_specs(
                        runtime_details.tool_output_mapping_specs
                    ),
                    "validation_policy": _stable_json_like(
                        runtime_details.validation_policy
                    ),
                    "writes_context_keys": list(
                        dict.fromkeys(runtime_details.writes_context_keys)
                    ),
                },
                keep_empty_keys=("state_id",),
            )
        )

    initial_state = _state_reference_to_bundle_state_id(
        workflow_id=workflow_id,
        state_ref=getattr(definition, "initial_state", None),
    )
    return _compact_mapping(
        {
            "initial_state": initial_state or (
                str(steps[0].get("state_id") or "").strip() if steps else None
            ),
            "steps": steps,
        },
        keep_empty_keys=("steps",),
    )


def _build_workflow_entry(
    *,
    workflow_id: str,
    raw_workflow_entry: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        raise RuntimeError(f"repo_seed_authority_definition_missing:{workflow_id}")

    concept_doc = concept_service.get_concept_by_concept_id(workflow_id)
    if concept_doc is None:
        raise RuntimeError(f"repo_seed_authority_concept_missing:{workflow_id}")

    relationships = concept_doc.get("relationships") or {}
    raw_type_ids = _normalise_type_ids(
        raw_workflow_entry.get("type_ids")
        if isinstance(raw_workflow_entry, Mapping)
        else ()
    )
    type_ids = raw_type_ids or _normalise_type_ids(
        relationships.get("is_an_instance_of")
        if isinstance(relationships, Mapping)
        else ()
    )
    launch_input_contract, _launch_source = resolve_workflow_launch_input_contract(
        workflow_id
    )
    workflow_notes = []
    if isinstance(raw_workflow_entry, Mapping) and isinstance(
        raw_workflow_entry.get("workflow_notes"), Sequence
    ):
        workflow_notes = [
            str(item).strip()
            for item in (raw_workflow_entry.get("workflow_notes") or [])
            if isinstance(item, str) and str(item).strip()
        ]

    entry = _compact_mapping(
        {
            "workflow_id": workflow_id,
            "display_name": concept_service.get_concept_display_name(concept_doc)
            or workflow_id,
            "description": _resolve_preferred_relation_text(
                workflow_id,
                predicate_precedence=_DESCRIPTION_PREDICATE_PRECEDENCE,
            ),
            "content": _resolve_preferred_relation_text(
                workflow_id,
                predicate_precedence=_CONTENT_PREDICATE_PRECEDENCE,
            ),
            "workflow_notes": workflow_notes,
            "text_relations": _resolve_workflow_text_relations(workflow_id),
            "launch_input_contract": _stable_json_like(launch_input_contract),
            "publication_spec": _build_publication_spec_payload_from_definition(
                workflow_id=workflow_id,
                definition=definition,
            ),
            "type_ids": type_ids,
            "step_notes": (
                _resolve_step_notes(
                    workflow_id=workflow_id,
                    definition=definition,
                )
                if isinstance(raw_workflow_entry, Mapping)
                and isinstance(raw_workflow_entry.get("step_notes"), Mapping)
                else {}
            ),
        },
        keep_empty_keys=("workflow_id",),
    )
    return entry, collect_workflow_action_ids(definition)


def build_repo_seed_workflow_bundle_from_authority(
    *,
    asset_path: str | Path,
) -> dict[str, Any]:
    """Build a deterministic repo-seed bundle from authoritative Vontology state."""

    _target_path, raw_payload, raw_workflow_entry_by_id = _load_raw_repo_seed_bundle_payload(
        asset_path=asset_path
    )
    workflow_ids = list(raw_workflow_entry_by_id.keys())
    workflow_entries: list[dict[str, Any]] = []
    supported_action_ids: set[str] = set()
    for workflow_id in workflow_ids:
        workflow_entry, action_ids = _build_workflow_entry(
            workflow_id=workflow_id,
            raw_workflow_entry=raw_workflow_entry_by_id.get(workflow_id),
        )
        workflow_entries.append(workflow_entry)
        supported_action_ids.update(action_ids)
    ordered_supported_action_ids: list[str] = []
    for action_id in raw_payload.get("supported_action_ids") or []:
        action_id_text = str(action_id or "").strip()
        if not action_id_text or action_id_text not in supported_action_ids:
            continue
        ordered_supported_action_ids.append(action_id_text)
        supported_action_ids.discard(action_id_text)
    ordered_supported_action_ids.extend(sorted(supported_action_ids))

    return _compact_mapping(
        {
            "family_id": raw_payload.get("family_id"),
            "managed_by": raw_payload.get("managed_by"),
            "schema_version": authority_service.REPO_SEED_WORKFLOW_BUNDLE_SCHEMA_VERSION,
            "source_tag": raw_payload.get("source_tag"),
            "supported_action_ids": ordered_supported_action_ids,
            "workflows": workflow_entries,
        },
        keep_empty_keys=("workflows",),
    )


def render_repo_seed_workflow_bundle_from_authority(
    *,
    asset_path: str | Path,
) -> str:
    payload = build_repo_seed_workflow_bundle_from_authority(asset_path=asset_path)
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def write_repo_seed_workflow_bundle_from_authority(
    *,
    asset_path: str | Path,
) -> dict[str, Any]:
    target_path = Path(asset_path).expanduser().resolve()
    payload = build_repo_seed_workflow_bundle_from_authority(asset_path=target_path)
    rendered = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    target_path.write_text(rendered, encoding="utf-8")
    authority_service.clear_repo_seed_workflow_bundle_cache()
    return {
        "asset_path": str(target_path),
        "workflow_ids": [
            str(item.get("workflow_id") or "").strip()
            for item in (payload.get("workflows") or [])
            if isinstance(item, Mapping) and str(item.get("workflow_id") or "").strip()
        ],
        "bytes_written": len(rendered.encode("utf-8")),
    }


def diff_repo_seed_workflow_bundle_from_authority(
    *,
    asset_path: str | Path,
) -> dict[str, Any]:
    target_path = Path(asset_path).expanduser().resolve()
    expected = render_repo_seed_workflow_bundle_from_authority(asset_path=target_path)
    current = target_path.read_text(encoding="utf-8")
    diff_lines = list(
        unified_diff(
            current.splitlines(),
            expected.splitlines(),
            fromfile=str(target_path),
            tofile=f"{target_path} (authority_export)",
            lineterm="",
        )
    )
    return {
        "asset_path": str(target_path),
        "has_differences": bool(diff_lines),
        "diff": "\n".join(diff_lines) + ("\n" if diff_lines else ""),
    }


__all__ = [
    "build_repo_seed_workflow_bundle_from_authority",
    "diff_repo_seed_workflow_bundle_from_authority",
    "render_repo_seed_workflow_bundle_from_authority",
    "write_repo_seed_workflow_bundle_from_authority",
]
