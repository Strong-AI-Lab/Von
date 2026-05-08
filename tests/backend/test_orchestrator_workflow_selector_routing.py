"""Tests for workflow selector routing in the internal MCP orchestrator.

JVNAUTOSCI-922 Phase 1.3 + 2.2: Tests cover:
- Workflow selector fires for any authenticated turn (no presenter-mode gate)
- Narration workflow routing
- Discovered workflow routing
- Voice hint injection (selector disabled)

JVNAUTOSCI-825: Additional tests cover:
- Plain response routing (skips tool-calling overhead)
- WorkflowRoutingInfo presence in results
- Routing timing telemetry
- Selector default-on behaviour

These tests construct a minimal orchestrator stub, bypassing DB-dependent
init to avoid hanging on MongoDB connections.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, cast
from unittest.mock import MagicMock

import pytest

import src.backend.integrations.internal_mcp.orchestrator as orchestrator_module
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    ProgressTracker,
    WorkflowRoutingInfo,
    _ModelCandidate,
)
import src.backend.services.workflow_selection_experience as selection_experience_module
from src.backend.services.paper_representation_workflow_vontology_service import (
    ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
)
from src.backend.services.tool_metadata_service import ToolDispatchSurfaceMetadata
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowRegistration,
    WorkflowStateSpec,
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
)
from src.backend.workflows.turn_expected_outcome_contract import (
    build_turn_expected_outcome_boundary_payload,
)
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_gap_workflow_contracts import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
)
from src.backend.workflows.workflow_selector import WorkflowSelector
from orchestrator_test_harness import build_db_independent_orchestrator


def _build_structured_turn_contract_payload(
    *,
    summary: str | None = None,
    grounding_requirement: str | None = None,
    precision_policy: str | None = None,
    selector_guidance: str | None = None,
    answering_guidance: str | None = None,
    reasoning: str | None = None,
    required_tools: Sequence[str] = (),
) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    for field_name, value in (
        ("summary", summary),
        ("grounding_requirement", grounding_requirement),
        ("precision_policy", precision_policy),
        ("selector_guidance", selector_guidance),
        ("answering_guidance", answering_guidance),
        ("reasoning", reasoning),
    ):
        if isinstance(value, str) and value.strip():
            contract[field_name] = value.strip()
    tools = [
        str(item).strip()
        for item in required_tools
        if isinstance(item, str) and str(item).strip()
    ]
    if tools:
        contract["required_tools"] = tools
    return build_turn_expected_outcome_boundary_payload(contract)


def _dispatch_surface(
    surface_family: str,
    *,
    external_surface: bool = False,
) -> ToolDispatchSurfaceMetadata:
    return ToolDispatchSurfaceMetadata(
        surface_family=surface_family,
        evidence_surface_family=surface_family,
        external_surface=external_surface,
    )


# ---------------------------------------------------------------------------
# Stubs – avoid DB, gateway, and real LLM calls.
# ---------------------------------------------------------------------------


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {
            "renderer_resolve_applicability": {"category": "read"},
        }

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Gateway should not be invoked in this test")


class _InvokeResult:
    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        self.duration_ms = 0.0


class _CapturingLLM:
    """Test double that returns canned responses and records calls."""

    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def _build_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selector_enabled: bool = False,
) -> InternalMCPChatOrchestrator:
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _StubGateway()),
        selector_enabled=selector_enabled,
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )
    return orchestrator


def _stub_execute_workflow_result(
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: InternalMCPChatOrchestrator,
    *,
    expected_workflow_id: str,
    data: Mapping[str, Any],
    final_state: str = "completed",
    completed: bool = True,
    passthrough_unmatched: bool = False,
) -> None:
    """Stub direct workflow execution so routing tests stay focused."""

    original_execute_workflow = orchestrator.execute_workflow

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != expected_workflow_id:
            if passthrough_unmatched:
                return original_execute_workflow(workflow_id, **_kwargs)
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data=dict(data),
            final_state=final_state,
            completed=completed,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)


def _register_terminal_custom_workflow(
    orchestrator: InternalMCPChatOrchestrator,
    *,
    workflow_id: str,
    purpose: str = "Custom workflow for routing tests.",
) -> None:
    """Register a minimal launchable custom workflow for selector tests."""

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_custom"),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=purpose,
            source="test",
        )
    )


# ---------------------------------------------------------------------------
# Voice hint injection (selector OFF — tests augmented context, not routing).
# ---------------------------------------------------------------------------



# Test cases for this harness are split across focused modules named test_orchestrator_*_routing.py.
