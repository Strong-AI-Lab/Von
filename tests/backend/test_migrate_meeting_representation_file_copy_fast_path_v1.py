from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager, nullcontext

import pytest

from scripts import migrate_meeting_representation_file_copy_fast_path_v1 as migration
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


def _sample_authoring_spec() -> dict:
    current_prompt = "Legacy meeting representation prompt."
    current_policy = {
        "allowed_tools": [
            "search_concepts",
            "fetch_concept",
            "get_predicate_incidence",
            "find_relations_with_argument",
            "create_concepts",
            "add_relationship",
            "upsert_text_relation",
            "upsert_singleton_text_relation",
        ],
        "context_fields": [
            {"context_key": "prompt", "label": "Current turn request"},
            {
                "context_key": "meeting_source_text",
                "label": "Partially published source text",
            },
            {
                "context_key": "meeting_source_filename",
                "label": "Partially published source filename",
            },
        ],
        "max_tool_invocations": 24,
        "prompt_candidates": [migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID],
        "prompt_text": current_prompt,
        "required_tools": [
            "search_concepts",
            "fetch_concept",
            "get_predicate_incidence",
            "create_concepts",
            "add_relationship",
        ],
        "selected_prompt_id": migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID,
        "tool_argument_defaults": {
            "fetch_concept": {
                "include_concept_preview": True,
                "include_relations_any_arg": True,
                "include_relations_arg1": True,
                "include_text_relations_arg1": True,
                "limit": 80,
            },
            "get_predicate_incidence": {
                "argument_index": "subject",
                "limit": 40,
            },
        },
        "tool_mode": "allowed",
    }
    current_prompt_contract = {
        "resolved_prompt_concept_id": migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID,
        "requested_prompt_concept_ids": [migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID],
        "prompt_text": current_prompt,
        "validation_policy": "fail",
    }
    return {
        "workflow_id": migration.WORKFLOW_ID,
        "description": "Represent meetings.",
        "initial_state_key": migration.ROUTE_SOURCE_STATE_ID,
        "steps": [
            {
                "state_id": migration.ROUTE_SOURCE_STATE_ID,
                "concept_id": migration.ROUTE_SOURCE_STATE_ID,
                "terminal": False,
                "conditional_transitions": [
                    {
                        "to_state": migration.READ_FILE_COPY_STATE_ID,
                        "reason": "uploaded_file_copy_available",
                        "condition_spec": {
                            "kind": "context_exists",
                            "key": "file_copy_concept_id",
                            "expected": True,
                        },
                    }
                ],
                "next_state_key": "represent_meeting",
            },
            {
                "state_id": migration.READ_FILE_COPY_STATE_ID,
                "concept_id": migration.READ_FILE_COPY_STATE_ID,
                "action_id": "workflow_mcp.invoke_tool",
                "execution_mode": "deterministic",
                "static_input_bindings": [
                    {"tool_param": "tool_name", "value": "read_file_copy"},
                ],
                "next_state_key": "represent_meeting",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "represent_meeting",
                "action_id": "llm.action",
                "execution_mode": "llm",
                "llm_policy": current_policy,
                "prompt_contract": current_prompt_contract,
                "metadata": {
                    "llm_policy": copy.deepcopy(current_policy),
                    "llm_policies": [copy.deepcopy(current_policy)],
                    "prompt_contract": copy.deepcopy(current_prompt_contract),
                },
                "next_state_key": "completed",
                "on_failure_state_key": "failed",
            },
            {"state_id": "completed", "terminal": True},
            {"state_id": "failed", "terminal": True},
        ],
        "workflow_metadata": {
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "required_inputs": ["prompt"],
                "input_mappings": [
                    {
                        "target_context_key": "prompt",
                        "source_expression": "inputs.prompt",
                        "extractor": "identity",
                        "required": True,
                    }
                ],
            },
            "required_effects_contract": {
                "schema_version": "workflow_required_effects_contract.v1",
                "contract_id": "meeting_representation_materialisation",
                "required_effects": [
                    {
                        "effect_id": "meeting_representation_mutation",
                        "effect_type": "meeting_representation",
                        "required_tools": ["create_concepts", "add_relationship"],
                        "required_tools_match": "all",
                        "activation_required_tools": [
                            "search_concepts",
                            "fetch_concept",
                            "get_predicate_incidence",
                        ],
                        "activation_required_tools_match": "all",
                    }
                ],
            },
        },
    }


