"""Tests for vontology_loader.py — Phase 3.2 correctness.

JVNAUTOSCI-922 Phase 3.2: Verifies that load_workflow_definition_from_vontology()
correctly converts Vontology process graphs into executable WorkflowDefinitions,
including:
- initial_step key resolution
- Lambda closure correctness (no late-binding bugs)
- on_failure transition support
- on_unknown transition support
- Input mapping via hasInputMap
- Semantic context mapping via workflow_step_maps_context_key_to_tool_param
- Output context contracts via workflow_step_writes_context_key
- Preconditions/effects carried as state metadata
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch, MagicMock

import pytest

from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.vontology_loader import (
    _fetch_concepts_by_id,
    build_workflow_process_graph,
    detect_vacuous_workflow_steps,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
    _normalise_relationship_targets,
    _normalise_invoked_action_target,
    _first_relationship_target,
    _all_relationship_targets,
    resolve_workflow_background_launch_policy,
    resolve_workflow_description,
    resolve_workflow_narrative_text,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
)


# ---------------------------------------------------------------------------
# Helpers — build mock graph structures without touching MongoDB.
# ---------------------------------------------------------------------------


def _make_graph(
    *,
    workflow_id: str = "#V#test_workflow",
    initial_step: str = "#V#step_a",
    steps: List[Dict[str, Any]] | None = None,
    edges: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a raw process graph dict matching build_workflow_process_graph output."""
    return {
        "representation": "vontology_process_graph_v1",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "steps": steps or [],
        "edges": edges or [],
        "warnings": [],
    }


def _make_step(
    step_id: str,
    *,
    invokes_action: str | None = None,
    invokes_workflow: str | None = None,
    next_step: str | None = None,
    on_true: str | None = None,
    on_false: str | None = None,
    on_failure: str | None = None,
    on_unknown: str | None = None,
    on_break: str | None = None,
    on_continue: str | None = None,
    preconditions: List[str] | None = None,
    effects: List[str] | None = None,
    reads_variables: List[str] | None = None,
    writes_variables: List[str] | None = None,
    context_input_mappings: List[str] | None = None,
    tool_output_context_mappings: List[str] | None = None,
    writes_context_keys: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "step_id": step_id,
        "name": step_id,
        "invokes_action": invokes_action,
        "invokes_workflow": invokes_workflow,
        "preconditions": preconditions or [],
        "effects": effects or [],
        "reads_variables": reads_variables or [],
        "writes_variables": writes_variables or [],
        "context_input_mappings": context_input_mappings or [],
        "tool_output_context_mappings": tool_output_context_mappings or [],
        "writes_context_keys": writes_context_keys or [],
        "control_flow": {
            "next": next_step,
            "on_true": on_true,
            "on_false": on_false,
            "on_failure": on_failure,
            "on_unknown": on_unknown,
            "on_break": on_break,
            "on_continue": on_continue,
        },
    }


def _stub_fetch_concepts(step_docs: Dict[str, Dict[str, Any]] | None = None):
    """Patch _fetch_concepts_by_id to return pre-built docs."""
    docs = step_docs or {}
    return patch(
        "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
        return_value=docs,
    )


def _stub_narrative(text: str | None = None):
    return patch(
        "src.backend.workflows.vontology_loader.best_effort_workflow_narrative_text",
        return_value=text,
    )


# ---------------------------------------------------------------------------
# Unit tests — helper functions.
# ---------------------------------------------------------------------------


class TestNormaliseRelationshipTargets:
    def test_string_value(self):
        assert _normalise_relationship_targets("foo") == ["foo"]

    def test_empty_string(self):
        assert _normalise_relationship_targets("") == []

    def test_list_value(self):
        assert _normalise_relationship_targets(["a", "b"]) == ["a", "b"]

    def test_mixed_list(self):
        assert _normalise_relationship_targets(["a", 42, "", "b"]) == ["a", "b"]

    def test_none(self):
        assert _normalise_relationship_targets(None) == []

    def test_int(self):
        assert _normalise_relationship_targets(123) == []


class TestFirstRelationshipTarget:
    def test_finds_first_match(self):
        rels = {"hasStep": "#V#step_a", "nextStep": "#V#step_b"}
        assert _first_relationship_target(rels, ("hasStep",)) == "#V#step_a"

    def test_fallback_to_second_predicate(self):
        rels = {"has_step": "#V#step_a"}
        assert _first_relationship_target(rels, ("hasStep", "has_step")) == "#V#step_a"

    def test_none_when_missing(self):
        assert _first_relationship_target({}, ("hasStep",)) is None


class TestAllRelationshipTargets:
    def test_deduplicates(self):
        rels = {"hasStep": ["#V#a", "#V#b"], "has_step": ["#V#b", "#V#c"]}
        result = _all_relationship_targets(rels, ("hasStep", "has_step"))
        assert result == ["#V#a", "#V#b", "#V#c"]


class TestNormaliseInvokedActionTarget:
    def test_passthrough_plain_action_id(self):
        assert _normalise_invoked_action_target("fetch_concept") == "fetch_concept"

    def test_concept_id_tool_suffix_becomes_action_id(self):
        assert (
            _normalise_invoked_action_target("#V#fetch_concept_tool")
            == "fetch_concept"
        )


class TestFetchConceptProjection:
    def test_fetch_concepts_projection_avoids_parent_child_path_collision(self):
        captured: Dict[str, Any] = {}

        def _fake_find(_query, projection, limit=None):
            captured["projection"] = dict(projection or {})
            return iter(
                [
                    {
                        "concept_id": "#V#example",
                        "name": "Example",
                        "relationships": {},
                    }
                ]
            )

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find",
            side_effect=_fake_find,
        ):
            docs = _fetch_concepts_by_id(["#V#example"])

        assert "#V#example" in docs
        projection = captured["projection"]
        assert "concept_data" in projection
        assert "concept_data.preserved_fields.description" not in projection


