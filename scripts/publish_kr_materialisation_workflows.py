"""Publish and verify the KR design materialisation workflow family.

This script is intentionally a small, concrete publication template for future
Vontology-authored workflow families that need the same shape of work: define a
bounded VWL workflow family, validate it with the generation-safe contract,
publish it through the workflow concept authority service, persist policy
metadata, and read it back from Vontology. It is useful as a starting point for
future workflow-family materialisation tasks where Python should remain a
support surface and the durable behaviour should live in Vontology/VWL.

Write commands are guarded by ``VON_DB_NAME=von_db``. Validation and verify
commands are safe diagnostics, but publish commands intentionally mutate the
live Vontology workflow graph.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

MAIN_ID = "#V#kr_design_materialisation_workflow"
CONCEPT_ITEM_ID = "#V#kr_design_concept_materialisation_item_workflow"
RELATION_ITEM_ID = "#V#kr_design_relationship_assertion_item_workflow"

TERMINAL_SUCCESS_CONTRACT = {
    "schema_version": "workflow_terminal_success_contract.v1",
    "success_statuses": ["completed"],
    "required_summary_fields": [
        "workflow_id",
        "terminal_status",
        "final_state",
        "completed",
    ],
    "require_terminal_status": True,
    "require_final_state": True,
    "require_completed_true": True,
    "require_completion_gate_safe_to_claim_completion": False,
}

CONCEPT_REQUIRED_EFFECTS_CONTRACT = {
    "schema_version": "workflow_required_effects_contract.v1",
    "contract_id": "kr_concept_materialisation_mutation_readback",
    "required_effects": [
        {
            "effect_id": "kr_concept_mutation_readback",
            "effect_type": "grounded_evidence",
            "description": (
                "Each concept item must create or reuse a concept, assert the "
                "resolved type/instance parent relation, attach a description, "
                "and read back concept plus text relation evidence before success."
            ),
            "required_tools": [
                "create_concepts",
                "add_relationship",
                "upsert_singleton_text_relation",
                "fetch_concept",
                "get_text_relations_summary",
            ],
            "required_tools_match": "all",
            "missing_failure_code": "kr_concept_mutation_readback_missing",
            "failed_failure_code": "kr_concept_mutation_readback_failed",
            "not_executed_reason": "Concept materialisation or read-back did not run.",
            "not_satisfied_reason": "Concept materialisation was not verified from Vontology read-back.",
        }
    ],
}

RELATION_REQUIRED_EFFECTS_CONTRACT = {
    "schema_version": "workflow_required_effects_contract.v1",
    "contract_id": "kr_relationship_assertion_readback",
    "required_effects": [
        {
            "effect_id": "kr_relationship_mutation_readback",
            "effect_type": "grounded_evidence",
            "description": (
                "Each relationship item must assert a concept-to-concept edge "
                "with add_relationship and read back both endpoints before success."
            ),
            "required_tools": ["add_relationship", "fetch_concept"],
            "required_tools_match": "all",
            "missing_failure_code": "kr_relationship_mutation_readback_missing",
            "failed_failure_code": "kr_relationship_mutation_readback_failed",
            "not_executed_reason": "Relationship assertion or endpoint read-back did not run.",
            "not_satisfied_reason": "Relationship assertion was not verified from endpoint read-back.",
        }
    ],
}

MAIN_REQUIRED_EFFECTS_CONTRACT = {
    "schema_version": "workflow_required_effects_contract.v1",
    "contract_id": "kr_design_materialisation_mutation_readback",
    "required_effects": [
        {
            "effect_id": "kr_design_concept_and_relationship_readback",
            "effect_type": "grounded_evidence",
            "description": (
                "The workflow must materialise KR concepts through create_concepts, "
                "assert type/instance and requested relationship edges, attach "
                "descriptions, and carry read-back evidence before it may claim success."
            ),
            "required_tools": [
                "create_concepts",
                "add_relationship",
                "upsert_singleton_text_relation",
                "fetch_concept",
                "get_text_relations_summary",
            ],
            "required_tools_match": "all",
            "missing_failure_code": "kr_design_materialisation_evidence_missing",
            "failed_failure_code": "kr_design_materialisation_evidence_failed",
            "not_executed_reason": "KR concept/relationship mutation or read-back did not run.",
            "not_satisfied_reason": "KR materialisation was not verified from Vontology read-back.",
        }
    ],
}


def mappings(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"tool_output_field": source, "context_key": target}
        for source, target in pairs
    ]


def step(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    mapping_rows = result.get("tool_output_context_mappings")
    if isinstance(mapping_rows, list) and mapping_rows:
        result.setdefault("tool_output_mapping_specs", deepcopy(mapping_rows))
    return result


def context_exists_non_null(key: str) -> dict[str, Any]:
    return {
        "kind": "all",
        "conditions": [
            {"kind": "context_exists", "key": key, "expected": True},
            {"kind": "context_is_null", "key": key, "expected": False},
        ],
    }


def workflow_metadata(*, routing_eligible: bool, main: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "routing_profile": {
            "schema_version": "workflow_routing_profile.v1",
            "role": "execution",
            "authoring_intent_required": False,
            "explicit_workflow_context_required": False,
            "prefer_existing_capability": False,
            "routing_eligible": routing_eligible,
        },
        "terminal_success_contract": deepcopy(TERMINAL_SUCCESS_CONTRACT),
    }
    if main:
        metadata.update(
            {
                "required_effects_contract": deepcopy(MAIN_REQUIRED_EFFECTS_CONTRACT),
                "discovery_exemplars": {
                    "schema_version": "workflow_discovery_exemplars.v1",
                    "keywords": [
                        "KR materialisation",
                        "knowledge representation design",
                        "ontology design",
                        "create Vontology concepts",
                        "create Vontology relationships",
                        "materialise taxonomy",
                        "materialise ontology",
                    ],
                    "examples": [
                        "Materialise this KR design into Vontology concepts and relationships.",
                        "Create the ontology taxonomy from this design and verify it in Vontology.",
                        "Turn the proposed knowledge representation into durable Vontology structure.",
                    ],
                },
                "launch_input_contract": {
                    "schema_version": "workflow_launch_input_contract.v1",
                    "required_inputs": ["prompt"],
                    "optional_inputs": [
                        "user_concept_id",
                        "org_concept_id",
                        "turn_expected_outcome_contract_state",
                        "turn_expected_grounding_requirement",
                        "workflow_success_guidance_history",
                        "workflow_failure_avoidance_history",
                    ],
                    "input_mappings": [
                        {
                            "target_context_key": "prompt",
                            "source_expression": "inputs.prompt",
                            "extractor": "identity",
                            "required": True,
                            "description": "KR design text or instruction to materialise.",
                        },
                        {
                            "target_context_key": "user_prompt",
                            "source_expression": "inputs.prompt",
                            "extractor": "identity",
                            "required": False,
                            "description": "Alias for prompt-aware tooling.",
                        },
                    ],
                },
            }
        )
    else:
        metadata["launch_input_contract"] = {
            "schema_version": "workflow_launch_input_contract.v1",
            "required_inputs": ["current_kr_concept_spec"],
            "optional_inputs": ["user_concept_id", "org_concept_id"],
            "input_mappings": [
                {
                    "target_context_key": "current_kr_concept_spec",
                    "source_expression": "inputs.current_kr_concept_spec",
                    "extractor": "identity",
                    "required": True,
                    "description": "Concept item passed by the parent for-each workflow.",
                },
                {
                    "target_context_key": "user_concept_id",
                    "source_expression": "inputs.user_concept_id",
                    "extractor": "identity",
                    "required": False,
                    "description": "Optional authenticated user concept for additive writes.",
                },
                {
                    "target_context_key": "org_concept_id",
                    "source_expression": "inputs.org_concept_id",
                    "extractor": "identity",
                    "required": False,
                    "description": "Optional authenticated organisation concept for additive writes.",
                },
            ],
        }
    return metadata


def concept_item_spec() -> dict[str, Any]:
    return {
        "workflow_id": CONCEPT_ITEM_ID,
        "description": (
            "Materialise one bounded KR concept specification into Vontology, "
            "including the correct type or instance parent edge, description text, "
            "and read-back evidence."
        ),
        "initial_state_key": "initialise_from_concept_spec",
        "workflow_metadata": {
            **workflow_metadata(routing_eligible=False),
            "required_effects_contract": deepcopy(CONCEPT_REQUIRED_EFFECTS_CONTRACT),
        },
        "steps": [
            step(
                {
                    "state_id": "initialise_from_concept_spec",
                    "action_id": "workflow_control.context_set",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "kr_concept_key",
                                    "value_from_context_options": [
                                        "current_kr_concept_spec.key",
                                        "current_kr_concept_spec.target_key",
                                        "current_kr_concept_spec.target_name",
                                        "current_kr_concept_spec.name",
                                    ],
                                },
                                {
                                    "key": "kr_concept_decision",
                                    "value_from_context": "current_kr_concept_spec.decision",
                                },
                                {
                                    "key": "kr_concept_name",
                                    "value_from_context_options": [
                                        "current_kr_concept_spec.target_name",
                                        "current_kr_concept_spec.name",
                                    ],
                                },
                                {
                                    "key": "kr_concept_kind",
                                    "value_from_context_options": [
                                        "current_kr_concept_spec.target_kind",
                                        "current_kr_concept_spec.kind",
                                        "current_kr_concept_spec.kind_hint",
                                    ],
                                },
                                {
                                    "key": "kr_concept_parent_id",
                                    "value_from_context": "current_kr_concept_spec.parent_id",
                                },
                                {
                                    "key": "kr_concept_id",
                                    "value_from_context_options": [
                                        "current_kr_concept_spec.existing_concept_id",
                                        "current_kr_concept_spec.concept_id",
                                    ],
                                    "skip_if_unresolved": True,
                                },
                                {
                                    "key": "kr_concept_create_concepts",
                                    "value_from_context": "current_kr_concept_spec.concepts",
                                    "skip_if_unresolved": True,
                                },
                                {
                                    "key": "kr_concept_description_text",
                                    "value_from_context_options": [
                                        "current_kr_concept_spec.description_text",
                                        "current_kr_concept_spec.description",
                                    ],
                                },
                                {
                                    "key": "kr_concept_parent_rationale",
                                    "value_from_context": "current_kr_concept_spec.parent_rationale",
                                    "skip_if_unresolved": True,
                                },
                                {
                                    "key": "kr_concept_blocking_reason",
                                    "value_from_context": "current_kr_concept_spec.blocking_reason",
                                    "skip_if_unresolved": True,
                                },
                            ],
                        }
                    ],
                    "writes_context_keys": [
                        "kr_concept_key",
                        "kr_concept_decision",
                        "kr_concept_name",
                        "kr_concept_kind",
                        "kr_concept_parent_id",
                        "kr_concept_id",
                        "kr_concept_create_concepts",
                        "kr_concept_description_text",
                        "kr_concept_parent_rationale",
                        "kr_concept_blocking_reason",
                    ],
                    "conditional_transitions": [
                        {
                            "to_state": "route_parent_assertion",
                            "reason": "reuse_existing_concept_ready",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    {
                                        "kind": "context_value_equals",
                                        "key": "kr_concept_decision",
                                        "value": "reuse_existing",
                                    },
                                    context_exists_non_null("kr_concept_id"),
                                    context_exists_non_null("kr_concept_parent_id"),
                                ],
                            },
                        },
                        {
                            "to_state": "create_concept",
                            "reason": "create_concept_parent_ready",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    {
                                        "kind": "context_value_equals",
                                        "key": "kr_concept_decision",
                                        "value": "create",
                                    },
                                    context_exists_non_null("kr_concept_parent_id"),
                                    {
                                        "kind": "context_cardinality",
                                        "key": "kr_concept_create_concepts",
                                        "operator": ">",
                                        "value": 0,
                                    },
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "concept_spec_not_materialisable",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                }
            ),
            step(
                {
                    "state_id": "create_concept",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {
                            "tool_param": "parent_id",
                            "context_key": "kr_concept_parent_id",
                            "required": True,
                        },
                        {
                            "tool_param": "concepts",
                            "context_key": "kr_concept_create_concepts",
                            "required": True,
                        },
                        {
                            "tool_param": "created_by_concept_id",
                            "context_key": "user_concept_id",
                            "required": False,
                        },
                        {
                            "tool_param": "organisation_concept_id",
                            "context_key": "org_concept_id",
                            "required": False,
                        },
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "create_concepts"},
                        {"key": "scope_mode", "value": "user_org_default"},
                        {"key": "allow_duplicate_instances", "value": False},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("created_concept_ids", "kr_concept_created_ids"),
                        ("results", "kr_concept_create_results"),
                        ("parent_id_used", "kr_concept_parent_id_used"),
                        ("scope_selection", "kr_concept_scope_selection"),
                    ),
                    "writes_context_keys": [
                        "kr_concept_created_ids",
                        "kr_concept_create_results",
                        "kr_concept_parent_id_used",
                        "kr_concept_scope_selection",
                    ],
                    "mutation_authority": {
                        "schema_version": "workflow_step_mutation_authority.v1",
                        "maximum_level": "additive_vontology",
                        "reason_code": "kr_concept_additive_create",
                    },
                    "next_state": "resolve_concept_id",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "resolve_concept_id",
                    "action_id": "workflow_control.context_set",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "kr_concept_id",
                                    "preserve_existing": True,
                                    "value_from_context_options": [
                                        "kr_concept_created_ids.0",
                                        "kr_concept_create_results.0.concept_id",
                                        "kr_concept_create_results.0.canonical_concept_id",
                                        "kr_concept_create_results.0.existing_concept_id",
                                    ],
                                }
                            ],
                        }
                    ],
                    "writes_context_keys": ["kr_concept_id"],
                    "conditional_transitions": [
                        {
                            "to_state": "route_parent_assertion",
                            "reason": "concept_id_resolved",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    context_exists_non_null("kr_concept_id"),
                                    context_exists_non_null("kr_concept_parent_id"),
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "concept_id_missing_after_create",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "route_parent_assertion",
                    "action_id": "workflow_control.context_set",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "kr_concept_parent_id",
                                    "preserve_existing": True,
                                    "value_from_context": "kr_concept_parent_id_used",
                                    "skip_if_unresolved": True,
                                }
                            ],
                        }
                    ],
                    "writes_context_keys": ["kr_concept_parent_id"],
                    "conditional_transitions": [
                        {
                            "to_state": "assert_instance_parent_relationship",
                            "reason": "instance_or_predicate_parent_edge",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    context_exists_non_null("kr_concept_id"),
                                    context_exists_non_null("kr_concept_parent_id"),
                                    {
                                        "kind": "any",
                                        "conditions": [
                                            {
                                                "kind": "context_value_equals",
                                                "key": "kr_concept_kind",
                                                "value": "instance",
                                            },
                                            {
                                                "kind": "context_value_equals",
                                                "key": "kr_concept_kind",
                                                "value": "individual",
                                            },
                                            {
                                                "kind": "context_value_equals",
                                                "key": "kr_concept_kind",
                                                "value": "predicate",
                                            },
                                        ],
                                    },
                                ],
                            },
                        },
                        {
                            "to_state": "assert_type_parent_relationship",
                            "reason": "type_parent_edge",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    context_exists_non_null("kr_concept_id"),
                                    context_exists_non_null("kr_concept_parent_id"),
                                    {
                                        "kind": "context_value_equals",
                                        "key": "kr_concept_kind",
                                        "value": "type",
                                    },
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "concept_kind_not_supported",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "assert_type_parent_relationship",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {"tool_param": "source_id", "context_key": "kr_concept_id", "required": True},
                        {"tool_param": "target", "context_key": "kr_concept_parent_id", "required": True},
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "add_relationship"},
                        {"key": "predicate", "value": "type_of"},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("success", "kr_parent_relationship_success"),
                        ("predicate", "kr_parent_relationship_predicate"),
                        ("target", "kr_parent_relationship_target"),
                    ),
                    "writes_context_keys": [
                        "kr_parent_relationship_success",
                        "kr_parent_relationship_predicate",
                        "kr_parent_relationship_target",
                    ],
                    "mutation_authority": {
                        "schema_version": "workflow_step_mutation_authority.v1",
                        "maximum_level": "additive_vontology",
                        "reason_code": "kr_type_parent_relationship_write",
                    },
                    "next_state": "attach_description",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "assert_instance_parent_relationship",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {"tool_param": "source_id", "context_key": "kr_concept_id", "required": True},
                        {"tool_param": "target", "context_key": "kr_concept_parent_id", "required": True},
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "add_relationship"},
                        {"key": "predicate", "value": "instance_of"},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("success", "kr_parent_relationship_success"),
                        ("predicate", "kr_parent_relationship_predicate"),
                        ("target", "kr_parent_relationship_target"),
                    ),
                    "writes_context_keys": [
                        "kr_parent_relationship_success",
                        "kr_parent_relationship_predicate",
                        "kr_parent_relationship_target",
                    ],
                    "mutation_authority": {
                        "schema_version": "workflow_step_mutation_authority.v1",
                        "maximum_level": "additive_vontology",
                        "reason_code": "kr_instance_parent_relationship_write",
                    },
                    "next_state": "attach_description",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "attach_description",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {"tool_param": "concept_id", "context_key": "kr_concept_id", "required": True},
                        {"tool_param": "text", "context_key": "kr_concept_description_text", "required": True},
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "upsert_singleton_text_relation"},
                        {"key": "predicate", "value": "hasDescription"},
                        {"key": "language", "value": "en-NZ"},
                        {"key": "policy", "value": "replace_others"},
                        {"key": "garbage_collect", "value": True},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("relation_id", "kr_description_relation_id"),
                        ("text_value_id", "kr_description_text_value_id"),
                    ),
                    "writes_context_keys": [
                        "kr_description_relation_id",
                        "kr_description_text_value_id",
                    ],
                    "mutation_authority": {
                        "schema_version": "workflow_step_mutation_authority.v1",
                        "maximum_level": "additive_vontology",
                        "reason_code": "kr_concept_description_write",
                    },
                    "next_state": "read_back_concept",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "read_back_concept",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {"tool_param": "concept_id", "context_key": "kr_concept_id", "required": True}
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "fetch_concept"},
                        {"key": "include_relations_any_arg", "value": True},
                        {"key": "include_text_relations_arg1", "value": True},
                        {"key": "limit", "value": 40},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("concept_id", "kr_readback_concept_id"),
                        ("relationships", "kr_readback_relationships"),
                    ),
                    "writes_context_keys": [
                        "kr_readback_concept_id",
                        "kr_readback_relationships",
                    ],
                    "next_state": "read_back_text_relations",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "read_back_text_relations",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {"tool_param": "concept_id", "context_key": "kr_concept_id", "required": True}
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "get_text_relations_summary"},
                        {"key": "predicates", "value": ["hasDescription"]},
                        {"key": "languages", "value": ["en-NZ"]},
                        {"key": "max_relation_ids_per_group", "value": 10},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("groups", "kr_text_relation_groups"),
                        ("groups_found", "kr_text_relation_groups_found"),
                    ),
                    "writes_context_keys": [
                        "kr_text_relation_groups",
                        "kr_text_relation_groups_found",
                    ],
                    "next_state": "emit_concept_payload",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "emit_concept_payload",
                    "action_id": "workflow_control.context_set",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {"key": "kr_concept_key", "value_from_context": "kr_concept_key"},
                                {"key": "kr_concept_id", "value_from_context": "kr_concept_id"},
                                {"key": "kr_concept_name", "value_from_context": "kr_concept_name"},
                                {"key": "kr_concept_kind", "value_from_context": "kr_concept_kind"},
                                {"key": "kr_concept_parent_id", "value_from_context": "kr_concept_parent_id"},
                                {
                                    "key": "kr_parent_relationship_predicate",
                                    "value_from_context": "kr_parent_relationship_predicate",
                                },
                                {
                                    "key": "kr_description_relation_id",
                                    "value_from_context": "kr_description_relation_id",
                                },
                                {
                                    "key": "kr_readback_concept_id",
                                    "value_from_context": "kr_readback_concept_id",
                                },
                            ],
                        }
                    ],
                    "writes_context_keys": [
                        "kr_concept_key",
                        "kr_concept_id",
                        "kr_concept_name",
                        "kr_concept_kind",
                        "kr_concept_parent_id",
                        "kr_parent_relationship_predicate",
                        "kr_description_relation_id",
                        "kr_readback_concept_id",
                    ],
                    "next_state": "completed",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "summarise_blocker",
                    "action_id": "workflow_control.context_template",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "response_text",
                                    "template": "KR concept item was not materialised. Concept: {name}. Blocker: {blocker}.",
                                    "variables": {
                                        "name": {"value_from_context": "kr_concept_name", "default": "unresolved"},
                                        "blocker": {
                                            "value_from_context_options": [
                                                "kr_concept_blocking_reason",
                                                "last_action_error",
                                            ],
                                            "default": "materialisation_preconditions_not_met",
                                        },
                                    },
                                }
                            ],
                        }
                    ],
                    "writes_context_keys": ["response_text"],
                    "next_state": "failed",
                    "on_failure_state": "failed",
                }
            ),
            {"state_id": "completed"},
            {"state_id": "failed"},
        ],
    }


def relation_item_spec() -> dict[str, Any]:
    metadata = workflow_metadata(routing_eligible=False)
    metadata["required_effects_contract"] = deepcopy(RELATION_REQUIRED_EFFECTS_CONTRACT)
    metadata["launch_input_contract"] = {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": ["current_kr_relationship_spec"],
        "optional_inputs": ["user_concept_id", "org_concept_id"],
        "input_mappings": [
            {
                "target_context_key": "current_kr_relationship_spec",
                "source_expression": "inputs.current_kr_relationship_spec",
                "extractor": "identity",
                "required": True,
                "description": "Relationship item passed by the parent for-each workflow.",
            },
            {
                "target_context_key": "user_concept_id",
                "source_expression": "inputs.user_concept_id",
                "extractor": "identity",
                "required": False,
                "description": "Optional authenticated user concept for additive writes.",
            },
            {
                "target_context_key": "org_concept_id",
                "source_expression": "inputs.org_concept_id",
                "extractor": "identity",
                "required": False,
                "description": "Optional authenticated organisation concept for additive writes.",
            },
        ],
    }
    return {
        "workflow_id": RELATION_ITEM_ID,
        "description": (
            "Assert one resolved KR relationship specification as a Vontology "
            "concept-to-concept edge and read back both endpoints."
        ),
        "initial_state_key": "initialise_from_relationship_spec",
        "workflow_metadata": metadata,
        "steps": [
            step(
                {
                    "state_id": "initialise_from_relationship_spec",
                    "action_id": "workflow_control.context_set",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "kr_relationship_key",
                                    "value_from_context_options": [
                                        "current_kr_relationship_spec.key",
                                        "current_kr_relationship_spec.relationship_key",
                                    ],
                                    "skip_if_unresolved": True,
                                },
                                {
                                    "key": "kr_relationship_source_id",
                                    "value_from_context": "current_kr_relationship_spec.source_id",
                                },
                                {
                                    "key": "kr_relationship_predicate",
                                    "value_from_context": "current_kr_relationship_spec.predicate",
                                },
                                {
                                    "key": "kr_relationship_target_id",
                                    "value_from_context": "current_kr_relationship_spec.target_id",
                                },
                                {
                                    "key": "kr_relationship_rationale",
                                    "value_from_context": "current_kr_relationship_spec.rationale",
                                    "skip_if_unresolved": True,
                                },
                                {
                                    "key": "kr_relationship_blocking_reason",
                                    "value_from_context": "current_kr_relationship_spec.blocking_reason",
                                    "skip_if_unresolved": True,
                                },
                            ],
                        }
                    ],
                    "writes_context_keys": [
                        "kr_relationship_key",
                        "kr_relationship_source_id",
                        "kr_relationship_predicate",
                        "kr_relationship_target_id",
                        "kr_relationship_rationale",
                        "kr_relationship_blocking_reason",
                    ],
                    "conditional_transitions": [
                        {
                            "to_state": "assert_relationship",
                            "reason": "relationship_spec_ready",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    context_exists_non_null("kr_relationship_source_id"),
                                    context_exists_non_null("kr_relationship_predicate"),
                                    context_exists_non_null("kr_relationship_target_id"),
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "relationship_spec_unresolved",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "assert_relationship",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {
                            "tool_param": "source_id",
                            "context_key": "kr_relationship_source_id",
                            "required": True,
                        },
                        {
                            "tool_param": "predicate",
                            "context_key": "kr_relationship_predicate",
                            "required": True,
                        },
                        {
                            "tool_param": "target",
                            "context_key": "kr_relationship_target_id",
                            "required": True,
                        },
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "add_relationship"},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("success", "kr_relationship_assert_success"),
                        ("predicate", "kr_relationship_assert_predicate"),
                        ("target", "kr_relationship_assert_target"),
                        ("added", "kr_relationship_assert_added"),
                    ),
                    "writes_context_keys": [
                        "kr_relationship_assert_success",
                        "kr_relationship_assert_predicate",
                        "kr_relationship_assert_target",
                        "kr_relationship_assert_added",
                    ],
                    "mutation_authority": {
                        "schema_version": "workflow_step_mutation_authority.v1",
                        "maximum_level": "additive_vontology",
                        "reason_code": "kr_relationship_assertion_write",
                    },
                    "next_state": "read_back_source",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "read_back_source",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {
                            "tool_param": "concept_id",
                            "context_key": "kr_relationship_source_id",
                            "required": True,
                        }
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "fetch_concept"},
                        {"key": "include_relations_any_arg", "value": True},
                        {"key": "limit", "value": 40},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("concept_id", "kr_relationship_source_readback_id"),
                        ("relationships", "kr_relationship_source_readback_relationships"),
                    ),
                    "writes_context_keys": [
                        "kr_relationship_source_readback_id",
                        "kr_relationship_source_readback_relationships",
                    ],
                    "next_state": "read_back_target",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "read_back_target",
                    "action_id": "workflow_mcp.invoke_tool",
                    "execution_mode": "deterministic",
                    "context_input_mappings": [
                        {
                            "tool_param": "concept_id",
                            "context_key": "kr_relationship_target_id",
                            "required": True,
                        }
                    ],
                    "static_input_bindings": [
                        {"key": "tool_name", "value": "fetch_concept"},
                        {"key": "include_relations_any_arg", "value": True},
                        {"key": "limit", "value": 40},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("concept_id", "kr_relationship_target_readback_id"),
                        ("relationships", "kr_relationship_target_readback_relationships"),
                    ),
                    "writes_context_keys": [
                        "kr_relationship_target_readback_id",
                        "kr_relationship_target_readback_relationships",
                    ],
                    "next_state": "emit_relationship_payload",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "emit_relationship_payload",
                    "action_id": "workflow_control.context_set",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {"key": "kr_relationship_key", "value_from_context": "kr_relationship_key"},
                                {"key": "kr_relationship_source_id", "value_from_context": "kr_relationship_source_id"},
                                {"key": "kr_relationship_predicate", "value_from_context": "kr_relationship_predicate"},
                                {"key": "kr_relationship_target_id", "value_from_context": "kr_relationship_target_id"},
                                {
                                    "key": "kr_relationship_assert_success",
                                    "value_from_context": "kr_relationship_assert_success",
                                },
                                {
                                    "key": "kr_relationship_assert_added",
                                    "value_from_context": "kr_relationship_assert_added",
                                },
                                {
                                    "key": "kr_relationship_source_readback_id",
                                    "value_from_context": "kr_relationship_source_readback_id",
                                },
                                {
                                    "key": "kr_relationship_target_readback_id",
                                    "value_from_context": "kr_relationship_target_readback_id",
                                },
                            ],
                        }
                    ],
                    "writes_context_keys": [
                        "kr_relationship_key",
                        "kr_relationship_source_id",
                        "kr_relationship_predicate",
                        "kr_relationship_target_id",
                        "kr_relationship_assert_success",
                        "kr_relationship_assert_added",
                        "kr_relationship_source_readback_id",
                        "kr_relationship_target_readback_id",
                    ],
                    "next_state": "completed",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "summarise_blocker",
                    "action_id": "workflow_control.context_template",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "response_text",
                                    "template": "KR relationship item was not asserted. Source: {source}. Predicate: {predicate}. Target: {target}. Blocker: {blocker}.",
                                    "variables": {
                                        "source": {"value_from_context": "kr_relationship_source_id", "default": "unresolved"},
                                        "predicate": {"value_from_context": "kr_relationship_predicate", "default": "unresolved"},
                                        "target": {"value_from_context": "kr_relationship_target_id", "default": "unresolved"},
                                        "blocker": {
                                            "value_from_context_options": [
                                                "kr_relationship_blocking_reason",
                                                "last_action_error",
                                            ],
                                            "default": "relationship_preconditions_not_met",
                                        },
                                    },
                                }
                            ],
                        }
                    ],
                    "writes_context_keys": ["response_text"],
                    "next_state": "failed",
                    "on_failure_state": "failed",
                }
            ),
            {"state_id": "completed"},
            {"state_id": "failed"},
        ],
    }


def main_spec() -> dict[str, Any]:
    extraction_prompt = (
        "You materialise bounded knowledge representation designs into Vontology. "
        "Return JSON only. Decide whether the user prompt contains a concrete KR/ontology design that can be materialised. "
        "When materialising, search for existing concepts and close parent types before proposing writes. "
        "Do not use #V#thing as a parent. Prefer exact reuse when search verifies an existing concept. "
        "Every concept_specs item must include: key, decision ('create' or 'reuse_existing'), target_name, target_kind "
        "('type', 'instance', or 'predicate'), parent_id, description_text, parent_rationale, and either existing_concept_id "
        "or concepts as a one-item create_concepts array with name, kind, and description. "
        "For type hierarchy use target_kind 'type'; for individuals use target_kind 'instance'; for relationship predicates use "
        "target_kind 'predicate' and parent_id #V#predicate or a closer predicate type. "
        "relationship_specs may use source_key/target_key that match concept_specs keys, or exact source_id/target_id values. "
        "Use predicate 'type_of' or 'instance_of' only for structural parent edges; for arbitrary relationships use an existing "
        "#V# predicate concept or include a predicate concept in concept_specs. "
        "Keep the batch bounded: at most 24 concept_specs and 40 relationship_specs. "
        "If required parents, predicates, or endpoint identities cannot be grounded, return decision 'block' with blocking_reason."
    )
    relationship_resolution_prompt = (
        "Resolve KR relationship endpoint references after concept materialisation. Return JSON only. "
        "Use kr_concept_iteration_results[].result to map source_key and target_key onto verified kr_concept_id values. "
        "If a relationship spec already supplies source_id or target_id, fetch/search as needed to ensure it is an existing Vontology concept. "
        "For dynamic predicates, require a #V# predicate concept that exists or was materialised in the concept fan-out; structural aliases "
        "such as type_of, instance_of, is_a_type_of, and is_an_instance_of are allowed. "
        "Return decision 'assert' with resolved_relationship_specs items containing key, source_id, predicate, target_id, and rationale. "
        "If any requested relationship cannot be resolved safely, return decision 'block' and explain blocking_reason. "
        "If there are no requested relationships, return decision 'skip' with an empty resolved_relationship_specs array."
    )
    return {
        "workflow_id": MAIN_ID,
        "description": (
            "Extract a bounded KR design from a user request, materialise its concepts "
            "and relationships into Vontology through generic write primitives, and "
            "verify durable read-back evidence before success."
        ),
        "initial_state_key": "extract_kr_materialisation_plan",
        "workflow_metadata": workflow_metadata(routing_eligible=True, main=True),
        "steps": [
            step(
                {
                    "state_id": "extract_kr_materialisation_plan",
                    "action_id": "llm.action",
                    "execution_mode": "llm",
                    "llm_policy": {
                        "prompt_text": extraction_prompt,
                        "allowed_tools": ["search_concepts", "fetch_concept"],
                        "required_tools": ["search_concepts"],
                        "tool_mode": "allowed",
                        "max_tool_invocations": 12,
                        "policy_stage": "kr_design_materialisation_plan",
                        "response_contract_text": (
                            "Return JSON only with keys: decision ('materialise' or 'block'), "
                            "concept_specs array, relationship_specs array, summary, blocking_reason."
                        ),
                        "tool_argument_defaults": {
                            "search_concepts": {
                                "include_description": True,
                                "include_hierarchy_path": True,
                                "limit": 10,
                                "match_type": "substring",
                            },
                            "fetch_concept": {
                                "include_relations_any_arg": True,
                                "include_text_relations_arg1": True,
                                "limit": 50,
                            },
                        },
                        "context_fields": [
                            {"context_key": "prompt", "label": "User KR design request"},
                            {"context_key": "user_prompt", "label": "User prompt alias"},
                            {"context_key": "user_concept_id", "label": "Authenticated user concept"},
                            {"context_key": "org_concept_id", "label": "Authenticated organisation concept"},
                            {"context_key": "turn_expected_outcome_contract_state", "label": "Expected outcome contract"},
                            {"context_key": "turn_expected_grounding_requirement", "label": "Expected grounding requirement"},
                            {"context_key": "workflow_success_guidance_history", "label": "Historical successful-run guidance"},
                            {"context_key": "workflow_failure_avoidance_history", "label": "Historical failure-avoidance guidance"},
                        ],
                    },
                    "validation_policy": {"output_format": "json_value"},
                    "tool_output_context_mappings": mappings(
                        ("validated_json.decision", "kr_materialisation_decision"),
                        ("validated_json.concept_specs", "kr_concept_specs"),
                        ("validated_json.relationship_specs", "kr_relationship_specs"),
                        ("validated_json.summary", "kr_materialisation_plan_summary"),
                        ("validated_json.blocking_reason", "kr_materialisation_blocking_reason"),
                    ),
                    "writes_context_keys": [
                        "kr_materialisation_decision",
                        "kr_concept_specs",
                        "kr_relationship_specs",
                        "kr_materialisation_plan_summary",
                        "kr_materialisation_blocking_reason",
                    ],
                    "conditional_transitions": [
                        {
                            "to_state": "materialise_concepts",
                            "reason": "materialisation_plan_ready",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    {
                                        "kind": "any",
                                        "conditions": [
                                            {
                                                "kind": "context_value_equals",
                                                "key": "kr_materialisation_decision",
                                                "value": "materialise",
                                            },
                                            {
                                                "kind": "context_value_equals",
                                                "key": "kr_materialisation_decision",
                                                "value": "create",
                                            },
                                        ],
                                    },
                                    {
                                        "kind": "context_cardinality",
                                        "key": "kr_concept_specs",
                                        "operator": ">",
                                        "value": 0,
                                    },
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "materialisation_plan_blocked",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "materialise_concepts",
                    "action_id": "workflow_control.for_each",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {"key": "workflow_id", "value": CONCEPT_ITEM_ID},
                        {"key": "items_context_key", "value": "kr_concept_specs"},
                        {"key": "item_context_key", "value": "current_kr_concept_spec"},
                        {"key": "index_context_key", "value": "kr_concept_index"},
                        {"key": "max_items", "value": 24},
                        {"key": "max_concurrency", "value": 1},
                        {"key": "success_policy", "value": "all_must_succeed"},
                        {"key": "max_transitions", "value": 40},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("for_each_success_count", "kr_concept_success_count"),
                        ("for_each_error_count", "kr_concept_error_count"),
                        ("for_each_item_count", "kr_concept_item_count"),
                        ("iteration_results", "kr_concept_iteration_results"),
                        ("invocations", "invocations"),
                    ),
                    "writes_context_keys": [
                        "kr_concept_success_count",
                        "kr_concept_error_count",
                        "kr_concept_item_count",
                        "kr_concept_iteration_results",
                        "invocations",
                    ],
                    "conditional_transitions": [
                        {
                            "to_state": "resolve_relationship_specs",
                            "reason": "concepts_verified_relationships_requested",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    {
                                        "kind": "context_compare",
                                        "key": "kr_concept_error_count",
                                        "operator": "=",
                                        "value": 0,
                                    },
                                    {
                                        "kind": "context_cardinality",
                                        "key": "kr_relationship_specs",
                                        "operator": ">",
                                        "value": 0,
                                    },
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_success",
                            "reason": "concepts_verified_no_requested_relationships",
                            "condition_spec": {
                                "kind": "context_compare",
                                "key": "kr_concept_error_count",
                                "operator": "=",
                                "value": 0,
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "concept_materialisation_failed",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "resolve_relationship_specs",
                    "action_id": "llm.action",
                    "execution_mode": "llm",
                    "llm_policy": {
                        "prompt_text": relationship_resolution_prompt,
                        "allowed_tools": ["search_concepts", "fetch_concept"],
                        "tool_mode": "allowed",
                        "max_tool_invocations": 12,
                        "policy_stage": "kr_relationship_endpoint_resolution",
                        "response_contract_text": (
                            "Return JSON only with keys: decision ('assert', 'skip', or 'block'), "
                            "resolved_relationship_specs array, blocking_reason."
                        ),
                        "tool_argument_defaults": {
                            "search_concepts": {
                                "include_description": True,
                                "include_hierarchy_path": True,
                                "limit": 10,
                                "match_type": "substring",
                            },
                            "fetch_concept": {
                                "include_relations_any_arg": True,
                                "include_text_relations_arg1": True,
                                "limit": 50,
                            },
                        },
                        "context_fields": [
                            {"context_key": "kr_relationship_specs", "label": "Requested relationship specs"},
                            {"context_key": "kr_concept_iteration_results", "label": "Verified concept materialisation results"},
                            {"context_key": "kr_materialisation_plan_summary", "label": "Materialisation plan summary"},
                            {"context_key": "prompt", "label": "Original KR design request"},
                        ],
                    },
                    "validation_policy": {"output_format": "json_value"},
                    "tool_output_context_mappings": mappings(
                        ("validated_json.decision", "kr_relationship_resolution_decision"),
                        ("validated_json.resolved_relationship_specs", "kr_resolved_relationship_specs"),
                        ("validated_json.blocking_reason", "kr_relationship_resolution_blocking_reason"),
                    ),
                    "writes_context_keys": [
                        "kr_relationship_resolution_decision",
                        "kr_resolved_relationship_specs",
                        "kr_relationship_resolution_blocking_reason",
                    ],
                    "conditional_transitions": [
                        {
                            "to_state": "assert_relationships",
                            "reason": "relationships_resolved",
                            "condition_spec": {
                                "kind": "all",
                                "conditions": [
                                    {
                                        "kind": "context_value_equals",
                                        "key": "kr_relationship_resolution_decision",
                                        "value": "assert",
                                    },
                                    {
                                        "kind": "context_cardinality",
                                        "key": "kr_resolved_relationship_specs",
                                        "operator": ">",
                                        "value": 0,
                                    },
                                ],
                            },
                        },
                        {
                            "to_state": "summarise_success",
                            "reason": "relationship_resolution_skipped",
                            "condition_spec": {
                                "kind": "context_value_equals",
                                "key": "kr_relationship_resolution_decision",
                                "value": "skip",
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "relationship_resolution_blocked",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "assert_relationships",
                    "action_id": "workflow_control.for_each",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {"key": "workflow_id", "value": RELATION_ITEM_ID},
                        {"key": "items_context_key", "value": "kr_resolved_relationship_specs"},
                        {"key": "item_context_key", "value": "current_kr_relationship_spec"},
                        {"key": "index_context_key", "value": "kr_relationship_index"},
                        {"key": "max_items", "value": 40},
                        {"key": "max_concurrency", "value": 1},
                        {"key": "success_policy", "value": "all_must_succeed"},
                        {"key": "max_transitions", "value": 24},
                    ],
                    "tool_output_context_mappings": mappings(
                        ("for_each_success_count", "kr_relationship_success_count"),
                        ("for_each_error_count", "kr_relationship_error_count"),
                        ("for_each_item_count", "kr_relationship_item_count"),
                        ("iteration_results", "kr_relationship_iteration_results"),
                        ("invocations", "invocations"),
                    ),
                    "writes_context_keys": [
                        "kr_relationship_success_count",
                        "kr_relationship_error_count",
                        "kr_relationship_item_count",
                        "kr_relationship_iteration_results",
                        "invocations",
                    ],
                    "conditional_transitions": [
                        {
                            "to_state": "summarise_success",
                            "reason": "relationships_verified",
                            "condition_spec": {
                                "kind": "context_compare",
                                "key": "kr_relationship_error_count",
                                "operator": "=",
                                "value": 0,
                            },
                        },
                        {
                            "to_state": "summarise_blocker",
                            "reason": "relationship_assertion_failed",
                            "condition_spec": {"kind": "always"},
                        },
                    ],
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "summarise_success",
                    "action_id": "workflow_control.context_template",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "response_text",
                                    "template": (
                                        "KR design materialisation verified. Concepts: {concept_count}; "
                                        "relationships: {relationship_count}. Evidence tools included create_concepts, "
                                        "add_relationship, upsert_singleton_text_relation, fetch_concept, and "
                                        "get_text_relations_summary."
                                    ),
                                    "variables": {
                                        "concept_count": {
                                            "value_from_context": "kr_concept_success_count",
                                            "default": 0,
                                        },
                                        "relationship_count": {
                                            "value_from_context": "kr_relationship_success_count",
                                            "default": 0,
                                        },
                                    },
                                }
                            ],
                        }
                    ],
                    "writes_context_keys": ["response_text"],
                    "next_state": "completed",
                    "on_failure_state": "summarise_blocker",
                }
            ),
            step(
                {
                    "state_id": "summarise_blocker",
                    "action_id": "workflow_control.context_template",
                    "execution_mode": "deterministic",
                    "static_input_bindings": [
                        {
                            "key": "assignments",
                            "value": [
                                {
                                    "key": "response_text",
                                    "template": "KR design materialisation did not complete. Blocker: {blocker}.",
                                    "variables": {
                                        "blocker": {
                                            "value_from_context_options": [
                                                "kr_materialisation_blocking_reason",
                                                "kr_relationship_resolution_blocking_reason",
                                                "last_action_error",
                                            ],
                                            "default": "materialisation_preconditions_not_met",
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                    "writes_context_keys": ["response_text"],
                    "next_state": "failed",
                    "on_failure_state": "failed",
                }
            ),
            {"state_id": "completed"},
            {"state_id": "failed"},
        ],
    }


def specs_by_id() -> dict[str, dict[str, Any]]:
    specs = [concept_item_spec(), relation_item_spec(), main_spec()]
    return {spec["workflow_id"]: spec for spec in specs}


def _assert_live_db() -> None:
    if os.environ.get("VON_DB_NAME") != "von_db":
        raise SystemExit(
            "Refusing to write workflows unless VON_DB_NAME is explicitly set to von_db."
        )


def _validation_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    candidate = result.get("candidate_validation") if isinstance(result, Mapping) else {}
    candidate_map = candidate if isinstance(candidate, Mapping) else {}
    generation = candidate_map.get("generation_safe_validation")
    generation_map = generation if isinstance(generation, Mapping) else {}
    contract = candidate_map.get("contract_validation")
    contract_map = contract if isinstance(contract, Mapping) else {}
    return {
        "workflow_id": candidate_map.get("workflow_id") or result.get("workflow_id"),
        "valid": bool(candidate_map.get("valid")),
        "contract_valid": bool(contract_map.get("valid")),
        "contract_errors": contract_map.get("errors") or [],
        "generation_valid": bool(generation_map.get("valid")),
        "generation_errors": generation_map.get("errors") or [],
        "generation_warnings": generation_map.get("warnings") or [],
        "repair_hints": candidate_map.get("repair_hints") or [],
    }


def validate_workflow(workflow_id: str) -> dict[str, Any]:
    from src.backend.workflows.workflow_studio_service import validate_workflow_candidate

    spec = specs_by_id()[workflow_id]
    result = validate_workflow_candidate(
        workflow_id,
        authoring_spec=spec,
        validation_profile="generation_safe",
        include_preview=False,
    )
    summary = _validation_summary(result)
    print(json.dumps({"validation": summary}, ensure_ascii=True, sort_keys=True))
    if not summary["valid"]:
        raise SystemExit(f"candidate validation failed for {workflow_id}")
    return result


def _persist_policy_metadata(workflow_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    from src.backend.workflows.required_effects_contracts import (
        normalise_workflow_required_effects_contract,
    )
    from src.backend.workflows.terminal_success_contracts import (
        normalise_workflow_terminal_success_contract,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        upsert_workflow_json_policy_text,
        upsert_workflow_publication_lifecycle,
    )
    from src.backend.workflows.workflow_studio_service import _apply_workflow_policy_metadata

    metadata_map = dict(metadata)
    applied = _apply_workflow_policy_metadata(
        workflow_id=workflow_id,
        workflow_metadata=metadata_map,
    )
    terminal_contract = normalise_workflow_terminal_success_contract(
        metadata_map.get("terminal_success_contract")
    )
    if terminal_contract:
        applied["terminal_success_contract"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate="#V#hasWorkflowTerminalSuccessContractJson",
            payload=terminal_contract,
            context={"source": "JVNAUTOSCI-2268"},
        )
    required_effects_contract = normalise_workflow_required_effects_contract(
        metadata_map.get("required_effects_contract")
    )
    if required_effects_contract:
        applied["required_effects_contract"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate="#V#hasWorkflowRequiredEffectsContractJson",
            payload=required_effects_contract,
            context={"source": "JVNAUTOSCI-2268"},
        )
    routing_profile = metadata_map.get("routing_profile")
    routing_eligible = None
    if isinstance(routing_profile, Mapping) and isinstance(
        routing_profile.get("routing_eligible"), bool
    ):
        routing_eligible = bool(routing_profile.get("routing_eligible"))
    applied["publication_lifecycle"] = upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id,
        phase="published",
        published=True,
        validation_passed=True,
        postconditions_verified=True,
        last_error=None,
        review_state="approved",
        review_reason="JVNAUTOSCI-2268 initial KR materialisation workflow publication",
        reviewed_by="GitHub Copilot",
        routing_eligible=routing_eligible,
        rollout_state="published",
        approval_required=False,
    )
    return applied


def publish_workflow(workflow_id: str) -> dict[str, Any]:
    _assert_live_db()
    from src.backend.workflows.workflow_authoring_service import (
        build_workflow_definition_from_authoring_spec,
    )
    from src.backend.workflows.workflow_studio_service import apply_workflow_authoring_spec

    validate_workflow(workflow_id)
    spec = specs_by_id()[workflow_id]
    result = apply_workflow_authoring_spec(workflow_id, authoring_spec=spec)
    publication = result.get("publication") if isinstance(result, Mapping) else {}
    publication_map = publication if isinstance(publication, Mapping) else {}
    counts = publication_map.get("counts") if isinstance(publication_map, Mapping) else {}
    if isinstance(counts, Mapping) and counts.get("errors"):
        raise SystemExit(f"publication errors for {workflow_id}: {publication_map}")
    published = publication_map.get("published_workflow_ids") or []
    if workflow_id not in published:
        raise SystemExit(f"workflow not published: {workflow_id}: {publication_map}")
    definition = build_workflow_definition_from_authoring_spec(spec)
    metadata = getattr(definition, "metadata", {})
    applied_metadata = _persist_policy_metadata(workflow_id, metadata if isinstance(metadata, Mapping) else {})
    print(
        json.dumps(
            {
                "published": {
                    "workflow_id": workflow_id,
                    "counts": counts,
                    "metadata_keys": sorted(applied_metadata.keys()),
                }
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return result


def verify_workflows() -> dict[str, Any]:
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
        resolve_workflow_discovery_exemplars,
        resolve_workflow_launch_input_contract,
        resolve_workflow_long_horizon_policies,
        resolve_workflow_publication_lifecycle,
        resolve_workflow_routing_profile,
    )

    payload: dict[str, Any] = {}
    for workflow_id, spec in specs_by_id().items():
        definition = load_workflow_definition_from_vontology(workflow_id)
        lifecycle, _ = resolve_workflow_publication_lifecycle(workflow_id)
        routing, _ = resolve_workflow_routing_profile(workflow_id)
        discovery, _ = resolve_workflow_discovery_exemplars(workflow_id)
        launch, _ = resolve_workflow_launch_input_contract(workflow_id)
        long_horizon, policy_warnings = resolve_workflow_long_horizon_policies(workflow_id)
        terminal = long_horizon.get("terminal_success_contract")
        required = long_horizon.get("required_effects_contract")
        states = getattr(definition, "states", {}) if definition is not None else {}
        payload[workflow_id] = {
            "loaded": definition is not None,
            "state_count": len(states) if isinstance(states, Mapping) else 0,
            "initial_state": getattr(definition, "initial_state", None) if definition is not None else None,
            "published": bool((lifecycle or {}).get("published")) if isinstance(lifecycle, Mapping) else False,
            "phase": (lifecycle or {}).get("phase") if isinstance(lifecycle, Mapping) else None,
            "routing_eligible": (routing or {}).get("routing_eligible") if isinstance(routing, Mapping) else None,
            "lifecycle_routing_eligible": (lifecycle or {}).get("routing_eligible") if isinstance(lifecycle, Mapping) else None,
            "discovery_present": bool(discovery),
            "launch_contract_present": bool(launch),
            "terminal_contract_present": bool(terminal),
            "required_effects_contract_present": bool(required),
            "policy_warnings": policy_warnings,
            "expected_state_count": len(spec.get("steps") or []),
        }
    print(json.dumps({"verification": payload}, ensure_ascii=True, sort_keys=True))
    if not all(item["loaded"] and item["published"] for item in payload.values()):
        raise SystemExit("live workflow verification failed")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "validate-children",
            "publish-children",
            "validate-main",
            "publish-main",
            "publish-all",
            "verify",
        ],
    )
    args = parser.parse_args(argv)

    if args.command == "validate-children":
        validate_workflow(CONCEPT_ITEM_ID)
        validate_workflow(RELATION_ITEM_ID)
    elif args.command == "publish-children":
        publish_workflow(CONCEPT_ITEM_ID)
        publish_workflow(RELATION_ITEM_ID)
    elif args.command == "validate-main":
        validate_workflow(MAIN_ID)
    elif args.command == "publish-main":
        publish_workflow(MAIN_ID)
    elif args.command == "publish-all":
        validate_workflow(CONCEPT_ITEM_ID)
        validate_workflow(RELATION_ITEM_ID)
        publish_workflow(CONCEPT_ITEM_ID)
        publish_workflow(RELATION_ITEM_ID)
        validate_workflow(MAIN_ID)
        publish_workflow(MAIN_ID)
        verify_workflows()
    elif args.command == "verify":
        verify_workflows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