def _prompt_snapshot(
    text: str,
    relation_count: int = 1,
    *,
    concept_exists: bool = True,
    is_prompt_instance: bool = True,
    is_global_general: bool = True,
) -> dict:
    return {
        "concept_exists": concept_exists,
        "is_prompt_instance": is_prompt_instance if concept_exists else False,
        "is_global_general": is_global_general if concept_exists else False,
        "specific_to_user": [],
        "specific_to_organisation": [],
        "relation_count": relation_count,
        "relation_id": "prompt-relation" if relation_count else None,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _step_with_action(spec: dict, action_id: str) -> dict:
    return next(step for step in spec["steps"] if step.get("action_id") == action_id)


def _compiled_context_binding(mapping: dict) -> dict:
    return {
        "$context_key": mapping["context_key"],
        "$mapping_concept_id": mapping["mapping_concept_id"],
        "$required": mapping["required"],
    }


def test_rewrite_adds_bounded_optional_file_copy_path_and_is_idempotent() -> None:
    original = _sample_authoring_spec()
    before = copy.deepcopy(original)

    candidate, changed = migration.rewrite_meeting_representation_workflow(original)

    assert changed is True
    assert original == before
    launch = candidate["workflow_metadata"]["launch_input_contract"]
    assert launch["required_inputs"] == ["prompt"]
    assert {
        "target_context_key": "file_copy_concept_id",
        "source_expression": "inputs.file_copy_concept_id",
        "extractor": "identity",
        "required": False,
        "description": (
            "Pass through the optional authenticated file-copy concept ID "
            "created for an uploaded meeting source."
        ),
    } in launch["input_mappings"]

    assert candidate["initial_state_key"] == "represent_meeting"
    candidate_state_ids = {step.get("state_id") for step in candidate["steps"]}
    assert migration.ROUTE_SOURCE_STATE_ID not in candidate_state_ids
    assert migration.READ_FILE_COPY_STATE_ID not in candidate_state_ids
    assert migration.VERIFY_EVIDENCE_STATE_KEY in candidate_state_ids
    assert migration.CORRELATE_EVIDENCE_STATE_KEY in candidate_state_ids
    assert candidate_state_ids == {
        "represent_meeting",
        migration.VERIFY_EVIDENCE_STATE_KEY,
        migration.CORRELATE_EVIDENCE_STATE_KEY,
        migration.COMPLETED_STATE_KEY,
        migration.FAILED_STATE_KEY,
    }
    assert len(candidate["steps"]) == 5
    step = _step_with_action(candidate, "llm.action")
    assert step["conditional_transitions"] == [
        {
            "to_state": migration.VERIFY_EVIDENCE_STATE_KEY,
            "reason": "uploaded_file_requires_canonical_evidence_readback",
            "condition_spec": (migration._file_copy_present_after_success_condition()),
        }
    ]
    assert step["next_state_key"] == migration.COMPLETED_STATE_KEY
    assert step["on_failure_state_key"] == migration.FAILED_STATE_KEY
    assert step["on_unknown_state_key"] == migration.FAILED_STATE_KEY

    readback = _step_with_action(candidate, "workflow_mcp.invoke_tool")
    assert readback["state_id"] == migration.VERIFY_EVIDENCE_STATE_KEY
    assert readback["concept_id"] == migration.VERIFY_EVIDENCE_STATE_ID
    assert {
        item["tool_param"]: item["value"] for item in readback["static_input_bindings"]
    } == {
        "tool_name": "find_relations_with_argument",
        "argument_index": "subject",
        "predicate_filter": [migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID],
        "relation_kind": "binary",
        "include_concept_preview": False,
        "include_text_snippets": False,
        "include_uncertain": False,
        "uncertainty_mode": "asserted_only",
        "limit": 80,
        "offset": 0,
    }
    assert readback["context_input_mappings"] == [
        migration._context_input_mapping(
            state_key=migration.VERIFY_EVIDENCE_STATE_KEY,
            tool_param="concept_id",
            context_key="file_copy_concept_id",
            required=True,
        )
    ]
    assert readback["tool_output_context_mappings"] == [
        migration._tool_output_mapping(
            state_key=migration.VERIFY_EVIDENCE_STATE_KEY,
            tool_output_field="result.concept_id",
            context_key=migration.EVIDENCE_READBACK_CONCEPT_ID_KEY,
        ),
        migration._tool_output_mapping(
            state_key=migration.VERIFY_EVIDENCE_STATE_KEY,
            tool_output_field="result.total_hits",
            context_key=migration.EVIDENCE_READBACK_TOTAL_HITS_KEY,
        ),
        migration._tool_output_mapping(
            state_key=migration.VERIFY_EVIDENCE_STATE_KEY,
            tool_output_field="result.hits",
            context_key=migration.EVIDENCE_READBACK_HITS_KEY,
        ),
        migration._tool_output_mapping(
            state_key=migration.VERIFY_EVIDENCE_STATE_KEY,
            tool_output_field="result.total_hits_is_lower_bound",
            context_key=migration.EVIDENCE_READBACK_TOTAL_HITS_LOWER_BOUND_KEY,
        ),
    ]
    assert readback["writes_context_keys"] == [
        migration.EVIDENCE_READBACK_CONCEPT_ID_KEY,
        migration.EVIDENCE_READBACK_TOTAL_HITS_KEY,
        migration.EVIDENCE_READBACK_HITS_KEY,
    ]
    assert readback["conditional_transitions"] == [
        {
            "to_state": migration.CORRELATE_EVIDENCE_STATE_KEY,
            "reason": "file_copy_evidence_canonical_readback_available",
            "condition_spec": {
                "kind": "all",
                "conditions": [
                    {
                        "kind": "context_flag",
                        "key": "last_action_succeeded",
                        "expected": True,
                    },
                    {
                        "kind": "context_exists",
                        "key": migration.EVIDENCE_READBACK_CONCEPT_ID_KEY,
                        "expected": True,
                    },
                    {
                        "kind": "context_exists",
                        "key": migration.EVIDENCE_READBACK_TOTAL_HITS_KEY,
                        "expected": True,
                    },
                    {
                        "kind": "context_exists",
                        "key": migration.EVIDENCE_READBACK_HITS_KEY,
                        "expected": True,
                    },
                ],
            },
        }
    ]
    assert readback["on_unknown_state_key"] == migration.FAILED_STATE_KEY
    assert readback["on_failure_state_key"] == migration.FAILED_STATE_KEY
    assert readback["next_state_key"] == migration.FAILED_STATE_KEY

    correlate = _step_with_action(
        candidate,
        "workflow_control.relationship_effect_readback",
    )
    assert correlate["state_id"] == migration.CORRELATE_EVIDENCE_STATE_KEY
    assert correlate["concept_id"] == migration.CORRELATE_EVIDENCE_STATE_ID
    assert {
        item["tool_param"]: item["value"] for item in correlate["static_input_bindings"]
    } == {
        "mutation_tool_name": "add_relationship",
        "expected_predicate_id": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
        "expected_relation_kind": "binary",
    }
    assert correlate["context_input_mappings"] == [
        migration._context_input_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_param="expected_source_id",
            context_key="file_copy_concept_id",
            required=True,
        ),
        migration._context_input_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_param="tool_invocations",
            context_key="tool_invocations",
            required=True,
        ),
        migration._context_input_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_param="readback_concept_id",
            context_key=migration.EVIDENCE_READBACK_CONCEPT_ID_KEY,
            required=True,
        ),
        migration._context_input_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_param="readback_total_hits",
            context_key=migration.EVIDENCE_READBACK_TOTAL_HITS_KEY,
            required=True,
        ),
        migration._context_input_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_param="readback_hits",
            context_key=migration.EVIDENCE_READBACK_HITS_KEY,
            required=True,
        ),
        migration._context_input_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_param="readback_total_hits_is_lower_bound",
            context_key=migration.EVIDENCE_READBACK_TOTAL_HITS_LOWER_BOUND_KEY,
            required=False,
        ),
    ]
    assert correlate["tool_output_context_mappings"] == [
        migration._tool_output_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_output_field="relationship_effect_readback_verified",
            context_key=migration.EVIDENCE_EFFECT_VERIFIED_KEY,
        ),
        migration._tool_output_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_output_field="represented_target_concept_id",
            context_key=migration.EVIDENCE_EFFECT_TARGET_KEY,
        ),
        migration._tool_output_mapping(
            state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
            tool_output_field="verified_relationship",
            context_key=migration.EVIDENCE_EFFECT_RELATIONSHIP_KEY,
        ),
    ]
    assert correlate["writes_context_keys"] == [
        migration.EVIDENCE_EFFECT_VERIFIED_KEY,
        migration.EVIDENCE_EFFECT_TARGET_KEY,
        migration.EVIDENCE_EFFECT_RELATIONSHIP_KEY,
    ]
    assert correlate["conditional_transitions"] == [
        {
            "to_state": migration.COMPLETED_STATE_KEY,
            "reason": "exact_file_copy_evidence_effect_verified",
            "condition_spec": {
                "kind": "all",
                "conditions": [
                    {
                        "kind": "context_flag",
                        "key": "last_action_succeeded",
                        "expected": True,
                    },
                    {
                        "kind": "context_flag",
                        "key": migration.EVIDENCE_EFFECT_VERIFIED_KEY,
                        "expected": True,
                    },
                    {
                        "kind": "context_exists",
                        "key": migration.EVIDENCE_EFFECT_TARGET_KEY,
                        "expected": True,
                    },
                ],
            },
        }
    ]
    assert correlate["on_unknown_state_key"] == migration.FAILED_STATE_KEY
    assert correlate["on_failure_state_key"] == migration.FAILED_STATE_KEY
    assert correlate["next_state_key"] == migration.FAILED_STATE_KEY

    policy = step["llm_policy"]
    assert "read_file_copy" in policy["allowed_tools"]
    assert "get_predicate_incidence" not in policy["allowed_tools"]
    assert policy["required_tools"] == ["search_concepts"]
    assert policy["tool_argument_defaults"]["read_file_copy"] == {
        "as_text": True,
        "allow_large": False,
        "max_bytes": migration.READ_FILE_COPY_MAX_BYTES,
    }
    assert policy["tool_argument_defaults"]["fetch_concept"] == {
        "include_concept_preview": True,
        "include_relations_arg1": False,
        "include_relations_any_arg": False,
        "include_text_relations_arg1": False,
        "limit": 8,
    }
    assert "get_predicate_incidence" not in policy["tool_argument_defaults"]
    assert policy["tool_argument_defaults"]["find_relations_with_argument"] == {
        "argument_index": "subject",
        "relation_kind": "binary",
        "include_concept_preview": False,
        "include_text_snippets": False,
        "limit": 8,
    }
    context_keys = {field.get("context_key") for field in policy["context_fields"]}
    assert "file_copy_concept_id" in context_keys
    assert "meeting_source_text" not in context_keys
    assert "meeting_source_filename" not in context_keys
    assert policy["prompt_text"].startswith(
        f"[Versioned migration: {migration.MIGRATION_ID}]"
    )
    assert step["prompt_contract"]["prompt_text"] == (
        migration.MEETING_REPRESENTATION_PROMPT
    )
    assert step["metadata"]["llm_policy"] == policy
    assert step["metadata"]["llm_policies"] == [policy]
    assert "#V#documentary_evidence_for" in policy["prompt_text"]
    assert "deterministic workflow" in policy["prompt_text"]
    assert "will fail the workflow" in policy["prompt_text"]
    assert "your first source action must be exactly one" in policy["prompt_text"]
    assert f"max_bytes={migration.READ_FILE_COPY_MAX_BYTES}" in policy["prompt_text"]
    assert "deterministic predecessor" not in policy["prompt_text"]

    effect = candidate["workflow_metadata"]["required_effects_contract"][
        "required_effects"
    ][0]
    assert effect["effect_type"] == "representation_meeting"
    assert effect["required_tools_match"] == "any"
    assert effect["required_tools"] == [
        "add_relationship",
        "upsert_text_relation",
        "upsert_singleton_text_relation",
    ]
    assert effect["activation_required_tools"] == []
    assert "fetch_concept" not in effect["required_tools"]
    assert "get_predicate_incidence" not in effect["required_tools"]
    assert "create_concepts" not in effect["required_tools"]

    definition = build_workflow_definition_from_authoring_spec(candidate)
    validation = validate_workflow_definition_contract(definition=definition)
    assert validation["valid"] is True
    assert definition.initial_state == "represent_meeting"
    assert migration.ROUTE_SOURCE_STATE_ID not in definition.states
    assert migration.READ_FILE_COPY_STATE_ID not in definition.states
    assert definition.states["represent_meeting"].actions[0].action_id == "llm.action"
    llm_transitions = definition.states["represent_meeting"].transitions
    assert llm_transitions[0].to_state == migration.VERIFY_EVIDENCE_STATE_KEY
    assert (
        llm_transitions[0].condition(
            {
                "file_copy_concept_id": "#V#uploaded_file_copy_example",
                "last_action_succeeded": True,
            }
        )
        is True
    )
    assert (
        llm_transitions[0].condition(
            {
                "file_copy_concept_id": "#V#uploaded_file_copy_example",
                "last_action_succeeded": False,
            }
        )
        is False
    )

    readback_state = definition.states[migration.VERIFY_EVIDENCE_STATE_KEY]
    assert readback_state.actions[0].action_id == "workflow_mcp.invoke_tool"
    assert readback_state.actions[0].inputs["concept_id"] == _compiled_context_binding(
        migration._context_input_mapping(
            state_key=migration.VERIFY_EVIDENCE_STATE_KEY,
            tool_param="concept_id",
            context_key="file_copy_concept_id",
            required=True,
        )
    )
    assert [transition.to_state for transition in readback_state.transitions] == [
        migration.CORRELATE_EVIDENCE_STATE_KEY,
        migration.FAILED_STATE_KEY,
        migration.FAILED_STATE_KEY,
        migration.FAILED_STATE_KEY,
    ]
    assert (
        readback_state.transitions[0].condition(
            {
                "last_action_succeeded": True,
                migration.EVIDENCE_READBACK_CONCEPT_ID_KEY: (
                    "#V#uploaded_file_copy_example"
                ),
                migration.EVIDENCE_READBACK_TOTAL_HITS_KEY: 1,
                migration.EVIDENCE_READBACK_HITS_KEY: [
                    {
                        "predicate_concept_id": (
                            migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID
                        ),
                        "relation_kind": "binary",
                        "target_value": "#V#meeting",
                    }
                ],
            }
        )
        is True
    )
    assert (
        readback_state.transitions[0].condition(
            {
                "last_action_succeeded": True,
                migration.EVIDENCE_READBACK_CONCEPT_ID_KEY: (
                    "#V#uploaded_file_copy_example"
                ),
                migration.EVIDENCE_READBACK_TOTAL_HITS_KEY: 0,
            }
        )
        is False
    )
    assert (
        readback_state.transitions[0].condition(
            {
                "last_action_succeeded": True,
                migration.EVIDENCE_READBACK_CONCEPT_ID_KEY: (
                    "#V#uploaded_file_copy_example"
                ),
                migration.EVIDENCE_READBACK_TOTAL_HITS_KEY: 0,
                migration.EVIDENCE_READBACK_HITS_KEY: [],
            }
        )
        is True
    )

    correlate_state = definition.states[migration.CORRELATE_EVIDENCE_STATE_KEY]
    correlate_action = correlate_state.actions[0]
    assert correlate_action.action_id == "workflow_control.relationship_effect_readback"
    assert correlate_action.inputs == {
        "mutation_tool_name": "add_relationship",
        "expected_predicate_id": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
        "expected_relation_kind": "binary",
        "expected_source_id": _compiled_context_binding(
            migration._context_input_mapping(
                state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
                tool_param="expected_source_id",
                context_key="file_copy_concept_id",
                required=True,
            )
        ),
        "tool_invocations": _compiled_context_binding(
            migration._context_input_mapping(
                state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
                tool_param="tool_invocations",
                context_key="tool_invocations",
                required=True,
            )
        ),
        "readback_concept_id": _compiled_context_binding(
            migration._context_input_mapping(
                state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
                tool_param="readback_concept_id",
                context_key=migration.EVIDENCE_READBACK_CONCEPT_ID_KEY,
                required=True,
            )
        ),
        "readback_total_hits": _compiled_context_binding(
            migration._context_input_mapping(
                state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
                tool_param="readback_total_hits",
                context_key=migration.EVIDENCE_READBACK_TOTAL_HITS_KEY,
                required=True,
            )
        ),
        "readback_hits": _compiled_context_binding(
            migration._context_input_mapping(
                state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
                tool_param="readback_hits",
                context_key=migration.EVIDENCE_READBACK_HITS_KEY,
                required=True,
            )
        ),
        "readback_total_hits_is_lower_bound": _compiled_context_binding(
            migration._context_input_mapping(
                state_key=migration.CORRELATE_EVIDENCE_STATE_KEY,
                tool_param="readback_total_hits_is_lower_bound",
                context_key=migration.EVIDENCE_READBACK_TOTAL_HITS_LOWER_BOUND_KEY,
                required=False,
            )
        ),
    }
    assert [transition.to_state for transition in correlate_state.transitions] == [
        migration.COMPLETED_STATE_KEY,
        migration.FAILED_STATE_KEY,
        migration.FAILED_STATE_KEY,
        migration.FAILED_STATE_KEY,
    ]
    assert (
        correlate_state.transitions[0].condition(
            {
                "last_action_succeeded": True,
                migration.EVIDENCE_EFFECT_VERIFIED_KEY: True,
                migration.EVIDENCE_EFFECT_TARGET_KEY: "#V#meeting",
            }
        )
        is True
    )
    assert (
        correlate_state.transitions[0].condition(
            {
                "last_action_succeeded": True,
                migration.EVIDENCE_EFFECT_VERIFIED_KEY: False,
                migration.EVIDENCE_EFFECT_TARGET_KEY: "#V#meeting",
            }
        )
        is False
    )

    second_candidate, second_changed = (
        migration.rewrite_meeting_representation_workflow(candidate)
    )
    assert second_changed is False
    assert second_candidate == candidate


