"""Tests for the todo_refresh workflow handlers (JVNAUTOSCI-922 Phase 2.3).

Covers:
  - Each of the 5 action handlers in isolation (unit tests)
  - Full workflow execution through the engine (integration test)

All external dependencies (gateway, LLM, task service) are mocked so
these tests run without network or database access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch


from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.definitions import TODO_REFRESH_WORKFLOW_ID
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.services.prompt_template_service import RenderedPrompt
from workflow_test_support import (
    build_authoritative_test_workflow_definition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_env(**overrides: Any) -> WorkflowEnvironment:
    """Build a ``WorkflowEnvironment`` with sensible defaults."""
    defaults = dict(
        llm_client=MagicMock(),
        gateway=MagicMock(),
        model="test-model",
        user_namespace="#V#test_user",
        default_gmail_profile="test@example.com",
    )
    defaults.update(overrides)
    return WorkflowEnvironment(**defaults)  # type: ignore[arg-type]


def _make_request(
    action_id: str = "test",
    data: Dict[str, Any] | None = None,
    env: WorkflowEnvironment | None = None,
) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id=action_id,
        inputs={},
        environment=env or _make_env(),
        data=data if data is not None else {},
    )


@dataclass
class _FakeGatewayResult:
    payload: dict[str, Any] | None = None
    duration_ms: float = 0.0


def _build_orchestrator_stub():
    """Create a minimal orchestrator instance with logger, for handler tests.

    We avoid instantiating the real ``InternalMCPChatOrchestrator`` because it
    requires many constructor args.  Instead we create a bare object and graft
    on the handler methods + logger.
    """
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    import logging

    # Use __new__ to skip __init__; we only need the handler methods + logger.
    stub = object.__new__(InternalMCPChatOrchestrator)
    stub._logger = logging.getLogger("test_todo_refresh")
    # Set the class-level TTL attribute for testing (default 3600).
    stub._TODO_CACHE_TTL_SECONDS = 3600
    prompt_templates = {
        "#V#todo_refresh_extract_tasks_prompt": (
            "Analyse the following email digest and extract actionable to-do items.\n\n"
            "EMAILS:\n{email_digest}"
        ),
        "#V#todo_refresh_extract_tasks_system_prompt": (
            "You extract actionable tasks from emails. Reply ONLY with a JSON array."
        ),
        "#V#todo_refresh_prioritise_prompt": (
            "Given these tasks, assign a priority to each.\n\n"
            "Tasks:\n{task_summaries}"
        ),
        "#V#todo_refresh_prioritise_system_prompt": (
            "You prioritise tasks. Reply ONLY with a JSON array."
        ),
    }

    class _PromptTemplateStub:
        def render_prompt(
            self,
            concept_ids,
            *,
            variables=None,
            fallback=None,
            max_chars=None,
        ):
            for prompt_id in concept_ids or ():
                template = prompt_templates.get(str(prompt_id))
                if not template:
                    continue
                text = template
                for key, value in dict(variables or {}).items():
                    text = text.replace("{" + key + "}", str(value))
                return RenderedPrompt(
                    prompt_id=str(prompt_id),
                    text=text,
                    variables=dict(variables or {}),
                    truncated=False,
                )
            if fallback is None:
                return None
            return RenderedPrompt(
                prompt_id=None,
                text=str(fallback),
                variables=dict(variables or {}),
                truncated=False,
            )

    stub._prompt_templates = cast(Any, _PromptTemplateStub())
    return stub


# ---------------------------------------------------------------------------
# check_cache handler
# ---------------------------------------------------------------------------


class TestCheckCache:
    """Tests for ``_action_todo_refresh_check_cache``."""

    def test_no_user_namespace_triggers_refresh(self):
        orch = _build_orchestrator_stub()
        req = _make_request(data={}, env=_make_env(user_namespace=None))
        result = orch._action_todo_refresh_check_cache(req)

        assert result.ok
        assert result.outputs["todo_refresh_needed"] is True

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_no_existing_tasks_triggers_refresh(self, mock_get_tasks):
        mock_get_tasks.return_value = []
        orch = _build_orchestrator_stub()
        req = _make_request(data={"user_namespace": "#V#alice"})
        result = orch._action_todo_refresh_check_cache(req)

        assert result.ok
        assert result.outputs["todo_refresh_needed"] is True
        mock_get_tasks.assert_called_once_with("#V#alice")

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_fresh_tasks_skip_refresh(self, mock_get_tasks):
        now = datetime.now(timezone.utc)
        mock_get_tasks.return_value = [
            {"title": "Recent task", "updated_at": now.isoformat()},
        ]
        orch = _build_orchestrator_stub()
        orch._TODO_CACHE_TTL_SECONDS = 3600
        req = _make_request(data={"user_namespace": "#V#alice"})
        result = orch._action_todo_refresh_check_cache(req)

        assert result.ok
        assert result.outputs["todo_refresh_needed"] is False

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_stale_tasks_trigger_refresh(self, mock_get_tasks):
        stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_get_tasks.return_value = [
            {"title": "Old task", "updated_at": stale_time.isoformat()},
        ]
        orch = _build_orchestrator_stub()
        orch._TODO_CACHE_TTL_SECONDS = 3600  # 1 hour
        req = _make_request(data={"user_namespace": "#V#alice"})
        result = orch._action_todo_refresh_check_cache(req)

        assert result.ok
        assert result.outputs["todo_refresh_needed"] is True

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_service_exception_triggers_refresh(self, mock_get_tasks):
        mock_get_tasks.side_effect = RuntimeError("DB down")
        orch = _build_orchestrator_stub()
        req = _make_request(data={"user_namespace": "#V#alice"})
        result = orch._action_todo_refresh_check_cache(req)

        assert result.ok
        assert result.outputs["todo_refresh_needed"] is True


# ---------------------------------------------------------------------------
# fetch_gmail handler
# ---------------------------------------------------------------------------


class TestFetchGmail:
    """Tests for ``_action_todo_refresh_fetch_gmail``."""

    def test_no_gateway_returns_empty(self):
        orch = _build_orchestrator_stub()
        req = _make_request(env=_make_env(gateway=None))
        result = orch._action_todo_refresh_fetch_gmail(req)

        assert result.ok
        assert result.outputs["raw_gmail_messages"] == []
        assert result.outputs["gmail_fetch_ok"] is False

    def test_no_gmail_profile_returns_empty(self):
        orch = _build_orchestrator_stub()
        req = _make_request(
            data={},
            env=_make_env(default_gmail_profile=None),
        )
        result = orch._action_todo_refresh_fetch_gmail(req)

        assert result.ok
        assert result.outputs["raw_gmail_messages"] == []
        assert result.outputs["gmail_fetch_ok"] is False

    def test_empty_inbox_returns_ok(self):
        gateway = MagicMock()
        gateway.invoke.return_value = _FakeGatewayResult(payload={"messages": []})
        orch = _build_orchestrator_stub()
        req = _make_request(env=_make_env(gateway=gateway))
        result = orch._action_todo_refresh_fetch_gmail(req)

        assert result.ok
        assert result.outputs["raw_gmail_messages"] == []
        assert result.outputs["gmail_fetch_ok"] is True

    def test_fetches_message_details(self):
        gateway = MagicMock()
        # First call: list_messages returns summaries.
        list_payload = {
            "messages": [
                {"id": "msg1", "snippet": "Please review the PR"},
                {"id": "msg2", "snippet": "Meeting tomorrow"},
            ]
        }
        msg1_payload = {"subject": "PR Review", "body": "Please review PR #42"}
        msg2_payload = {"subject": "Meeting", "body": "Team meeting at 10am"}

        gateway.invoke.side_effect = [
            _FakeGatewayResult(payload=list_payload),
            _FakeGatewayResult(payload=msg1_payload),
            _FakeGatewayResult(payload=msg2_payload),
        ]

        orch = _build_orchestrator_stub()
        req = _make_request(env=_make_env(gateway=gateway))
        result = orch._action_todo_refresh_fetch_gmail(req)

        assert result.ok
        assert len(result.outputs["raw_gmail_messages"]) == 2
        assert result.outputs["gmail_fetch_ok"] is True
        assert gateway.invoke.call_count == 3  # 1 list + 2 get

    def test_gateway_exception_returns_gracefully(self):
        gateway = MagicMock()
        gateway.invoke.side_effect = RuntimeError("Network error")
        orch = _build_orchestrator_stub()
        req = _make_request(env=_make_env(gateway=gateway))
        result = orch._action_todo_refresh_fetch_gmail(req)

        assert result.ok
        assert result.outputs["raw_gmail_messages"] == []
        assert result.outputs["gmail_fetch_ok"] is False


# ---------------------------------------------------------------------------
# extract_tasks handler
# ---------------------------------------------------------------------------


class TestExtractTasks:
    """Tests for ``_action_todo_refresh_extract_tasks``."""

    def test_no_messages_returns_empty(self):
        orch = _build_orchestrator_stub()
        req = _make_request(data={"raw_gmail_messages": []})
        result = orch._action_todo_refresh_extract_tasks(req)

        assert result.ok
        assert result.outputs["extracted_tasks"] == []

    def test_parses_llm_json_response(self):
        llm_client = MagicMock()
        llm_response = json.dumps(
            [
                {
                    "title": "Review PR #42",
                    "description": "Review the pull request",
                    "source_email_index": 1,
                },
                {
                    "title": "Schedule meeting",
                    "description": "Set up team meeting",
                    "source_email_index": 2,
                },
            ]
        )
        llm_client.generate.return_value = llm_response

        orch = _build_orchestrator_stub()
        req = _make_request(
            data={
                "raw_gmail_messages": [
                    {"subject": "PR Review", "body": "Please review"},
                    {"subject": "Meeting", "body": "Schedule a meeting"},
                ],
                "aux_llm_calls": [],
            },
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_extract_tasks(req)

        assert result.ok
        tasks = result.outputs["extracted_tasks"]
        assert len(tasks) == 2
        assert tasks[0]["title"] == "Review PR #42"
        assert tasks[1]["title"] == "Schedule meeting"

    def test_handles_markdown_fenced_response(self):
        llm_client = MagicMock()
        llm_client.generate.return_value = (
            "```json\n"
            '[{"title": "Fix bug", "description": "Fix the login bug"}]\n'
            "```"
        )

        orch = _build_orchestrator_stub()
        req = _make_request(
            data={
                "raw_gmail_messages": [{"subject": "Bug", "body": "Fix login"}],
                "aux_llm_calls": [],
            },
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_extract_tasks(req)

        assert result.ok
        assert len(result.outputs["extracted_tasks"]) == 1
        assert result.outputs["extracted_tasks"][0]["title"] == "Fix bug"

    def test_llm_failure_returns_empty(self):
        llm_client = MagicMock()
        llm_client.generate.side_effect = RuntimeError("LLM down")

        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"raw_gmail_messages": [{"subject": "Test", "body": "Body"}]},
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_extract_tasks(req)

        assert result.ok
        assert result.outputs["extracted_tasks"] == []

    def test_invalid_json_returns_empty(self):
        llm_client = MagicMock()
        llm_client.generate.return_value = "This is not JSON at all"

        orch = _build_orchestrator_stub()
        req = _make_request(
            data={
                "raw_gmail_messages": [{"subject": "Test", "body": "Body"}],
                "aux_llm_calls": [],
            },
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_extract_tasks(req)

        assert result.ok
        assert result.outputs["extracted_tasks"] == []

    def test_logs_to_aux_llm_calls(self):
        llm_client = MagicMock()
        llm_client.generate.return_value = "[]"

        aux_log: list = []
        orch = _build_orchestrator_stub()
        req = _make_request(
            data={
                "raw_gmail_messages": [{"subject": "X", "body": "Y"}],
                "aux_llm_calls": aux_log,
            },
            env=_make_env(llm_client=llm_client),
        )
        orch._action_todo_refresh_extract_tasks(req)

        assert len(aux_log) == 1
        assert aux_log[0]["type"] == "todo_refresh.extract_tasks"


# ---------------------------------------------------------------------------
# prioritise handler
# ---------------------------------------------------------------------------


class TestPrioritise:
    """Tests for ``_action_todo_refresh_prioritise``."""

    def test_empty_tasks_returns_empty(self):
        orch = _build_orchestrator_stub()
        req = _make_request(data={"extracted_tasks": []})
        result = orch._action_todo_refresh_prioritise(req)

        assert result.ok
        assert result.outputs["prioritised_tasks"] == []

    def test_small_batch_uses_heuristic(self):
        tasks = [
            {"title": "Task A", "description": "Do A"},
            {"title": "Task B", "description": "Do B"},
        ]
        llm_client = MagicMock()
        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"extracted_tasks": tasks},
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_prioritise(req)

        assert result.ok
        for t in result.outputs["prioritised_tasks"]:
            assert t["priority"] == "medium"
        # LLM should NOT be called for <=3 tasks.
        llm_client.generate.assert_not_called()

    def test_large_batch_calls_llm(self):
        tasks = [{"title": f"Task {i}", "description": f"Do {i}"} for i in range(5)]
        llm_client = MagicMock()
        llm_client.generate.return_value = json.dumps(
            [
                {"index": 0, "priority": "high"},
                {"index": 1, "priority": "low"},
                {"index": 2, "priority": "critical"},
                {"index": 3, "priority": "medium"},
                {"index": 4, "priority": "high"},
            ]
        )

        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"extracted_tasks": tasks},
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_prioritise(req)

        assert result.ok
        prioritised = result.outputs["prioritised_tasks"]
        assert prioritised[0]["priority"] == "high"
        assert prioritised[1]["priority"] == "low"
        assert prioritised[2]["priority"] == "critical"
        llm_client.generate.assert_called_once()

    def test_llm_failure_defaults_to_medium(self):
        tasks = [{"title": f"Task {i}", "description": ""} for i in range(5)]
        llm_client = MagicMock()
        llm_client.generate.side_effect = RuntimeError("LLM error")

        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"extracted_tasks": tasks},
            env=_make_env(llm_client=llm_client),
        )
        result = orch._action_todo_refresh_prioritise(req)

        assert result.ok
        for t in result.outputs["prioritised_tasks"]:
            assert t["priority"] == "medium"


# ---------------------------------------------------------------------------
# summarise handler
# ---------------------------------------------------------------------------


class TestSummarise:
    """Tests for ``_action_todo_refresh_summarise``."""

    def test_no_tasks_returns_empty_summary(self):
        orch = _build_orchestrator_stub()
        req = _make_request(data={"prioritised_tasks": []})
        result = orch._action_todo_refresh_summarise(req)

        assert result.ok
        assert result.outputs["persisted_tasks"] == []
        assert "No new tasks" in result.outputs["todo_summary"]

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    @patch("src.backend.services.task_management_service.create_task")
    def test_creates_tasks_and_produces_summary(self, mock_create, mock_get_user):
        mock_get_user.return_value = []  # No existing tasks.
        mock_create.side_effect = lambda title, description, **kw: {
            "task_concept_id": f"#V#task_{title.lower().replace(' ', '_')}_abc",
            "title": title,
            "priority": kw.get("priority", "medium"),
            "status": "pending",
        }

        tasks = [
            {"title": "Review PR", "description": "Review #42", "priority": "high"},
            {"title": "Fix bug", "description": "Fix login", "priority": "low"},
        ]
        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"prioritised_tasks": tasks, "user_namespace": "#V#alice"},
        )
        result = orch._action_todo_refresh_summarise(req)

        assert result.ok
        assert len(result.outputs["persisted_tasks"]) == 2
        assert "2 new task(s) created" in result.outputs["todo_summary"]
        assert "[HIGH]" in result.outputs["todo_summary"]
        assert mock_create.call_count == 2

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    @patch("src.backend.services.task_management_service.create_task")
    def test_deduplicates_existing_tasks(self, mock_create, mock_get_user):
        mock_get_user.return_value = [
            {"title": "Review PR", "status": "pending"},
        ]
        mock_create.side_effect = lambda title, description, **kw: {
            "task_concept_id": "#V#task_new_abc",
            "title": title,
            "priority": kw.get("priority", "medium"),
            "status": "pending",
        }

        tasks = [
            {"title": "Review PR", "description": "Duplicate", "priority": "high"},
            {"title": "New task", "description": "Something new", "priority": "medium"},
        ]
        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"prioritised_tasks": tasks, "user_namespace": "#V#alice"},
        )
        result = orch._action_todo_refresh_summarise(req)

        assert result.ok
        assert len(result.outputs["persisted_tasks"]) == 1
        assert "1 duplicate(s) skipped" in result.outputs["todo_summary"]
        mock_create.assert_called_once()

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    @patch("src.backend.services.task_management_service.create_task")
    def test_create_task_failure_is_non_fatal(self, mock_create, mock_get_user):
        mock_get_user.return_value = []
        mock_create.side_effect = RuntimeError("DB error")

        tasks = [{"title": "Broken task", "description": "X", "priority": "low"}]
        orch = _build_orchestrator_stub()
        req = _make_request(
            data={"prioritised_tasks": tasks, "user_namespace": "#V#alice"},
        )
        result = orch._action_todo_refresh_summarise(req)

        assert result.ok
        assert len(result.outputs["persisted_tasks"]) == 0
        assert "0 new task(s) created" in result.outputs["todo_summary"]


# ---------------------------------------------------------------------------
# Integration test: full workflow through the engine
# ---------------------------------------------------------------------------


class TestTodoRefreshWorkflowIntegration:
    """Run the full todo_refresh workflow through WorkflowExecutor with mocks."""

    def _build_registry(self, orch) -> ActionRegistry:
        """Register the 5 real handlers in a fresh ActionRegistry."""
        registry = ActionRegistry()
        for action_id, handler in (
            ("todo_refresh.check_cache", orch._action_todo_refresh_check_cache),
            ("todo_refresh.fetch_gmail", orch._action_todo_refresh_fetch_gmail),
            ("todo_refresh.extract_tasks", orch._action_todo_refresh_extract_tasks),
            ("todo_refresh.prioritise", orch._action_todo_refresh_prioritise),
            ("todo_refresh.summarise", orch._action_todo_refresh_summarise),
        ):
            registry.register(ActionSpec(action_id=action_id, handler=handler))
        return registry

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_cache_fresh_short_circuits(self, mock_get_tasks):
        """When tasks are fresh, workflow should skip directly to completed."""
        now = datetime.now(timezone.utc)
        mock_get_tasks.return_value = [
            {"title": "Fresh task", "updated_at": now.isoformat()},
        ]

        orch = _build_orchestrator_stub()
        registry = self._build_registry(orch)
        executor = WorkflowExecutor(registry=registry)

        env = _make_env()
        result = executor.run(
            build_authoritative_test_workflow_definition(TODO_REFRESH_WORKFLOW_ID),
            environment=env,
            data={"user_namespace": "#V#alice"},
        )

        assert result.completed
        assert result.final_state == "completed"
        assert result.data.get("todo_refresh_needed") is False
        # Gateway should NOT have been called (skipped Gmail).
        env.gateway.invoke.assert_not_called()  # type: ignore[union-attr]

    @patch("src.backend.services.task_management_service.create_task")
    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_full_refresh_flow(self, mock_get_tasks, mock_create):
        """Full flow: stale cache -> fetch Gmail -> extract -> prioritise -> summarise."""
        stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
        # check_cache: returns stale tasks.
        # summarise: returns empty for dedup check.
        mock_get_tasks.side_effect = [
            [{"title": "Old", "updated_at": stale_time.isoformat()}],  # check_cache
            [],  # summarise dedup
        ]
        mock_create.side_effect = lambda title, description, **kw: {
            "task_concept_id": "#V#task_test_abc",
            "title": title,
            "priority": kw.get("priority", "medium"),
            "status": "pending",
        }

        # Gateway mocks for Gmail.
        gateway = MagicMock()
        gateway.invoke.side_effect = [
            _FakeGatewayResult(
                payload={"messages": [{"id": "m1", "snippet": "Please review the PR"}]}
            ),
            _FakeGatewayResult(
                payload={
                    "subject": "PR Review",
                    "from": "bob@example.com",
                    "body": "Please review PR #42 by Friday",
                }
            ),
        ]

        # LLM mock for extract_tasks (<=3 tasks so prioritise won't call LLM).
        llm_client = MagicMock()
        llm_client.generate.return_value = json.dumps(
            [
                {
                    "title": "Review PR #42",
                    "description": "Review by Friday",
                    "source_email_index": 1,
                },
            ]
        )

        orch = _build_orchestrator_stub()
        orch._TODO_CACHE_TTL_SECONDS = 3600
        registry = self._build_registry(orch)
        executor = WorkflowExecutor(registry=registry)

        env = _make_env(
            gateway=gateway,
            llm_client=llm_client,
            default_gmail_profile="test@example.com",
        )
        result = executor.run(
            build_authoritative_test_workflow_definition(TODO_REFRESH_WORKFLOW_ID),
            environment=env,
            data={
                "user_namespace": "#V#alice",
                "aux_llm_calls": [],
            },
        )

        assert result.completed
        assert result.final_state == "completed"
        assert len(result.data.get("persisted_tasks", [])) == 1
        assert "1 new task(s) created" in result.data.get("todo_summary", "")
        mock_create.assert_called_once()

    @patch("src.backend.services.task_management_service.get_tasks_for_user")
    def test_empty_inbox_completes_gracefully(self, mock_get_tasks):
        """Stale cache but empty inbox -> no tasks, clean completion."""
        mock_get_tasks.return_value = []  # No existing tasks -> refresh needed.

        gateway = MagicMock()
        gateway.invoke.return_value = _FakeGatewayResult(payload={"messages": []})

        orch = _build_orchestrator_stub()
        registry = self._build_registry(orch)
        executor = WorkflowExecutor(registry=registry)

        env = _make_env(gateway=gateway)
        result = executor.run(
            build_authoritative_test_workflow_definition(TODO_REFRESH_WORKFLOW_ID),
            environment=env,
            data={"user_namespace": "#V#alice"},
        )

        assert result.completed
        assert result.final_state == "completed"
        assert result.data.get("extracted_tasks") == []
        assert "No new tasks" in result.data.get("todo_summary", "")