class TestWorkflowDescriptionResolution:
    def test_resolve_narrative_prefers_has_definition_precedence(self):
        with patch(
            "src.backend.workflows.vontology_loader.get_texts_for_concept",
            return_value=[
                {"predicate": "hasContent", "text": "Content text"},
                {"predicate": "hasDefinition", "text": "Definition text"},
                {"predicate": "hasDescription", "text": "Description text"},
            ],
        ):
            with patch(
                "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
                side_effect=AssertionError(
                    "legacy fallback should not run when canonical text exists"
                ),
            ):
                text, source = resolve_workflow_narrative_text("#V#demo_workflow")

        assert text == "Definition text"
        assert source == "text_relation:hasDefinition"

    def test_resolve_narrative_supports_v_prefixed_has_description(self):
        with patch(
            "src.backend.workflows.vontology_loader.get_texts_for_concept",
            return_value=[
                {"predicate": "#V#hasDescription", "text": "Canonical V description"}
            ],
        ):
            text, source = resolve_workflow_narrative_text("#V#demo_workflow")

        assert text == "Canonical V description"
        assert source == "text_relation:#V#hasDescription"

    def test_resolve_narrative_returns_none_when_canonical_text_missing(self):
        with patch(
            "src.backend.workflows.vontology_loader.get_texts_for_concept",
            return_value=[],
        ):
            text, source = resolve_workflow_narrative_text("#V#legacy_workflow")

        assert text is None
        assert source == "none"

    def test_resolve_workflow_description_prefers_canonical_vontology_source(self):
        with patch(
            "src.backend.workflows.vontology_loader.resolve_workflow_narrative_text",
            return_value=("Narrative from relation", "text_relation:hasContent"),
        ):
            description, source = resolve_workflow_description(
                "#V#workflow",
                workflow_source="vontology",
                registration_purpose="Cached purpose",
                definition_purpose="Definition purpose",
            )

        assert description == "Narrative from relation"
        assert source == "text_relation:hasContent"


class TestWorkflowBackgroundLaunchPolicyResolution:
    def test_prefers_canonical_json_policy_relation(self):
        with patch(
            "src.backend.workflows.vontology_loader.get_texts_for_concept",
            return_value=[
                {
                    "predicate": "#V#hasBackgroundLaunchPolicyJson",
                    "text": (
                        '{"enabled": true, "min_interval_seconds": 300, '
                        '"applies_to_sources": ["event"]}'
                    ),
                }
            ],
        ):
            policy, source = resolve_workflow_background_launch_policy(
                "#V#enrichment_workflow"
            )

        assert policy is not None
        assert policy["enabled"] is True
        assert policy["min_interval_seconds"] == 300
        assert policy["scope"] == "global_per_server"
        assert policy["applies_to_sources"] == ["event"]
        assert source == "text_relation:#V#hasBackgroundLaunchPolicyJson"

    def test_supports_plain_numeric_interval_relation(self):
        with patch(
            "src.backend.workflows.vontology_loader.get_texts_for_concept",
            return_value=[
                {
                    "predicate": "#V#hasMinimumBackgroundLaunchIntervalSeconds",
                    "text": "300",
                }
            ],
        ):
            policy, source = resolve_workflow_background_launch_policy(
                "#V#enrichment_workflow"
            )

        assert policy is not None
        assert policy["enabled"] is True
        assert policy["min_interval_seconds"] == 300
        assert policy["applies_to_sources"] == ["event"]
        assert source == "text_relation:#V#hasMinimumBackgroundLaunchIntervalSeconds"

    def test_returns_none_when_policy_payload_is_invalid(self):
        with patch(
            "src.backend.workflows.vontology_loader.get_texts_for_concept",
            return_value=[
                {
                    "predicate": "#V#hasBackgroundLaunchPolicyJson",
                    "text": '{"enabled": true, "min_interval_seconds": "not-a-number"}',
                }
            ],
        ):
            policy, source = resolve_workflow_background_launch_policy(
                "#V#enrichment_workflow"
            )

        assert policy is None
        assert source == "text_relation_invalid:#V#hasBackgroundLaunchPolicyJson"