def test_rewrite_is_idempotent_after_canonical_serialise_and_rebuild() -> None:
    candidate, changed = migration.rewrite_meeting_representation_workflow(
        _sample_authoring_spec()
    )
    assert changed is True

    definition = build_workflow_definition_from_authoring_spec(candidate)
    canonical_spec = serialise_workflow_definition_to_authoring_spec(definition)
    rebuilt_definition = build_workflow_definition_from_authoring_spec(canonical_spec)
    rebuilt_validation = validate_workflow_definition_contract(
        definition=rebuilt_definition
    )
    assert rebuilt_validation["valid"] is True

    rebuilt_spec = serialise_workflow_definition_to_authoring_spec(rebuilt_definition)
    assert rebuilt_spec == canonical_spec
    rewritten_spec, rewritten = migration.rewrite_meeting_representation_workflow(
        rebuilt_spec
    )
    assert rewritten is False
    assert rewritten_spec == rebuilt_spec


def test_rewrite_is_idempotent_after_vontology_loader_state_id_projection() -> None:
    candidate, changed = migration.rewrite_meeting_representation_workflow(
        _sample_authoring_spec()
    )
    assert changed is True

    # Graph publication stores transition targets as step concept IDs, and the
    # Vontology loader uses those concept IDs as the runtime state keys. The
    # loader therefore serialises each row with the concept ID in ``state_id``
    # and without the now-redundant authoring-only ``concept_id`` field.
    state_id_map = {
        step["state_id"]: step.get("concept_id")
        or (f"#V#workflow_step_meeting_representation_workflow_{step['state_id']}")
        for step in candidate["steps"]
    }
    loaded_spec = copy.deepcopy(candidate)
    loaded_spec["initial_state_key"] = state_id_map[loaded_spec["initial_state_key"]]
    transition_keys = (
        "next_state_key",
        "on_true_state_key",
        "on_false_state_key",
        "on_failure_state_key",
        "on_unknown_state_key",
        "on_approval_required_state_key",
        "on_break_state_key",
        "on_continue_state_key",
    )
    for step in loaded_spec["steps"]:
        logical_state_id = step["state_id"]
        step["state_id"] = state_id_map[logical_state_id]
        step.pop("concept_id", None)
        for transition_key in transition_keys:
            target = step.get(transition_key)
            if target in state_id_map:
                step[transition_key] = state_id_map[target]
        for transition in step.get("conditional_transitions") or []:
            target = transition.get("to_state")
            if target in state_id_map:
                transition["to_state"] = state_id_map[target]

    rewritten_spec, rewritten = migration.rewrite_meeting_representation_workflow(
        loaded_spec
    )

    assert rewritten is False
    assert rewritten_spec == loaded_spec
    llm_step = _step_with_action(rewritten_spec, "llm.action")
    readback_step = _step_with_action(rewritten_spec, "workflow_mcp.invoke_tool")
    correlate_step = _step_with_action(
        rewritten_spec,
        "workflow_control.relationship_effect_readback",
    )
    assert readback_step["state_id"] == migration.VERIFY_EVIDENCE_STATE_ID
    assert "concept_id" not in readback_step
    assert correlate_step["state_id"] == migration.CORRELATE_EVIDENCE_STATE_ID
    assert "concept_id" not in correlate_step
    assert llm_step["conditional_transitions"][0]["to_state"] == (
        migration.VERIFY_EVIDENCE_STATE_ID
    )
    assert readback_step["conditional_transitions"][0]["to_state"] == (
        migration.CORRELATE_EVIDENCE_STATE_ID
    )


