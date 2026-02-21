"""Tests for the forward inference planning workflow (JVNAUTOSCI-924)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_mock_db(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    from src.backend.db.mongo_client import get_db
    from src.backend.workflows.durable import instance_manager

    db = get_db()
    if db is not None:
        try:
            db.drop_collection("workflow_instances")
            db.drop_collection("workflow_schedules")
        except Exception:
            pass

    instance_manager._indexes_ensured = False
    yield


class TestPlanningWorkflowDefinition:
    def test_workflow_structure(self) -> None:
        from src.backend.workflows.durable.planning_workflow import (
            PLANNING_WORKFLOW_ID,
            build_planning_workflow,
        )

        wf = build_planning_workflow()
        assert wf.workflow_id == PLANNING_WORKFLOW_ID
        assert wf.initial_state == "assess"
        assert "assess" in wf.states
        assert "infer" in wf.states
        assert "validate" in wf.states
        assert "complete" in wf.states
        assert "failed" in wf.states

    def test_failure_routes_exist(self) -> None:
        from src.backend.workflows.durable.planning_workflow import build_planning_workflow

        wf = build_planning_workflow()
        for state_id in ("assess", "infer", "validate"):
            reasons = [t.reason for t in wf.states[state_id].transitions]
            assert "on_failure" in reasons


class TestPlanningAssessHandler:
    def test_assess_collects_inventory_and_context(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import (
            _handle_assess_context,
        )

        with (
            patch(
                "src.backend.workflows.durable.planning_workflow._collect_available_tool_names",
                return_value=["search_concepts", "workflow_create_instance"],
            ),
            patch(
                "src.backend.workflows.durable.planning_workflow._collect_available_workflow_ids",
                return_value=["#V#enrichment_workflow", "#V#planning_workflow"],
            ),
        ):
            req = WorkflowActionRequest(
                action_id="planning.assess_context",
                inputs={},
                environment=WorkflowEnvironment(llm_client=None),
                data={
                    "goal": "Build a concrete execution plan for improving workflow reliability",
                    "constraints": ["Do not mutate production data"],
                    "max_actions": 4,
                },
            )

            result = _handle_assess_context(req)
            assert result.ok
            planning_context = result.outputs["planning_context"]
            assert planning_context["goal"].startswith("Build a concrete")
            assert planning_context["max_actions"] == 4
            inventory = result.outputs["planning_inventory"]
            assert "search_concepts" in inventory["available_tool_names"]
            assert "#V#planning_workflow" in inventory["available_workflow_ids"]

    def test_assess_requires_goal(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import (
            _handle_assess_context,
        )

        req = WorkflowActionRequest(
            action_id="planning.assess_context",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )

        result = _handle_assess_context(req)
        assert result.ok is False
        assert "goal_required" in str(result.error)


class TestPlanningInferHandler:
    def test_infer_parses_plan_json(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import _handle_infer_plan

        mock_llm = MagicMock()
        mock_llm.generate.return_value = json.dumps(
            {
                "goal": "Improve reliability",
                "assumptions": ["Workflow tools are enabled"],
                "actions": [
                    {
                        "id": "a1",
                        "title": "Check workflow capability matrix",
                        "kind": "tool_call",
                        "tool_name": "workflow_list_definitions",
                        "payload": {"limit": 20},
                    },
                    {
                        "id": "a2",
                        "title": "Run planning workflow",
                        "kind": "workflow_invocation",
                        "workflow_id": "#V#planning_workflow",
                        "dependencies": ["a1"],
                    },
                ],
            }
        )

        with patch(
            "src.backend.workflows.durable.planning_workflow._resolve_planning_prompt",
            return_value="Planner prompt.",
        ):
            req = WorkflowActionRequest(
                action_id="planning.infer_plan",
                inputs={},
                environment=WorkflowEnvironment(llm_client=mock_llm),
                data={
                    "planning_context": {
                        "goal": "Improve reliability",
                        "max_actions": 5,
                        "planning_horizon": 3,
                    },
                    "planning_inventory": {
                        "available_tool_names": ["workflow_list_definitions"],
                        "available_workflow_ids": ["#V#planning_workflow"],
                    },
                },
            )

            result = _handle_infer_plan(req)
            assert result.ok
            assert result.outputs["planning_goal"] == "Improve reliability"
            assert len(result.outputs["planning_actions"]) == 2
            assert result.outputs["planning_actions"][0]["kind"] == "tool_call"

    def test_infer_fails_on_unparseable_response(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import _handle_infer_plan

        mock_llm = MagicMock()
        mock_llm.generate.return_value = "I cannot provide JSON."

        req = WorkflowActionRequest(
            action_id="planning.infer_plan",
            inputs={},
            environment=WorkflowEnvironment(llm_client=mock_llm),
            data={"planning_context": {"goal": "Test parse failure"}},
        )

        result = _handle_infer_plan(req)
        assert result.ok is False
        assert "planning_json_parse_failed" in str(result.error)


class TestPlanningValidateHandler:
    def test_validate_marks_actionability(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import _handle_validate_plan

        req = WorkflowActionRequest(
            action_id="planning.validate_plan",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={
                "planning_context": {"max_actions": 5, "strict_validation": False},
                "planning_inventory": {
                    "available_tool_names": ["workflow_list_definitions"],
                    "available_workflow_ids": ["#V#planning_workflow"],
                },
                "planning_actions": [
                    {
                        "id": "a1",
                        "kind": "tool_call",
                        "title": "Inspect workflow registry",
                        "tool_name": "workflow_list_definitions",
                        "payload": {"limit": 10},
                    },
                    {
                        "id": "a2",
                        "kind": "workflow_invocation",
                        "title": "Start planning run",
                        "workflow_id": "#V#planning_workflow",
                        "dependencies": ["a1"],
                    },
                ],
            },
        )

        result = _handle_validate_plan(req)
        assert result.ok
        validation = result.outputs["planning_validation"]
        assert validation["actionable_actions"] == 2
        assert validation["non_actionable_actions"] == 0
        assert len(result.outputs["actionable_next_actions"]) == 2

    def test_validate_strict_mode_fails_for_unknown_tools(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import _handle_validate_plan

        req = WorkflowActionRequest(
            action_id="planning.validate_plan",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={
                "planning_context": {"strict_validation": True},
                "planning_inventory": {
                    "available_tool_names": ["search_concepts"],
                    "available_workflow_ids": [],
                },
                "planning_actions": [
                    {
                        "id": "a1",
                        "kind": "tool_call",
                        "title": "Invoke unsupported tool",
                        "tool_name": "workflow_list_definitions",
                    },
                ],
            },
        )

        result = _handle_validate_plan(req)
        assert result.ok is False
        assert result.outputs["planning_validation"]["non_actionable_actions"] == 1
        assert "planning_validation_failed" in str(result.error)


class TestPlanningFinaliseHandler:
    def test_finalise_emits_structured_result(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.planning_workflow import _handle_finalise

        req = WorkflowActionRequest(
            action_id="planning.finalise",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={
                "planning_context": {"goal": "Improve planning quality"},
                "planning_validation": {"passed": True},
                "planning_assumptions": ["Tools are available"],
                "validated_planning_actions": [{"id": "a1", "title": "Action"}],
                "actionable_next_actions": [{"id": "a1", "title": "Action"}],
                "planning_trace": [{"stage": "assess", "event": "planning_context_ready"}],
            },
        )

        result = _handle_finalise(req)
        assert result.ok
        payload = result.outputs["planning_result"]
        assert payload["success"] is True
        assert payload["goal"] == "Improve planning quality"
        assert payload["total_actions"] == 1
        assert payload["actionable_actions"] == 1


class TestPlanningRegistration:
    def test_workflow_registration(self) -> None:
        from src.backend.workflows.durable.planning_workflow import (
            PLANNING_WORKFLOW_ID,
            get_planning_workflow_registration,
        )

        registration = get_planning_workflow_registration()
        assert registration.workflow_id == PLANNING_WORKFLOW_ID
        assert registration.source == "built_in"

    def test_action_registration(self) -> None:
        from src.backend.workflows.action_registry import ActionRegistry
        from src.backend.workflows.durable.planning_workflow import register_planning_actions

        registry = ActionRegistry()
        register_planning_actions(registry)

        for action_id in (
            "planning.assess_context",
            "planning.infer_plan",
            "planning.validate_plan",
            "planning.finalise",
        ):
            assert registry.has(action_id), f"Missing action: {action_id}"

    def test_registered_in_factory(self) -> None:
        from src.backend.workflows.durable.planning_workflow import PLANNING_WORKFLOW_ID

        with patch(
            "src.backend.workflows.durable.registry_factory.discover_workflow_ids",
            return_value=[],
        ):
            from src.backend.workflows.durable.registry_factory import build_workflow_registry

            registry = build_workflow_registry()
            assert PLANNING_WORKFLOW_ID in registry.all_workflow_ids()