class TestWorkflowGraphPredicateCompatibility:
    def test_build_graph_accepts_legacy_aliases_and_emits_warning(self):
        workflow_doc = {
            "concept_id": "#V#legacy_workflow",
            "relationships": {
                "has_initial_step": "#V#step_a",
                "has_step": ["#V#step_a"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Step A",
                "relationships": {
                    "invokes_action": "tool.call",
                    "next_step": "#V#step_b",
                },
            }
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                graph, warnings = build_workflow_process_graph("#V#legacy_workflow")

        assert graph is not None
        assert graph["initial_step"] == "#V#step_a"
        assert graph["steps"][0]["invokes_action"] == "tool.call"
        assert any(
            str(item).startswith("legacy_workflow_predicates_used:")
            for item in warnings
        )

    def test_build_graph_reads_semantic_mapping_and_output_contract_predicates(self):
        output_mapping_id = (
            "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id"
        )
        workflow_doc = {
            "concept_id": "#V#workflow_semantic_contract",
            "relationships": {
                "hasInitialStep": "#V#step_a",
                "hasStep": ["#V#step_a"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Step A",
                "relationships": {
                    "invokesAction": "fetch_concept",
                    "workflow_step_maps_context_key_to_tool_param": [
                        "#V#workflow_mapping_target_type_id_to_concept_id_param"
                    ],
                    "workflow_step_maps_tool_output_field_to_context_key": [
                        output_mapping_id
                    ],
                    "workflow_step_writes_context_key": [
                        "#V#workflow_context_key_validated_type_id"
                    ],
                },
            }
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                graph, _warnings = build_workflow_process_graph(
                    "#V#workflow_semantic_contract"
                )

        assert graph is not None
        step = graph["steps"][0]
        assert step["context_input_mappings"] == [
            "#V#workflow_mapping_target_type_id_to_concept_id_param"
        ]
        assert step["tool_output_context_mappings"] == [output_mapping_id]
        assert step["writes_context_keys"] == [
            "#V#workflow_context_key_validated_type_id"
        ]

    def test_build_graph_reads_workflow_step_invokes_tool_predicate(self):
        workflow_doc = {
            "concept_id": "#V#workflow_invokes_tool_predicate",
            "relationships": {
                "hasInitialStep": "#V#step_a",
                "hasStep": ["#V#step_a"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Step A",
                "relationships": {
                    "#V#workflow_step_invokes_tool": ["#V#fetch_concept_tool"],
                },
            }
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                graph, warnings = build_workflow_process_graph(
                    "#V#workflow_invokes_tool_predicate"
                )

        assert graph is not None
        step = graph["steps"][0]
        assert step["invokes_action"] == "fetch_concept"
        assert any(
            str(item).startswith("legacy_workflow_predicates_used:")
            for item in warnings
        )

    def test_build_graph_reads_invokes_workflow_predicate(self):
        workflow_doc = {
            "concept_id": "#V#workflow_invokes_workflow_predicate",
            "relationships": {
                "hasInitialStep": "#V#step_a",
                "hasStep": ["#V#step_a"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Step A",
                "relationships": {
                    "invokesWorkflow": "#V#child_workflow",
                },
            }
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                graph, warnings = build_workflow_process_graph(
                    "#V#workflow_invokes_workflow_predicate"
                )

        assert graph is not None
        step = graph["steps"][0]
        assert step["invokes_workflow"] == "#V#child_workflow"
        assert step["invokes_action"] is None
        assert "workflow_step_multiple_invocation_targets" not in ",".join(warnings)

    def test_build_graph_warns_when_action_and_subworkflow_are_both_set(self):
        workflow_doc = {
            "concept_id": "#V#workflow_dual_invocation",
            "relationships": {
                "hasInitialStep": "#V#step_a",
                "hasStep": ["#V#step_a"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Step A",
                "relationships": {
                    "invokesAction": "tool.call",
                    "invokesWorkflow": "#V#child_workflow",
                },
            }
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                _graph, warnings = build_workflow_process_graph(
                    "#V#workflow_dual_invocation"
                )

        assert any(
            str(item).startswith("workflow_step_multiple_invocation_targets:")
            for item in warnings
        )

    def test_build_graph_reads_on_unknown_next_step_predicate(self):
        workflow_doc = {
            "concept_id": "#V#workflow_on_unknown_predicate",
            "relationships": {
                "hasInitialStep": "#V#step_a",
                "hasStep": ["#V#step_a", "#V#escalate_step", "#V#done_step"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Step A",
                "relationships": {
                    "invokesAction": "probe_tool",
                    "onUnknownNextStep": "#V#escalate_step",
                    "nextStep": "#V#done_step",
                },
            },
            "#V#escalate_step": {
                "concept_id": "#V#escalate_step",
                "name": "Escalate Step",
                "relationships": {},
            },
            "#V#done_step": {
                "concept_id": "#V#done_step",
                "name": "Done Step",
                "relationships": {},
            },
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                graph, _warnings = build_workflow_process_graph(
                    "#V#workflow_on_unknown_predicate"
                )

        assert graph is not None
        step = graph["steps"][0]
        assert step["control_flow"]["on_unknown"] == "#V#escalate_step"
        assert any(
            edge["predicate"] == "onUnknownNextStep"
            and edge["to"] == "#V#escalate_step"
            for edge in graph["edges"]
        )

    def test_build_graph_reads_on_break_and_on_continue_next_step_predicates(self):
        workflow_doc = {
            "concept_id": "#V#workflow_loop_controls",
            "relationships": {
                "hasInitialStep": "#V#step_a",
                "hasStep": ["#V#step_a", "#V#loop_head", "#V#loop_exit"],
            },
        }
        step_docs = {
            "#V#step_a": {
                "concept_id": "#V#step_a",
                "name": "Loop Step",
                "relationships": {
                    "invokesAction": "loop_tool",
                    "onBreakNextStep": "#V#loop_exit",
                    "onContinueNextStep": "#V#loop_head",
                },
            },
            "#V#loop_head": {
                "concept_id": "#V#loop_head",
                "name": "Loop Head",
                "relationships": {},
            },
            "#V#loop_exit": {
                "concept_id": "#V#loop_exit",
                "name": "Loop Exit",
                "relationships": {},
            },
        }

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            return_value=workflow_doc,
        ):
            with patch(
                "src.backend.workflows.vontology_loader._fetch_concepts_by_id",
                return_value=step_docs,
            ):
                graph, _warnings = build_workflow_process_graph(
                    "#V#workflow_loop_controls"
                )

        assert graph is not None
        step = graph["steps"][0]
        assert step["control_flow"]["on_break"] == "#V#loop_exit"
        assert step["control_flow"]["on_continue"] == "#V#loop_head"


# ---------------------------------------------------------------------------
# Key fix: initial_step key (not initial_state).
# ---------------------------------------------------------------------------


class TestInitialStepKey:
    def test_reads_initial_step_not_initial_state(self):
        """The graph uses 'initial_step' — load must read that key, not 'initial_state'."""
        graph = _make_graph(
            initial_step="#V#start",
            steps=[_make_step("#V#start", invokes_action="test.action")],
        )

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.initial_state == "#V#start"

    def test_fallback_to_first_step_when_initial_missing(self):
        """If initial_step is None, use the first step."""
        graph = _make_graph(steps=[_make_step("#V#fallback")])
        graph["initial_step"] = None

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.initial_state == "#V#fallback"

    def test_load_definition_carries_background_launch_policy_metadata(self):
        graph = _make_graph(
            initial_step="#V#start",
            steps=[_make_step("#V#start", invokes_action="test.action")],
        )

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                with patch(
                    "src.backend.workflows.vontology_loader.resolve_workflow_background_launch_policy",
                    return_value=(
                        {
                            "schema_version": "workflow_background_launch_policy.v1",
                            "enabled": True,
                            "min_interval_seconds": 300,
                            "scope": "global_per_server",
                            "applies_to_sources": ["event"],
                        },
                        "text_relation:#V#hasBackgroundLaunchPolicyJson",
                    ),
                ):
                    defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        metadata = dict(defn.metadata)
        assert metadata["background_launch_policy"]["min_interval_seconds"] == 300
        assert (
            metadata["background_launch_policy_source"]
            == "text_relation:#V#hasBackgroundLaunchPolicyJson"
        )


# ---------------------------------------------------------------------------
# Lambda closure correctness.
# ---------------------------------------------------------------------------


class TestLambdaClosureCapture:
    def test_transitions_capture_correct_targets(self):
        """Each step's transitions must target that step's control-flow, not the last step's."""
        steps = [
            _make_step("#V#step_a", invokes_action="act_a", next_step="#V#step_b"),
            _make_step("#V#step_b", invokes_action="act_b", next_step="#V#step_c"),
            _make_step("#V#step_c", invokes_action="act_c"),  # terminal
        ]
        graph = _make_graph(initial_step="#V#step_a", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        # step_a should transition to step_b, NOT step_c (late binding bug).
        assert defn.states["#V#step_a"].transitions[0].to_state == "#V#step_b"
        assert defn.states["#V#step_b"].transitions[0].to_state == "#V#step_c"
        # step_c is terminal (no transitions).
        assert defn.states["#V#step_c"].terminal is True
        assert len(defn.states["#V#step_c"].transitions) == 0

    def test_on_true_on_false_conditions_independent(self):
        """on_true and on_false for different steps must evaluate independently."""
        steps = [
            _make_step(
                "#V#branch_a",
                invokes_action="check",
                on_true="#V#yes_a",
                on_false="#V#no_a",
            ),
            _make_step(
                "#V#branch_b",
                invokes_action="check",
                on_true="#V#yes_b",
                on_false="#V#no_b",
            ),
            _make_step("#V#yes_a"),
            _make_step("#V#no_a"),
            _make_step("#V#yes_b"),
            _make_step("#V#no_b"),
        ]
        graph = _make_graph(initial_step="#V#branch_a", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        # branch_a on_true goes to yes_a.
        a_transitions = defn.states["#V#branch_a"].transitions
        assert a_transitions[0].to_state == "#V#yes_a"
        assert a_transitions[0].reason == "on_true"
        assert a_transitions[0].condition_spec == {
            "kind": "transition_result_truth",
            "expected": True,
        }
        assert a_transitions[1].to_state == "#V#no_a"
        assert a_transitions[1].reason == "on_false"
        assert a_transitions[1].condition_spec == {
            "kind": "transition_result_truth",
            "expected": False,
        }

        # branch_b on_true goes to yes_b (NOT yes_a — that would be the closure bug).
        b_transitions = defn.states["#V#branch_b"].transitions
        assert b_transitions[0].to_state == "#V#yes_b"
        assert b_transitions[0].reason == "on_true"
        assert b_transitions[1].to_state == "#V#no_b"
        assert b_transitions[1].reason == "on_false"


# ---------------------------------------------------------------------------
# on_failure transition support.
# ---------------------------------------------------------------------------


class TestOnFailureTransitions:
    def test_on_failure_becomes_transition(self):
        """on_failure should produce a transition with last_action_failed condition."""
        steps = [
            _make_step(
                "#V#risky",
                invokes_action="risky.action",
                on_failure="#V#recovery",
                next_step="#V#ok",
            ),
            _make_step("#V#recovery"),
            _make_step("#V#ok"),
        ]
        graph = _make_graph(initial_step="#V#risky", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        risky = defn.states["#V#risky"]
        failure_transition = next(
            (t for t in risky.transitions if t.reason == "on_failure"), None
        )
        assert failure_transition is not None
        assert failure_transition.to_state == "#V#recovery"
        assert failure_transition.condition_spec == {
            "kind": "context_flag",
            "key": "last_action_failed",
            "expected": True,
        }

        # The condition should fire when last_action_failed is True.
        assert failure_transition.condition({"last_action_failed": True}) is True
        assert failure_transition.condition({"last_action_failed": False}) is False
        assert failure_transition.condition({}) is False

    def test_on_failure_has_priority_over_next(self):
        """on_failure transitions should appear before next transitions."""
        steps = [
            _make_step(
                "#V#step",
                invokes_action="act",
                on_failure="#V#error",
                next_step="#V#ok",
            ),
            _make_step("#V#error"),
            _make_step("#V#ok"),
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        transitions = defn.states["#V#step"].transitions
        assert transitions[0].reason == "on_failure"
        assert transitions[1].reason == "next_step"


# ---------------------------------------------------------------------------
# on_unknown transition support.
# ---------------------------------------------------------------------------


class TestOnUnknownTransitions:
    def test_on_unknown_becomes_transition(self):
        """on_unknown should produce a transition keyed by last_action_unknown."""
        steps = [
            _make_step(
                "#V#probe",
                invokes_action="probe.action",
                on_unknown="#V#escalate",
                next_step="#V#ok",
            ),
            _make_step("#V#escalate"),
            _make_step("#V#ok"),
        ]
        graph = _make_graph(initial_step="#V#probe", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        probe = defn.states["#V#probe"]
        unknown_transition = next(
            (t for t in probe.transitions if t.reason == "on_unknown"), None
        )
        assert unknown_transition is not None
        assert unknown_transition.to_state == "#V#escalate"
        assert unknown_transition.condition_spec == {
            "kind": "context_flag",
            "key": "last_action_unknown",
            "expected": True,
        }
        assert unknown_transition.condition({"last_action_unknown": True}) is True
        assert unknown_transition.condition({"last_action_unknown": False}) is False
        assert unknown_transition.condition({}) is False

    def test_on_unknown_has_priority_over_next(self):
        """on_unknown transitions should be ordered before next transitions."""
        steps = [
            _make_step(
                "#V#step",
                invokes_action="act",
                on_unknown="#V#retry",
                next_step="#V#ok",
            ),
            _make_step("#V#retry"),
            _make_step("#V#ok"),
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        transitions = defn.states["#V#step"].transitions
        assert transitions[0].reason == "on_unknown"
        assert transitions[1].reason == "next_step"


class TestOnBreakContinueTransitions:
    def test_on_break_and_on_continue_become_control_signal_transitions(self):
        steps = [
            _make_step(
                "#V#loop_step",
                invokes_action="loop.action",
                on_break="#V#loop_exit",
                on_continue="#V#loop_head",
                next_step="#V#fallback",
            ),
            _make_step("#V#loop_head"),
            _make_step("#V#loop_exit"),
            _make_step("#V#fallback"),
        ]
        graph = _make_graph(initial_step="#V#loop_step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        loop_step = defn.states["#V#loop_step"]
        reasons = [transition.reason for transition in loop_step.transitions]
        assert reasons == ["on_break", "on_continue", "next_step"]

        on_break = loop_step.transitions[0]
        on_continue = loop_step.transitions[1]
        assert on_break.condition_spec == {"kind": "control_signal", "signal": "break"}
        assert on_continue.condition_spec == {
            "kind": "control_signal",
            "signal": "continue",
        }
        assert (
            on_break.condition(
                {"last_control_signal": "break", "last_control_signal_scope": "main"}
            )
            is True
        )
        assert on_continue.condition({"last_control_signal": "continue"}) is True


class TestDeclarativeConditionBranches:
    def test_explicit_condition_branch_compiles_to_declarative_spec(self):
        steps = [
            _make_step("#V#branch", invokes_action="route.action", next_step="#V#fallback"),
            _make_step("#V#preferred"),
            _make_step("#V#fallback"),
        ]
        steps[0]["control_flow"]["conditions"] = [
            {
                "to": "#V#preferred",
                "reason": "context_route_preferred",
                "condition": {
                    "kind": "context_value_equals",
                    "key": "route",
                    "value": "preferred",
                },
            }
        ]
        graph = _make_graph(initial_step="#V#branch", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        branch_transitions = defn.states["#V#branch"].transitions
        assert branch_transitions[0].reason == "context_route_preferred"
        assert branch_transitions[0].to_state == "#V#preferred"
        assert branch_transitions[0].condition_spec == {
            "kind": "context_value_equals",
            "key": "route",
            "value": "preferred",
        }
        assert branch_transitions[0].condition({"route": "preferred"}) is True
        assert branch_transitions[0].condition({"route": "fallback"}) is False
        assert branch_transitions[1].reason == "next_step"

    def test_invalid_explicit_condition_branch_fails_fast_with_reason_code(self):
        steps = [
            _make_step("#V#branch", invokes_action="route.action", next_step="#V#fallback"),
            _make_step("#V#fallback"),
        ]
        steps[0]["control_flow"]["conditions"] = [
            {
                "to": "#V#fallback",
                "reason": "broken_branch",
                "condition": {
                    "kind": "context_flag",
                    # Missing key -> actionable validation failure.
                },
            }
        ]
        graph = _make_graph(initial_step="#V#branch", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                with pytest.raises(ValueError) as exc_info:
                    load_workflow_definition_from_vontology("#V#test_workflow")

        assert "workflow_transition_condition_invalid" in str(exc_info.value)
        assert "context_flag_key_missing" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Input mapping via hasInputMap.
# ---------------------------------------------------------------------------


class TestInputMapping:
    def test_reads_input_map_key_equals_value(self):
        """hasInputMap entries like 'key=value' should populate action inputs."""
        step_docs = {
            "#V#step_with_inputs": {
                "concept_id": "#V#step_with_inputs",
                "relationships": {
                    "hasInputMap": ["prompt=user_prompt", "model=default"],
                },
            }
        }
        steps = [
            _make_step("#V#step_with_inputs", invokes_action="my.action"),
        ]
        graph = _make_graph(initial_step="#V#step_with_inputs", steps=steps)

        with _stub_fetch_concepts(step_docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step_with_inputs"].actions[0]
        assert action.inputs == {"prompt": "user_prompt", "model": "default"}

    def test_reads_input_map_colon_format(self):
        """hasInputMap entries like 'key:value' should also work."""
        step_docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {
                    "has_input_map": ["timeout:30"],
                },
            }
        }
        steps = [_make_step("#V#step", invokes_action="act")]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(step_docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step"].actions[0]
        assert action.inputs == {"timeout": "30"}

    def test_no_input_map_gives_empty_dict(self):
        """Steps without hasInputMap should have empty inputs."""
        steps = [_make_step("#V#step", invokes_action="act")]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step"].actions[0]
        assert action.inputs == {}


class TestSemanticContextMapping:
    def test_mapping_concept_id_pattern_becomes_dynamic_input_binding(self):
        mapping_id = "#V#workflow_mapping_target_type_id_to_concept_id_param"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                context_input_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step"].actions[0]
        assert action.inputs == {
            "concept_id": {
                "$context_key": "target_type_id",
                "$mapping_concept_id": mapping_id,
            }
        }
        assert defn.states["#V#step"].metadata["reads_context_keys"] == [
            "target_type_id"
        ]

    def test_mapping_description_fallback_is_supported(self):
        mapping_id = "#V#mapping_for_identify_type_step"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                context_input_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.get_preferred_text_for_concept",
                return_value={
                    "text": (
                        "Bind context key 'target_type_id' to the "
                        "'concept_id' parameter for deterministic lookup."
                    ),
                    "predicate": "hasDescription",
                    "lang": "en-NZ",
                },
            ):
                with patch(
                    "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                    return_value=(graph, []),
                ):
                    defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step"].actions[0]
        assert action.inputs == {
            "concept_id": {
                "$context_key": "target_type_id",
                "$mapping_concept_id": mapping_id,
            }
        }

    def test_structured_input_mapping_schema_is_supported(self):
        mapping_id = "#V#mapping_structured_input"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "context_key_to_tool_param",
                        "workflow_step_id": "#V#step",
                        "tool_id": "fetch_concept",
                        "context_key_concept_id": "#V#workflow_context_key_target_type_id",
                        "tool_param_name": "concept_id",
                    }
                },
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                context_input_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step"].actions[0]
        assert action.inputs == {
            "concept_id": {
                "$context_key": "target_type_id",
                "$mapping_concept_id": mapping_id,
            }
        }
        assert defn.states["#V#step"].metadata["reads_context_keys"] == [
            "target_type_id"
        ]

    def test_structured_input_mapping_tool_mismatch_is_rejected(self):
        mapping_id = "#V#mapping_structured_input_mismatch"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "context_key_to_tool_param",
                        "workflow_step_id": "#V#step",
                        "tool_id": "wrong_tool_name",
                        "context_key_concept_id": "#V#workflow_context_key_target_type_id",
                        "tool_param_name": "concept_id",
                    }
                },
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                context_input_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        action = defn.states["#V#step"].actions[0]
        assert action.inputs == {}
        assert defn.states["#V#step"].metadata["unresolved_input_mappings"] == [mapping_id]
        assert defn.states["#V#step"].metadata["invalid_input_mapping_specs"] == [
            {
                "mapping_concept_id": mapping_id,
                "reason_code": "schema_tool_mismatch",
            }
        ]


class TestContextOutputContractMetadata:
    def test_writes_context_keys_are_carried_to_state_metadata(self):
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                writes_context_keys=[
                    "#V#workflow_context_key_validated_type_id",
                    "#V#workflow_context_key_validated_type_name",
                ],
            ),
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.states["#V#step"].metadata["writes_context_keys"] == [
            "#V#workflow_context_key_validated_type_id",
            "#V#workflow_context_key_validated_type_name",
        ]


class TestToolOutputContextMappings:
    def test_output_mapping_concept_id_pattern_becomes_metadata(self):
        mapping_id = "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                tool_output_context_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.states["#V#step"].metadata["tool_output_context_mappings"] == [
            {
                "tool_output_field": "concept_id",
                "context_key": "validated_type_id",
                "mapping_concept_id": mapping_id,
            }
        ]

    def test_output_mapping_description_fallback_is_supported(self):
        mapping_id = "#V#mapping_for_output_contract"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                tool_output_context_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.get_preferred_text_for_concept",
                return_value={
                    "text": (
                        "Map tool output field 'concept_id' to context key "
                        "'#V#workflow_context_key_validated_type_id'."
                    ),
                    "predicate": "hasDescription",
                    "lang": "en-NZ",
                },
            ):
                with patch(
                    "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                    return_value=(graph, []),
                ):
                    defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.states["#V#step"].metadata["tool_output_context_mappings"] == [
            {
                "tool_output_field": "concept_id",
                "context_key": "validated_type_id",
                "mapping_concept_id": mapping_id,
            }
        ]

    def test_structured_output_mapping_schema_is_supported(self):
        mapping_id = "#V#mapping_structured_output"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "tool_output_field_to_context_key",
                        "workflow_step_id": "#V#step",
                        "tool_id": "fetch_concept",
                        "tool_output_field_name": "concept_id",
                        "target_context_key_concept_id": "#V#workflow_context_key_validated_type_id",
                    }
                },
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                tool_output_context_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.states["#V#step"].metadata["tool_output_context_mappings"] == [
            {
                "tool_output_field": "concept_id",
                "context_key": "validated_type_id",
                "mapping_concept_id": mapping_id,
            }
        ]

    def test_structured_output_mapping_with_unknown_fields_is_rejected(self):
        mapping_id = "#V#mapping_structured_output_bad"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            mapping_id: {
                "concept_id": mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "tool_output_field_to_context_key",
                        "workflow_step_id": "#V#step",
                        "tool_id": "fetch_concept",
                        "tool_output_field_name": "concept_id",
                        "target_context_key_concept_id": "#V#workflow_context_key_validated_type_id",
                        "unexpected_field": "not_allowed",
                    }
                },
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_action="fetch_concept",
                tool_output_context_mappings=[mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert "tool_output_context_mappings" not in defn.states["#V#step"].metadata
        assert defn.states["#V#step"].metadata["unresolved_output_mappings"] == [mapping_id]
        assert defn.states["#V#step"].metadata["invalid_output_mapping_specs"] == [
            {
                "mapping_concept_id": mapping_id,
                "reason_code": "schema_unknown_fields",
            }
        ]


class TestSubworkflowCompositionContracts:
    def test_load_definition_maps_invokes_workflow_to_subworkflow_action(self):
        input_mapping_id = "#V#mapping_subworkflow_input"
        output_mapping_id = "#V#mapping_subworkflow_output"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {},
            },
            input_mapping_id: {
                "concept_id": input_mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "context_key_to_tool_param",
                        "workflow_step_id": "#V#step",
                        "tool_id": "#V#child_workflow",
                        "context_key_concept_id": "#V#workflow_context_key_parent_input",
                        "tool_param_name": "child_input",
                    }
                },
                "relationships": {},
            },
            output_mapping_id: {
                "concept_id": output_mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "tool_output_field_to_context_key",
                        "workflow_step_id": "#V#step",
                        "tool_id": "#V#child_workflow",
                        "tool_output_field_name": "child_output",
                        "target_context_key_concept_id": (
                            "#V#workflow_context_key_parent_output"
                        ),
                    }
                },
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_workflow="#V#child_workflow",
                context_input_mappings=[input_mapping_id],
                tool_output_context_mappings=[output_mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#parent_workflow")

        assert defn is not None
        state = defn.states["#V#step"]
        assert state.actions[0].action_id == WORKFLOW_SUBWORKFLOW_ACTION_ID
        assert state.actions[0].inputs["workflow_id"] == "#V#child_workflow"
        assert state.actions[0].inputs["__parent_workflow_id"] == "#V#parent_workflow"
        assert state.actions[0].inputs["__parent_state_id"] == "#V#step"
        assert state.actions[0].inputs["child_input"] == {
            "$context_key": "parent_input",
            "$mapping_concept_id": input_mapping_id,
        }

        metadata = state.metadata
        assert metadata["invokes_workflow"] == "#V#child_workflow"
        assert metadata["subworkflow_contract"]["workflow_id"] == "#V#child_workflow"
        assert metadata["subworkflow_contract"]["provided_inputs"] == ["child_input"]
        assert metadata["subworkflow_contract"]["mapped_outputs"] == []
        assert metadata["tool_output_context_mappings"] == [
            {
                "tool_output_field": "child_output",
                "context_key": "parent_output",
                "mapping_concept_id": output_mapping_id,
            }
        ]

    def test_load_definition_preserves_subworkflow_failure_mode_and_result_mappings(self):
        input_mapping_id = "#V#mapping_subworkflow_input"
        result_mapping_id = "#V#mapping_subworkflow_result_output"
        failure_mapping_id = "#V#mapping_subworkflow_failure_output"
        docs = {
            "#V#step": {
                "concept_id": "#V#step",
                "relationships": {
                    "#V#hasInputMap": [
                        f"failure_mode={WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE}"
                    ],
                },
            },
            input_mapping_id: {
                "concept_id": input_mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "context_key_to_tool_param",
                        "workflow_step_id": "#V#step",
                        "tool_id": "#V#child_workflow",
                        "context_key_concept_id": "#V#workflow_context_key_parent_input",
                        "tool_param_name": "child_input",
                    }
                },
                "relationships": {},
            },
            result_mapping_id: {
                "concept_id": result_mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "tool_output_field_to_context_key",
                        "workflow_step_id": "#V#step",
                        "tool_id": "#V#child_workflow",
                        "tool_output_field_name": "result.child_output",
                        "target_context_key_concept_id": (
                            "#V#workflow_context_key_parent_output"
                        ),
                    }
                },
                "relationships": {},
            },
            failure_mapping_id: {
                "concept_id": failure_mapping_id,
                "concept_data": {
                    "workflow_mapping_spec": {
                        "schema_version": 1,
                        "mapping_type": "tool_output_field_to_context_key",
                        "workflow_step_id": "#V#step",
                        "tool_id": "#V#child_workflow",
                        "tool_output_field_name": "child_workflow_failed",
                        "target_context_key_concept_id": (
                            "#V#workflow_context_key_parent_failed"
                        ),
                    }
                },
                "relationships": {},
            },
        }
        steps = [
            _make_step(
                "#V#step",
                invokes_workflow="#V#child_workflow",
                context_input_mappings=[input_mapping_id],
                tool_output_context_mappings=[result_mapping_id, failure_mapping_id],
            )
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(docs), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#parent_workflow")

        assert defn is not None
        state = defn.states["#V#step"]
        assert state.actions[0].inputs["failure_mode"] == (
            WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE
        )
        metadata = state.metadata
        assert metadata["subworkflow_contract"]["failure_mode"] == (
            WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE
        )
        assert metadata["subworkflow_contract"]["mapped_outputs"] == ["child_output"]
        assert metadata["tool_output_context_mappings"] == [
            {
                "tool_output_field": "result.child_output",
                "context_key": "parent_output",
                "mapping_concept_id": result_mapping_id,
            },
            {
                "tool_output_field": "child_workflow_failed",
                "context_key": "parent_failed",
                "mapping_concept_id": failure_mapping_id,
            },
        ]

    def test_detect_vacuous_steps_treats_invokes_workflow_as_executable_contract(self):
        graph = _make_graph(
            initial_step="#V#start",
            steps=[
                _make_step(
                    "#V#start",
                    invokes_workflow="#V#child_workflow",
                )
            ],
        )

        issues = detect_vacuous_workflow_steps(
            workflow_id="#V#parent_workflow",
            graph=graph,
        )

        assert issues == []

    def test_detect_vacuous_steps_ignores_terminal_marker_leaf_step(self):
        graph = _make_graph(
            initial_step="#V#start",
            steps=[
                _make_step(
                    "#V#start",
                    invokes_action="tool.perform",
                    next_step="#V#completed",
                ),
                _make_step("#V#completed"),
            ],
        )

        issues = detect_vacuous_workflow_steps(
            workflow_id="#V#parent_workflow",
            graph=graph,
        )

        assert issues == []


# ---------------------------------------------------------------------------
# Preconditions/effects metadata.
# ---------------------------------------------------------------------------


class TestMetadataCarrythrough:
    def test_preconditions_and_effects_in_metadata(self):
        """Preconditions, effects, and variable lists should appear in state metadata."""
        steps = [
            _make_step(
                "#V#step",
                invokes_action="act",
                preconditions=["#V#user_authenticated"],
                effects=["#V#task_created"],
                reads_variables=["user_id"],
                writes_variables=["task_id"],
            ),
        ]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        meta = defn.states["#V#step"].metadata
        assert meta["preconditions"] == ["#V#user_authenticated"]
        assert meta["effects"] == ["#V#task_created"]
        assert meta["reads_variables"] == ["user_id"]
        assert meta["writes_variables"] == ["task_id"]

    def test_empty_metadata_when_no_annotations(self):
        """Steps without annotations should have empty metadata."""
        steps = [_make_step("#V#step", invokes_action="act")]
        graph = _make_graph(initial_step="#V#step", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.states["#V#step"].metadata == {}


# ---------------------------------------------------------------------------
# Integration: multi-step workflow produces correct definition.
# ---------------------------------------------------------------------------


class TestFullWorkflowConversion:
    def test_three_step_linear_workflow(self):
        """A→B→C linear graph should produce correct initial state and transitions."""
        steps = [
            _make_step("#V#a", invokes_action="act.a", next_step="#V#b"),
            _make_step("#V#b", invokes_action="act.b", next_step="#V#c"),
            _make_step("#V#c", invokes_action="act.c"),  # terminal
        ]
        graph = _make_graph(initial_step="#V#a", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative("Test workflow"):
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        assert defn.workflow_id == "#V#test_workflow"
        assert defn.initial_state == "#V#a"
        assert defn.purpose == "Test workflow"
        assert len(defn.states) == 3
        assert defn.states["#V#c"].terminal is True

    def test_none_returned_for_empty_graph(self):
        """No graph → None returned."""
        with patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(None, ["workflow_concept_not_found"]),
        ):
            defn = load_workflow_definition_from_vontology("#V#missing")

        assert defn is None

    def test_branching_workflow(self):
        """A workflow with on_true/on_false branching and failure/unknown routes."""
        steps = [
            _make_step(
                "#V#check",
                invokes_action="validate",
                on_true="#V#proceed",
                on_false="#V#retry",
                on_failure="#V#abort",
                on_unknown="#V#escalate",
            ),
            _make_step("#V#proceed"),  # terminal
            _make_step("#V#retry", invokes_action="retry", next_step="#V#check"),
            _make_step("#V#abort"),  # terminal
            _make_step("#V#escalate"),  # terminal
        ]
        graph = _make_graph(initial_step="#V#check", steps=steps)

        with _stub_fetch_concepts(), _stub_narrative():
            with patch(
                "src.backend.workflows.vontology_loader.build_workflow_process_graph",
                return_value=(graph, []),
            ):
                defn = load_workflow_definition_from_vontology("#V#test_workflow")

        assert defn is not None
        check = defn.states["#V#check"]
        # Should have 4 transitions: on_failure, on_unknown, on_true, on_false.
        assert len(check.transitions) == 4
        reasons = [t.reason for t in check.transitions]
        assert reasons == ["on_failure", "on_unknown", "on_true", "on_false"]

        # Verify condition semantics.
        # on_failure fires when last_action_failed.
        assert check.transitions[0].condition({"last_action_failed": True}) is True
        # on_unknown fires when last_action_unknown.
        assert check.transitions[1].condition({"last_action_unknown": True}) is True
        # on_true fires when result is truthy.
        assert check.transitions[2].condition({"result": True}) is True
        assert check.transitions[2].condition({"result": False}) is False
        # Explicit nested result must override generic success flags.
        assert (
            check.transitions[2].condition(
                {"last_step_ok": True, "result": {"result": False}}
            )
            is False
        )
        assert (
            check.transitions[3].condition(
                {"last_step_ok": True, "result": {"result": False}}
            )
            is True
        )
        # When no explicit result exists, fallback uses last_step_ok.
        assert check.transitions[2].condition({"last_step_ok": True}) is True
        assert check.transitions[3].condition({"last_step_ok": False}) is True

        # Terminal states.
        assert defn.states["#V#proceed"].terminal is True
        assert defn.states["#V#abort"].terminal is True
        assert defn.states["#V#escalate"].terminal is True
        assert defn.states["#V#retry"].terminal is False


class TestDiscoverWorkflowIds:
    def test_discovers_canonical_initial_step_predicates(self):
        captured_queries: list[dict[str, Any]] = []

        def _fake_find(query, *_args, **_kwargs):
            if isinstance(query, dict):
                captured_queries.append(query)

            if isinstance(query, dict) and isinstance(query.get("$or"), list):
                if any(
                    isinstance(item, dict)
                    and "relationships.#V#hasInitialStep" in item
                    for item in query["$or"]
                ):
                    return [{"concept_id": "#V#wf_canonical"}]
            return []

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find",
            side_effect=_fake_find,
        ), patch(
            "src.backend.workflows.vontology_loader._discover_workflow_type_family_ids",
            return_value=set(),
        ):
            workflow_ids = discover_workflow_ids()

        assert workflow_ids == ["#V#wf_canonical"]
        assert any(
            isinstance(query, dict)
            and isinstance(query.get("$or"), list)
            and any(
                isinstance(item, dict)
                and "relationships.#V#hasInitialStep" in item
                for item in query["$or"]
            )
            for query in captured_queries
        )

    def test_discovers_instance_typed_workflows_with_canonical_graph_predicates(self):
        def _fake_find(query, *_args, **_kwargs):
            if isinstance(query, dict) and isinstance(query.get("$or"), list):
                if any(
                    isinstance(item, dict) and "relationships.is_an_instance_of" in item
                    for item in query["$or"]
                ):
                    return [
                        {
                            "concept_id": "#V#wf_zeta",
                            "relationships": {
                                "is_an_instance_of": ["#V#workflow_variant"],
                                "#V#hasInitialStep": "#V#step_z",
                            },
                        },
                        {
                            "concept_id": "#V#wf_alpha",
                            "relationships": {
                                "is_an_instance_of": ["#V#workflow_variant"],
                                "#V#hasInitialStep": "#V#step_a",
                            },
                        },
                    ]
            return []

        with patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find",
            side_effect=_fake_find,
        ), patch(
            "src.backend.workflows.vontology_loader._discover_workflow_type_family_ids",
            return_value={"#V#workflow_variant"},
        ):
            workflow_ids = discover_workflow_ids()

        assert workflow_ids == ["#V#wf_alpha", "#V#wf_zeta"]