@pytest.mark.parametrize(
    (
        "readback_target_id",
        "expected_final_state",
        "expected_completed",
        "expected_verified",
        "expected_failure_code",
    ),
    [
        (
            "#V#represented_meeting",
            migration.COMPLETED_STATE_KEY,
            True,
            True,
            None,
        ),
        (
            "#V#different_meeting",
            migration.FAILED_STATE_KEY,
            False,
            False,
            "relationship_effect_exact_readback_missing",
        ),
    ],
)
def test_migrated_workflow_correlates_llm_relationship_with_exact_readback(
    monkeypatch: pytest.MonkeyPatch,
    readback_target_id: str,
    expected_final_state: str,
    expected_completed: bool,
    expected_verified: bool,
    expected_failure_code: str | None,
) -> None:
    from src.backend.workflows import llm_step_executor

    file_copy_id = "#V#uploaded_meeting_file_copy"
    represented_meeting_id = "#V#represented_meeting"
    relation_id = "struct::uploaded-meeting-documentary-evidence"
    invocation = {
        "tool": "add_relationship",
        "status": "ok",
        "effective_arguments": {
            "source_id": file_copy_id,
            "predicate": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            "target": represented_meeting_id,
        },
        "effective_payload": {
            "success": True,
            "effect_status": "succeeded",
            "relationship_type": "concept_relation",
            "source_id": file_copy_id,
            "predicate": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            "predicate_input": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            "target": represented_meeting_id,
            "added": True,
            "changed": True,
        },
    }
    hit = {
        "access_granted": True,
        "canonical_publication": True,
        "is_asserted": True,
        "relation_state": "asserted",
        "source_concept_id": file_copy_id,
        "predicate_concept_id": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
        "target_value": readback_target_id,
        "relation_kind": "binary",
        "argument_indexes": [1],
        "relation_metadata": {
            "canonical_publication": True,
            "relation_id": relation_id,
        },
    }

    def _execute_llm_step(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        assert request.action_id == "llm.action"
        assert request.execution_mode == "llm"
        assert request.data["file_copy_concept_id"] == file_copy_id
        return WorkflowActionResult(
            status="success",
            outputs={"tool_invocations": [invocation]},
        )

    mcp_requests: list[WorkflowActionRequest] = []

    def _invoke_tool(request: WorkflowActionRequest) -> WorkflowActionResult:
        mcp_requests.append(request)
        assert request.inputs == {
            "tool_name": "find_relations_with_argument",
            "argument_index": "subject",
            "predicate_filter": [migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID],
            "relation_kind": "binary",
            "include_concept_preview": False,
            "include_text_snippets": False,
            "include_uncertain": False,
            "uncertainty_mode": "asserted_only",
            "limit": 80,
            "offset": 0,
            "concept_id": file_copy_id,
        }
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "concept_id": file_copy_id,
                    "total_hits": 1,
                    "total_hits_is_lower_bound": False,
                    "hits": [hit],
                }
            },
        )

    monkeypatch.setattr(llm_step_executor, "execute_llm_step", _execute_llm_step)
    candidate, changed = migration.rewrite_meeting_representation_workflow(
        _sample_authoring_spec()
    )
    assert changed is True
    definition = build_workflow_definition_from_authoring_spec(candidate)
    registry = ActionRegistry()
    register_control_flow_actions(
        registry,
        definition_loader=lambda _workflow_id: None,
    )
    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=_invoke_tool)
    )

    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "prompt": "Represent this uploaded meeting.",
            "file_copy_concept_id": file_copy_id,
        },
    )

    assert result.completed is expected_completed
    assert result.final_state == expected_final_state
    assert len(mcp_requests) == 1
    assert result.data[migration.EVIDENCE_READBACK_CONCEPT_ID_KEY] == file_copy_id
    assert result.data[migration.EVIDENCE_READBACK_TOTAL_HITS_KEY] == 1
    assert result.data[migration.EVIDENCE_READBACK_HITS_KEY] == [hit]
    assert result.data[migration.EVIDENCE_READBACK_TOTAL_HITS_LOWER_BOUND_KEY] is False
    assert result.data[migration.EVIDENCE_EFFECT_VERIFIED_KEY] is expected_verified
    assert (
        result.data["relationship_effect_readback_failure_code"]
        == expected_failure_code
    )
    assert result.data[migration.EVIDENCE_EFFECT_TARGET_KEY] == represented_meeting_id
    if expected_verified:
        assert result.data[migration.EVIDENCE_EFFECT_RELATIONSHIP_KEY] == {
            "source_id": file_copy_id,
            "predicate_id": migration.DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            "target_id": represented_meeting_id,
            "relation_kind": "binary",
            "relation_id": relation_id,
        }
    else:
        assert result.data[migration.EVIDENCE_EFFECT_RELATIONSHIP_KEY] is None


