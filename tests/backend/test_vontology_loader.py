"""Tests for vontology_loader.py — Phase 3.2 correctness.

JVNAUTOSCI-922 Phase 3.2: Verifies that load_workflow_definition_from_vontology()
correctly converts Vontology process graphs into executable WorkflowDefinitions,
including:
- initial_step key resolution
- Lambda closure correctness (no late-binding bugs)
- on_failure transition support
- Input mapping via hasInputMap
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
    build_workflow_process_graph,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
    _normalise_relationship_targets,
    _first_relationship_target,
    _all_relationship_targets,
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
    next_step: str | None = None,
    on_true: str | None = None,
    on_false: str | None = None,
    on_failure: str | None = None,
    preconditions: List[str] | None = None,
    effects: List[str] | None = None,
    reads_variables: List[str] | None = None,
    writes_variables: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "step_id": step_id,
        "name": step_id,
        "invokes_action": invokes_action,
        "preconditions": preconditions or [],
        "effects": effects or [],
        "reads_variables": reads_variables or [],
        "writes_variables": writes_variables or [],
        "control_flow": {
            "next": next_step,
            "on_true": on_true,
            "on_false": on_false,
            "on_failure": on_failure,
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
        assert a_transitions[1].to_state == "#V#no_a"
        assert a_transitions[1].reason == "on_false"

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
        """A workflow with on_true/on_false branching and on_failure."""
        steps = [
            _make_step(
                "#V#check",
                invokes_action="validate",
                on_true="#V#proceed",
                on_false="#V#retry",
                on_failure="#V#abort",
            ),
            _make_step("#V#proceed"),  # terminal
            _make_step("#V#retry", invokes_action="retry", next_step="#V#check"),
            _make_step("#V#abort"),  # terminal
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
        # Should have 3 transitions: on_failure, on_true, on_false
        assert len(check.transitions) == 3
        reasons = [t.reason for t in check.transitions]
        assert reasons == ["on_failure", "on_true", "on_false"]

        # Verify condition semantics.
        # on_failure fires when last_action_failed.
        assert check.transitions[0].condition({"last_action_failed": True}) is True
        # on_true fires when result is truthy.
        assert check.transitions[1].condition({"result": True}) is True
        assert check.transitions[1].condition({"result": False}) is False

        # Terminal states.
        assert defn.states["#V#proceed"].terminal is True
        assert defn.states["#V#abort"].terminal is True
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