def test_preview_is_default_read_only_and_uses_actor_scoped_workflow_studio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_definition = object()
    previewed: dict = {}

    monkeypatch.setattr(
        migration,
        "override_current_actor",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        lambda workflow_id: (
            before_definition if workflow_id == migration.WORKFLOW_ID else None
        ),
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda definition: copy.deepcopy(_sample_authoring_spec()),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda definition: {"definition_hash": "before-hash"},
    )
    monkeypatch.setattr(
        migration,
        "_prompt_snapshot",
        lambda: _prompt_snapshot(
            "",
            relation_count=0,
            concept_exists=False,
        ),
    )

    def _preview(workflow_id, *, authoring_spec, base_definition_hash):
        previewed.update(
            {
                "workflow_id": workflow_id,
                "authoring_spec": copy.deepcopy(authoring_spec),
                "base_definition_hash": base_definition_hash,
            }
        )
        return {
            "preview": {
                "definition_identity": {"definition_hash": "candidate-hash"},
                "contract_validation": {"valid": True},
                "diff_summary": {"changed": True},
            }
        }

    monkeypatch.setattr(migration, "preview_workflow_authoring_spec", _preview)
    monkeypatch.setattr(
        migration,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: pytest.fail("preview must not publish workflow"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("preview must not publish prompt"),
    )
    monkeypatch.setattr(
        migration,
        "_create_public_prompt_concept",
        lambda: pytest.fail("preview must not create prompt concept"),
    )
    monkeypatch.setattr(
        migration,
        "suppress_event_workflow_launches",
        lambda _reason: pytest.fail("preview must not enter mutation scope"),
    )

    result = migration.run_migration(apply=False)

    assert result["mode"] == "preview"
    assert result["publishes_to_vontology"] is False
    assert result["contract_valid"] is True
    assert result["workflow_changed"] is True
    assert result["prompt_changed"] is True
    assert result["prompt_concept_creation_required"] is True
    assert previewed["workflow_id"] == migration.WORKFLOW_ID
    assert previewed["base_definition_hash"] == "before-hash"
    preview_llm_step = _step_with_action(previewed["authoring_spec"], "llm.action")
    assert "read_file_copy" in preview_llm_step["llm_policy"]["allowed_tools"]
    assert previewed["authoring_spec"]["initial_state_key"] == "represent_meeting"
    preview_state_ids = {
        step.get("state_id") for step in previewed["authoring_spec"]["steps"]
    }
    assert migration.ROUTE_SOURCE_STATE_ID not in preview_state_ids
    assert migration.READ_FILE_COPY_STATE_ID not in preview_state_ids


def test_apply_previews_then_publishes_prompt_and_verifies_canonical_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_definition = object()
    refreshed_definition = object()
    after_definition = object()
    before_spec = _sample_authoring_spec()
    candidate_spec, _changed = migration.rewrite_meeting_representation_workflow(
        before_spec
    )
    refreshed_spec = copy.deepcopy(before_spec)
    refreshed_spec["description"] = "Canonical workflow after prompt publication."
    refreshed_candidate_spec, refreshed_changed = (
        migration.rewrite_meeting_representation_workflow(refreshed_spec)
    )
    assert refreshed_changed is True
    load_results = iter(
        [
            ("load_before", before_definition),
            ("load_refreshed", refreshed_definition),
            ("load_after", after_definition),
        ]
    )
    prompt_results = iter(
        [
            _prompt_snapshot("", relation_count=0, concept_exists=False),
            _prompt_snapshot(migration.MEETING_REPRESENTATION_PROMPT),
        ]
    )
    events: list[str] = []
    actor_context: dict = {}
    prompt_upsert: dict = {}
    prompt_create: dict = {}
    apply_call: dict = {}

    def _actor(**kwargs):
        actor_context.update(kwargs)
        return nullcontext()

    monkeypatch.setattr(migration, "override_current_actor", _actor)

    def _load(_workflow_id):
        event, definition = next(load_results)
        events.append(event)
        return definition

    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        _load,
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda definition: copy.deepcopy(
            before_spec
            if definition is before_definition
            else refreshed_spec
            if definition is refreshed_definition
            else refreshed_candidate_spec
        ),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda definition: {
            "definition_hash": (
                "before-hash"
                if definition is before_definition
                else "refreshed-base-hash"
                if definition is refreshed_definition
                else "after-hash"
            )
        },
    )
    monkeypatch.setattr(migration, "_prompt_snapshot", lambda: next(prompt_results))

    preview_calls: list[dict] = []

    def _preview(workflow_id, *, authoring_spec, base_definition_hash):
        preview_calls.append(
            {
                "workflow_id": workflow_id,
                "authoring_spec": copy.deepcopy(authoring_spec),
                "base_definition_hash": base_definition_hash,
            }
        )
        is_refreshed = len(preview_calls) == 2
        events.append("refreshed_preview" if is_refreshed else "preview")
        return {
            "preview": {
                "definition_identity": {
                    "definition_hash": (
                        "refreshed-candidate-hash" if is_refreshed else "candidate-hash"
                    )
                },
                "contract_validation": {"valid": True},
                "diff_summary": {"changed": True},
            }
        }

    def _apply(workflow_id, *, authoring_spec, base_definition_hash):
        events.append("apply_workflow")
        apply_call.update(
            {
                "workflow_id": workflow_id,
                "authoring_spec": copy.deepcopy(authoring_spec),
                "base_definition_hash": base_definition_hash,
            }
        )
        return {
            "publication": {
                "counts": {"errors": 0},
                "published_workflow_ids": [migration.WORKFLOW_ID],
            }
        }

    def _upsert(**kwargs):
        events.append("upsert_prompt")
        prompt_upsert.update(kwargs)
        return {"relation_id": "prompt-relation"}

    @contextmanager
    def _suppression(reason):
        events.append(f"suppress_enter:{reason}")
        try:
            yield
        finally:
            events.append("suppress_exit")

    def _create_prompt():
        events.append("create_prompt")
        prompt_create.update(
            {
                "name": migration.PROMPT_CONCEPT_NAME,
                "concept_id": migration.PROMPT_CONCEPT_ID,
                "parent_concept_ids": [migration.PROMPT_TYPE_CONCEPT_ID],
                "create_as_instance": True,
                "visibility_scope_mode": "global_general",
            }
        )

    monkeypatch.setattr(migration, "preview_workflow_authoring_spec", _preview)
    monkeypatch.setattr(migration, "apply_workflow_authoring_spec", _apply)
    monkeypatch.setattr(migration, "upsert_singleton_text_relation", _upsert)
    monkeypatch.setattr(migration, "_create_public_prompt_concept", _create_prompt)
    monkeypatch.setattr(migration, "suppress_event_workflow_launches", _suppression)

    result = migration.run_migration(apply=True)

    assert events == [
        "load_before",
        "preview",
        f"suppress_enter:{migration.MIGRATION_ID}",
        "create_prompt",
        "upsert_prompt",
        "load_refreshed",
        "refreshed_preview",
        "apply_workflow",
        "suppress_exit",
        "load_after",
    ]
    assert actor_context == {
        "user_concept_id": migration.DEFAULT_ACTOR_USER_ID,
        "organisation_concept_id": (migration.DEFAULT_ACTOR_ORGANISATION_ID),
    }
    assert prompt_upsert["subject_concept_id"] == migration.PROMPT_CONCEPT_ID
    assert prompt_upsert["predicate"] == migration.PROMPT_PREDICATE
    assert prompt_upsert["text"] == migration.MEETING_REPRESENTATION_PROMPT
    assert prompt_upsert["context"]["migration_id"] == migration.MIGRATION_ID
    assert prompt_create == {
        "name": migration.PROMPT_CONCEPT_NAME,
        "concept_id": migration.PROMPT_CONCEPT_ID,
        "parent_concept_ids": [migration.PROMPT_TYPE_CONCEPT_ID],
        "create_as_instance": True,
        "visibility_scope_mode": "global_general",
    }
    assert result["prompt_concept_creation"] == {
        "concept_id": migration.PROMPT_CONCEPT_ID,
        "created": True,
        "parent_concept_id": migration.PROMPT_TYPE_CONCEPT_ID,
        "visibility_scope_mode": "global_general",
    }
    assert result["mode"] == "apply"
    assert result["refreshed_base_definition_hash"] == "refreshed-base-hash"
    assert result["refreshed_candidate_definition_hash"] == "refreshed-candidate-hash"
    assert result["refreshed_contract_valid"] is True
    assert preview_calls[1] == {
        "workflow_id": migration.WORKFLOW_ID,
        "authoring_spec": refreshed_candidate_spec,
        "base_definition_hash": "refreshed-base-hash",
    }
    assert apply_call == preview_calls[1]
    assert apply_call["authoring_spec"] != candidate_spec
    assert result["canonical_readback_verified"] is True
    assert result["readback_definition_hash"] == "after-hash"


def test_apply_rejects_invalid_preview_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migration,
        "override_current_actor",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: object(),
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: _sample_authoring_spec(),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda _definition: {"definition_hash": "before-hash"},
    )
    monkeypatch.setattr(
        migration,
        "_prompt_snapshot",
        lambda: _prompt_snapshot("", relation_count=0, concept_exists=False),
    )
    monkeypatch.setattr(
        migration,
        "preview_workflow_authoring_spec",
        lambda *_args, **_kwargs: {
            "preview": {
                "definition_identity": {"definition_hash": "candidate-hash"},
                "contract_validation": {
                    "valid": False,
                    "errors": ["invalid candidate"],
                },
            }
        },
    )
    monkeypatch.setattr(
        migration,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: pytest.fail("invalid preview must not publish"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("invalid preview must not update prompt"),
    )
    monkeypatch.setattr(
        migration,
        "_create_public_prompt_concept",
        lambda: pytest.fail("invalid preview must not create prompt"),
    )
    monkeypatch.setattr(
        migration,
        "suppress_event_workflow_launches",
        lambda _reason: pytest.fail("invalid preview must not enter mutation scope"),
    )

    with pytest.raises(ValueError, match="workflow_authoring_preview_invalid"):
        migration.run_migration(apply=True)


def test_apply_already_current_is_idempotent_without_mutation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_definition = object()
    current_spec, changed = migration.rewrite_meeting_representation_workflow(
        _sample_authoring_spec()
    )
    assert changed is True
    load_calls: list[str] = []
    preview_calls: list[str] = []

    monkeypatch.setattr(
        migration,
        "override_current_actor",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        lambda workflow_id: load_calls.append(workflow_id) or current_definition,
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: copy.deepcopy(current_spec),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda _definition: {"definition_hash": "current-hash"},
    )
    monkeypatch.setattr(
        migration,
        "_prompt_snapshot",
        lambda: _prompt_snapshot(migration.MEETING_REPRESENTATION_PROMPT),
    )
    monkeypatch.setattr(
        migration,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: (
            preview_calls.append(workflow_id)
            or {
                "preview": {
                    "definition_identity": {"definition_hash": "current-hash"},
                    "contract_validation": {"valid": True},
                    "diff_summary": {"changed": False},
                }
            }
        ),
    )
    monkeypatch.setattr(
        migration,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: pytest.fail("current workflow must not publish"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("current prompt must not upsert"),
    )
    monkeypatch.setattr(
        migration,
        "_create_public_prompt_concept",
        lambda: pytest.fail("current prompt concept must not be created"),
    )
    monkeypatch.setattr(
        migration,
        "suppress_event_workflow_launches",
        lambda _reason: pytest.fail("current apply must not enter mutation scope"),
    )

    result = migration.run_migration(apply=True)

    assert result["changed"] is False
    assert result["workflow_publication_skipped"] == "already_current"
    assert result["prompt_publication_skipped"] == "already_current"
    assert result["prompt_concept_creation"]["created"] is False
    assert result["canonical_readback_verified"] is True
    assert load_calls == [migration.WORKFLOW_ID, migration.WORKFLOW_ID]
    assert preview_calls == [migration.WORKFLOW_ID]
