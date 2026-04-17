"""
Tests for Phase 3 (JVNAUTOSCI-799): Orchestrator Structured Tool Calling Integration.

This test suite validates:
1. Structured calling path works when feature flag enabled
2. Legacy fallback works when feature flag disabled
3. Safety constraints preserved (namespace injection, gmail profile, whitelist)
4. call_id execution tracing works correctly
5. Tool definition conversion from MCP catalog to ToolDefinition format
"""

import json
from types import SimpleNamespace
from typing import Any, List, Mapping, Optional, Sequence, cast
from unittest.mock import MagicMock

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _MissingToolCallDetectorSpec,
    _WorkflowModelPolicyState,
)
from src.backend.languagemodels.structured_tool_calling.types import (
    LLMResponse,
    ToolCall,
    ToolDefinition,
)
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.definitions import TOOL_CALLING_WORKFLOW_ID
from src.backend.workflows.workflow_selector import (
    WorkflowSelection,
    WorkflowSelectionPrompt,
)


class MockLLMClientWithTools:
    """Mock LLM client that supports structured tool calling."""

    def __init__(self, should_use_structured: bool = True):
        self._should_use_structured_value = should_use_structured
        self.generate_called = False
        self.generate_with_tools_called = False

    def _should_use_structured_calling(self) -> bool:
        return self._should_use_structured_value

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> str:
        """Legacy generate method."""
        self.generate_called = True
        return "Legacy response without tools"

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """Structured tool calling method."""
        self.generate_with_tools_called = True
        # Simulate a tool call response
        return LLMResponse(
            text_response="I'll search for that concept",
            tool_calls=[
                ToolCall(
                    tool_name="search_knowledge_base",
                    payload={"query": "test query"},
                    call_id="call_abc123",
                )
            ],
        )


class MockLLMClientLegacyOnly:
    """Mock LLM client that only supports legacy generate()."""

    def __init__(self):
        self.generate_called = False

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> str:
        """Legacy generate method."""
        self.generate_called = True
        # Return JSON tool call format
        return '{"tool": "search_knowledge_base", "payload": {"query": "test query"}}'


@pytest.fixture
def mock_gateway():
    """Create a mock gateway with a sample tool catalog."""
    from unittest.mock import MagicMock
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True

    # Create a simple catalog with one tool
    catalog = {
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base",
            "input_schema": {
                "required": {"query": str},
                "optional": {},
                "allow_unknown": False,
                "description": "Search query parameters",
            },
            "output_schema": None,
            "category": "read",
        }
    }
    gateway.describe_methods.return_value = catalog

    # Mock invoke to return a simple result
    mock_result = MagicMock()
    mock_result.payload = {"results": ["found_concept"]}
    mock_result.duration_ms = 50
    gateway.invoke.return_value = mock_result

    return gateway


@pytest.fixture
def orchestrator(mock_gateway):
    """Create an orchestrator instance with mocked gateway."""
    orch = InternalMCPChatOrchestrator(gateway=mock_gateway)
    original_run_llm_with_fallbacks = orch._run_llm_with_fallbacks
    selector = cast(Any, orch._workflow_selector)

    def _run_llm_with_fallbacks_force_tool_workflow(*args, **kwargs):
        if kwargs.get("stage") == "workflow_dispatch":
            return TOOL_CALLING_WORKFLOW_ID, kwargs.get("default_model"), None
        return original_run_llm_with_fallbacks(*args, **kwargs)

    cast(Any, orch)._run_llm_with_fallbacks = _run_llm_with_fallbacks_force_tool_workflow
    selector.prepare_selection_prompt = lambda *args, **kwargs: WorkflowSelectionPrompt(
        prompt_id="#V#chat_turn_classifier_prompt",
        prompt_text="Select workflow",
        discovered_workflow_ids=(TOOL_CALLING_WORKFLOW_ID,),
        candidate_entries=(
            {
                "concept_id": TOOL_CALLING_WORKFLOW_ID,
                "name": "Tool Calling Workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
        ),
        candidate_list_text=(
            "- #V#tool_calling_workflow: Tool Calling Workflow — General-purpose "
            "tool-calling pipeline."
        ),
        requested_prompt_ids=("#V#chat_turn_classifier_prompt",),
        prompt_provenance={},
        policy_recommendation={},
    )
    selector.resolve_selection = lambda **kwargs: WorkflowSelection(
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        verdict="test_forced_tool_pipeline",
        prompt_id=kwargs.get("prompt_id"),
        prompt_used=kwargs.get("prompt_used"),
        raw_response=str(kwargs.get("response_text") or TOOL_CALLING_WORKFLOW_ID),
        discovered_workflow_ids=(TOOL_CALLING_WORKFLOW_ID,),
        confidence_score=1.0,
        reasoning="Structured-calling tests force the tool-calling workflow.",
        selection_source="selector",
        selection_metadata={"selected_workflow_id": TOOL_CALLING_WORKFLOW_ID},
    )
    selector.resolve_policy_selection = (
        lambda **kwargs: WorkflowSelection(
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            verdict="test_forced_tool_pipeline",
            prompt_id=kwargs.get("prompt_id"),
            prompt_used=kwargs.get("prompt_used"),
            raw_response=TOOL_CALLING_WORKFLOW_ID,
            discovered_workflow_ids=(TOOL_CALLING_WORKFLOW_ID,),
            confidence_score=1.0,
            reasoning="Structured-calling tests force the tool-calling workflow.",
            selection_source="selector",
            selection_metadata={"selected_workflow_id": TOOL_CALLING_WORKFLOW_ID},
        )
    )
    selector.resolve_prompt_unavailable_selection = (
        lambda **kwargs: WorkflowSelection(
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            verdict="test_forced_tool_pipeline",
            prompt_id="#V#chat_turn_classifier_prompt",
            prompt_used=None,
            raw_response=TOOL_CALLING_WORKFLOW_ID,
            discovered_workflow_ids=(TOOL_CALLING_WORKFLOW_ID,),
            confidence_score=1.0,
            reasoning="Structured-calling tests force the tool-calling workflow.",
            selection_source="selector",
            selection_metadata={"selected_workflow_id": TOOL_CALLING_WORKFLOW_ID},
        )
    )
    return orch


def _build_large_method_catalogue(
    *,
    read_count: int,
    write_count: int,
) -> dict[str, dict[str, Any]]:
    """Build a deterministic synthetic method catalogue for cap/filter tests."""

    catalogue: dict[str, dict[str, Any]] = {
        # Include baseline/safety pathways used by candidate fallback logic.
        "search_concepts": {
            "name": "search_concepts",
            "description": "Search concepts",
            "input_schema": {"required": ["query"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "fetch_concept": {
            "name": "fetch_concept",
            "description": "Fetch one concept",
            "input_schema": {"required": ["concept_id"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "concept_exists": {
            "name": "concept_exists",
            "description": "Check concept existence",
            "input_schema": {"required": ["concept_id"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "search_web": {
            "name": "search_web",
            "description": "Search the web",
            "input_schema": {"required": ["query"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "qna_search": {
            "name": "qna_search",
            "description": "Question answering search",
            "input_schema": {"required": ["query"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "extract_url": {
            "name": "extract_url",
            "description": "Extract URL content",
            "input_schema": {"required": ["url"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "resilient_extract_url": {
            "name": "resilient_extract_url",
            "description": "Extract URL content (resilient)",
            "input_schema": {"required": ["url"], "optional": [], "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
    }

    for index in range(read_count):
        tool_name = f"read_tool_{index:03d}"
        catalogue[tool_name] = {
            "name": tool_name,
            "description": f"Read tool {index}",
            "input_schema": {
                "required": ["value"],
                "optional": ["limit"],
                "allow_unknown": True,
            },
            "output_schema": None,
            "category": "read",
        }

    for index in range(write_count):
        tool_name = f"write_tool_{index:03d}"
        catalogue[tool_name] = {
            "name": tool_name,
            "description": f"Write tool {index}",
            "input_schema": {
                "required": ["value"],
                "optional": [],
                "allow_unknown": True,
            },
            "output_schema": None,
            "category": "write",
        }

    return catalogue


def _build_workflow_testing_method_catalogue() -> dict[str, dict[str, Any]]:
    catalogue = _build_large_method_catalogue(read_count=60, write_count=0)
    catalogue.update(
        {
            "workflow_execute": {
                "name": "workflow_execute",
                "description": "Execute a durable workflow",
                "input_schema": {
                    "required": ["workflow_id"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "workflow_get_instance": {
                "name": "workflow_get_instance",
                "description": "Read workflow instance state",
                "input_schema": {
                    "required": ["instance_id"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "experiment_start_run": {
                "name": "experiment_start_run",
                "description": "Create an experiment run",
                "input_schema": {
                    "required": ["experiment_spec_id"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "experiment_execute_target_workflow": {
                "name": "experiment_execute_target_workflow",
                "description": "Execute target workflow for an experiment",
                "input_schema": {
                    "required": ["workflow_id"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "experiment_compute_verdict": {
                "name": "experiment_compute_verdict",
                "description": "Compute experiment verdict",
                "input_schema": {
                    "required": ["run_id"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "testing_prepare_meeting_invitation_spec": {
                "name": "testing_prepare_meeting_invitation_spec",
                "description": "Prepare a meeting invitation experiment spec",
                "input_schema": {
                    "required": ["invitation_text"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "gmail_list_messages": {
                "name": "gmail_list_messages",
                "description": "List Gmail messages",
                "input_schema": {
                    "required": ["profile"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
            "jira_search": {
                "name": "jira_search",
                "description": "Search Jira issues",
                "input_schema": {
                    "required": ["jql"],
                    "optional": [],
                    "allow_unknown": True,
                },
                "output_schema": None,
                "category": "read",
            },
        }
    )
    return catalogue


def test_structured_calling_path_used_when_available(orchestrator):
    """Test that structured calling is used when available and feature flag enabled."""
    llm_client = MockLLMClientWithTools(should_use_structured=True)

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify structured calling was used (for initial prompt)
    assert llm_client.generate_with_tools_called
    # Note: generate() may be called for follow-up after tool execution
    # This is expected behavior - we just want to verify structured calling was used first

    # Verify tool was invoked
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"


def test_structured_tool_pipeline_preserves_answer_first_represented_context_response(
    orchestrator, mock_gateway
):
    class _RepresentedContextLLM:
        def __init__(self) -> None:
            self.generate_called = 0
            self.generate_with_tools_called = 0

        def _should_use_structured_calling(self) -> bool:
            return True

        def generate(self, prompt, context=None, model=None):
            self.generate_called += 1
            return "Michael Witbrock is affiliated with Test Org."

        def generate_with_tools(
            self,
            prompt: str,
            available_tools: List[ToolDefinition],
            context: Optional[Sequence[Mapping[str, Any]]] = None,
            model: Optional[str] = None,
            system_message: Optional[str] = None,
        ) -> LLMResponse:
            self.generate_with_tools_called += 1
            return LLMResponse(
                text_response="I will check the represented knowledge.",
                tool_calls=[
                    ToolCall(
                        tool_name="search_knowledge_base",
                        payload={"query": "Michael Witbrock affiliation"},
                        call_id="call_ctx_1",
                    )
                ],
            )

    mock_result = MagicMock()
    mock_result.payload = {
        "results": [
            {
                "concept_id": "#V#michael_witbrock",
                "text": "Michael Witbrock is affiliated with Test Org.",
            }
        ]
    }
    mock_result.duration_ms = 50
    mock_gateway.invoke.return_value = mock_result

    llm_client = _RepresentedContextLLM()
    result = orchestrator.run(
        prompt="Which organisation is Michael Witbrock affiliated with in the represented knowledge?",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    assert llm_client.generate_with_tools_called == 1
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"
    assert result.response_text == "Michael Witbrock is affiliated with Test Org."
    assert "Execution status:" not in result.response_text


def test_legacy_fallback_when_structured_disabled(orchestrator):
    """Test that legacy path is used when feature flag disabled."""
    llm_client = MockLLMClientWithTools(should_use_structured=False)

    orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify legacy generate was used
    assert llm_client.generate_called
    assert not llm_client.generate_with_tools_called


def test_legacy_fallback_when_structured_unavailable(orchestrator):
    """Test that legacy path works when client doesn't support structured calling."""
    llm_client = MockLLMClientLegacyOnly()

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify legacy generate was used
    assert llm_client.generate_called

    # Verify tool was still invoked (via JSON parsing)
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"


def test_call_id_tracing_in_structured_path(orchestrator):
    """Test that call_id is preserved in tool invocations (JVNAUTOSCI-803)."""
    llm_client = MockLLMClientWithTools(should_use_structured=True)

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify call_id is in invocation record
    assert len(result.tool_invocations) == 1
    assert "call_id" in result.tool_invocations[0]
    assert result.tool_invocations[0]["call_id"] == "call_abc123"


def test_tool_calling_respond_surfaces_tool_evidence_in_outputs(
    orchestrator, mock_gateway
):
    llm_client = MockLLMClientWithTools(should_use_structured=True)
    request = SimpleNamespace(
        action_id="tool_calling.respond",
        environment=WorkflowEnvironment(
            llm_client=llm_client,
            gateway=mock_gateway,
            model="gpt-4",
            user_namespace="#V#test_user",
        ),
        data={
            "prompt": "Find test concept",
            "augmented_context": [],
            "policy_state": _WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=(),
            ),
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "recent_user_prompts": ["Find test concept"],
            "conversation_session_id": "chat-1895",
            "turn_id": "turn-1895",
            "method_catalogue": mock_gateway.describe_methods(),
            "model_for_stage": lambda _stage: "gpt-4",
            "record_llm_call": lambda **_kwargs: None,
            "emit_progress": lambda _info: None,
            "emit_phase_transition": lambda _phase, extra=None: None,
            "check_cancellation": lambda: None,
            "build_parse_error_result": lambda *args, **kwargs: None,
            "build_validation_error_result": lambda *args, **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "invocations": [],
            "tool_messages": [],
            "iteration_count": 0,
            "allowed_write_tools": set(),
        },
        trace=None,
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="respond",
        workflow_state_metadata={},
    )

    result = orchestrator._action_tool_calling_respond(request)

    assert result.ok
    outputs = result.outputs
    invocations = outputs.get("invocations")
    assert isinstance(invocations, list)
    assert len(invocations) == 1
    assert invocations[0]["tool"] == "search_knowledge_base"
    assert outputs.get("iteration_count") == 1

    tool_messages = outputs.get("tool_messages")
    assert isinstance(tool_messages, list)
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["tool"] == "search_knowledge_base"


def test_tool_definition_conversion_accepts_list_schema():
    """Regression: accept list-based required/optional schema summaries."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    gateway.describe_methods.return_value = {
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base",
            "input_schema": {
                "required": ["query"],
                "optional": ["limit"],
                "allow_unknown": True,
                "description": "Search query parameters",
            },
            "output_schema": None,
            "category": "read",
        }
    }

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions()

    assert tool_defs
    schema = tool_defs[0].input_schema
    assert schema.get("required") == ["query"]
    assert schema.get("properties", {}).get("query", {}).get("type") == "string"
    assert schema.get("properties", {}).get("limit", {}).get("type") == "string"
    assert schema.get("additionalProperties") is True


def test_namespace_injection_preserved(orchestrator):
    """Test that namespace injection still works in structured path."""
    llm_client = MockLLMClientWithTools(should_use_structured=True)

    orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify namespace was injected into payload
    invoke_calls = orchestrator._gateway.invoke.call_args_list
    assert invoke_calls
    search_calls = [call for call in invoke_calls if call[0][0] == "search_knowledge_base"]
    assert search_calls
    # The invoke is called with (tool_name, payload) positional args
    payload = search_calls[0][0][1]
    assert isinstance(payload, dict)
    assert payload.get("namespace") == "#V#test_user"


def test_mcp_schema_to_json_schema_conversion(orchestrator):
    """Test Schema to JSON Schema conversion."""
    mcp_schema = {
        "required": {"query": str, "limit": int, "names": list},
        "optional": {"offset": int},
        "allow_unknown": False,
        "description": "Search parameters",
    }

    json_schema = orchestrator._mcp_schema_to_json_schema(mcp_schema)

    assert json_schema["type"] == "object"
    assert "properties" in json_schema
    assert "query" in json_schema["properties"]
    assert "limit" in json_schema["properties"]
    assert "offset" in json_schema["properties"]
    assert json_schema["properties"]["query"]["type"] == "string"
    assert json_schema["properties"]["limit"]["type"] == "integer"
    assert json_schema["properties"]["names"]["type"] == "array"
    assert json_schema["properties"]["names"]["items"] == {}
    assert json_schema["properties"]["offset"]["type"] == "integer"
    assert "required" in json_schema
    assert "query" in json_schema["required"]
    assert "limit" in json_schema["required"]
    assert "names" in json_schema["required"]
    assert "offset" not in json_schema["required"]


def test_tool_definitions_conversion(orchestrator):
    """Test conversion of MCP catalog to ToolDefinition list."""
    tool_defs = orchestrator._convert_mcp_tools_to_structured_definitions()

    assert len(tool_defs) == 1
    assert isinstance(tool_defs[0], ToolDefinition)
    assert tool_defs[0].name == "search_knowledge_base"
    assert tool_defs[0].description == "Search the knowledge base"
    assert "properties" in tool_defs[0].input_schema


def test_tool_definitions_conversion_appends_planner_hints():
    """Tool planner hints should be exposed through structured tool descriptions."""

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    gateway.describe_methods.return_value = {
        "list_papers": {
            "name": "list_papers",
            "description": "List cached papers",
            "input_schema": {
                "required": {},
                "optional": {},
                "allow_unknown": True,
                "description": "No input",
            },
            "output_schema": None,
            "category": "read",
        },
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base",
            "input_schema": {
                "required": {"query": str},
                "optional": {},
                "allow_unknown": True,
                "description": "Search query parameters",
            },
            "output_schema": None,
            "category": "read",
        },
    }

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions()
    by_name = {definition.name: definition.description for definition in tool_defs}

    assert "Inventory only." in by_name["list_papers"]
    assert "represented-knowledge lookup" in by_name["search_knowledge_base"]


def test_structured_calling_with_no_tool_response(orchestrator, mock_gateway):
    """Test that structured calling handles responses without tool calls."""

    class MockLLMClientNoTools:
        def _should_use_structured_calling(self):
            return True

        def generate(self, prompt, context=None, model=None):
            return "Just a text response"

        def generate_with_tools(
            self, prompt, available_tools, context=None, model=None, system_message=None
        ):
            # Return response without tool calls
            return LLMResponse(text_response="Just a text response", tool_calls=[])

    llm_client = MockLLMClientNoTools()

    result = orchestrator.run(
        prompt="What is the weather?",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
    )

    # Should return text response without invoking tools
    assert result.response_text == "Just a text response"
    assert len(result.tool_invocations) == 0


def test_structured_calling_exception_fallback(orchestrator, mock_gateway):
    """Test that exceptions in structured calling fall back to legacy path."""

    class MockLLMClientWithException:
        def _should_use_structured_calling(self):
            return True

        def generate(self, prompt, context=None, model=None):
            return '{"tool": "search_knowledge_base", "payload": {"query": "fallback"}}'

        def generate_with_tools(
            self, prompt, available_tools, context=None, model=None, system_message=None
        ):
            raise RuntimeError("Structured calling failed")

    llm_client = MockLLMClientWithException()

    # Should not raise, should fall back to legacy
    result = orchestrator.run(
        prompt="Find concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Should have invoked tool via legacy path
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"


def test_structured_path_missing_tool_call_recovery_smoke(
    orchestrator,
):
    """Structured no-tool recovery should surface a stable top-level result.

    When structured calling is enabled but the model returns no tool calls while
    promising to use tools, the orchestrator should:
    - invoke the structured planner once
    - avoid crashing the top-level turn
    - preserve structured path tags if recovery telemetry is surfaced

    Detailed missing-tool-call telemetry contracts are asserted in the
    extraction/helper tests. This integration test stays at the surfaced
    orchestrator-result layer.
    """

    class MockLLMClientStructuredMissingToolCall:
        def __init__(self):
            self.generate_called = 0
            self.generate_with_tools_called = 0

        def _should_use_structured_calling(self) -> bool:
            return True

        def generate(self, prompt: str, context=None, model=None):
            # 1) classifier verdict
            # 2) retry response (legacy JSON tool-call format)
            # 3) final response after tool execution
            self.generate_called += 1
            if self.generate_called == 1:
                return "YES"
            if self.generate_called == 2:
                return '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"test query"}}'
            return "Final response"

        def generate_with_tools(
            self,
            prompt: str,
            available_tools: List[ToolDefinition],
            context=None,
            model=None,
            system_message=None,
        ) -> LLMResponse:
            self.generate_with_tools_called += 1
            # Structured path: model promises a tool call but doesn't include one.
            return LLMResponse(
                text_response="I will search the knowledge base now.",
                tool_calls=[],
            )

    llm_client = MockLLMClientStructuredMissingToolCall()

    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    assert llm_client.generate_with_tools_called == 1
    assert isinstance(result.response_text, str)
    assert result.response_text.strip()

    aux_by_type: dict[str, list[Mapping[str, Any]]] = {}
    for entry in result.aux_llm_calls:
        if isinstance(entry, dict) and isinstance(entry.get("type"), str):
            aux_by_type.setdefault(entry["type"], []).append(entry)

    for detection_entry in aux_by_type.get("missing_tool_call_detection", []):
        assert detection_entry["path"] == "structured"
    for classifier_entry in aux_by_type.get("missing_tool_call_classifier", []):
        assert classifier_entry["path"] == "structured"
    for retry_entry in aux_by_type.get("missing_tool_call_retry", []):
        assert retry_entry["path"] == "structured"


def test_structured_candidate_resolver_enforces_provider_cap():
    """Structured planner candidates stay bounded without dragging in the full catalogue."""

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue = _build_large_method_catalogue(read_count=140, write_count=10)
    gateway.describe_methods.return_value = catalogue

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions(
        method_catalogue=catalogue
    )
    resolution = orch._resolve_structured_tool_candidates(
        prompt="Find the latest research updates and compare them.",
        context=[],
        stage="tool_call",
        workflow_action_id="tool_calling.plan",
        provider="openai",
        tool_definitions=tool_defs,
        method_catalogue=catalogue,
        required_prompt_tools=[],
    )

    assert resolution.provider_limit == 128
    assert resolution.effective_cap == 120
    assert len(resolution.candidate_tool_names) <= resolution.effective_cap
    lowered = {name.lower() for name in resolution.candidate_tool_names}
    assert resolution.truncation_applied is False
    assert "search_web" in lowered
    assert "qna_search" in lowered
    assert "read_tool_139" not in lowered
    assert len(resolution.candidate_tool_names) < 20


def test_structured_calling_passes_capped_tool_list_to_llm():
    """Planner LLM call must pass the filtered candidate tool list downstream."""

    class _CapturingLLM:
        def __init__(self):
            self.generate_with_tools_called = 0
            self.available_tool_names: list[str] = []

        def _should_use_structured_calling(self) -> bool:
            return True

        def generate_with_tools(
            self,
            prompt: str,
            available_tools: List[ToolDefinition],
            context: Optional[Sequence[Mapping[str, Any]]] = None,
            model: Optional[str] = None,
            system_message: Optional[str] = None,
        ) -> LLMResponse:
            self.generate_with_tools_called += 1
            self.available_tool_names = [tool.name for tool in available_tools]
            return LLMResponse(text_response="Direct response", tool_calls=[])

        def generate(
            self,
            prompt: str,
            context: Optional[Sequence[Mapping[str, Any]]] = None,
            model: Optional[str] = None,
            ) -> str:
                return "Direct response"

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue = _build_large_method_catalogue(
        read_count=140, write_count=12
    )
    gateway.describe_methods.return_value = catalogue

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    llm_client = _CapturingLLM()
    aux_log: list[Mapping[str, Any]] = []
    llm_response, _, _ = orch._run_llm_with_tools_fallbacks(
        stage="tool_call",
        prompt="Look up the latest updates.",
        context=[],
        tool_definitions=orch._convert_mcp_tools_to_structured_definitions(
            method_catalogue=catalogue
        ),
        default_client=llm_client,
        default_model="gpt-4",
        policy_state=_WorkflowModelPolicyState(
            enabled=False,
            policy=None,
            policy_id=None,
            predicate_id=None,
            errors=(),
        ),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_kwargs: None,
        workflow_action_id="tool_calling.plan",
        method_catalogue=catalogue,
        required_prompt_tools=[],
    )

    assert llm_client.generate_with_tools_called == 1
    assert len(llm_client.available_tool_names) < 20
    assert llm_response.text_response == "Direct response"
    selection_logs = [
        entry
        for entry in aux_log
        if isinstance(entry, Mapping)
        and str(entry.get("type") or "") == "structured_tool_candidates"
    ]
    assert selection_logs
    assert selection_logs[0]["candidate_tool_count"] == len(
        llm_client.available_tool_names
    )
    assert selection_logs[0]["truncation_applied"] is False


def test_structured_candidate_resolver_readds_required_tool_deterministically():
    """Required tools should survive filtering and produce stable ordering."""

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue = _build_large_method_catalogue(read_count=30, write_count=30)
    gateway.describe_methods.return_value = catalogue

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    orch._structured_tool_candidate_cap_override = 5
    tool_defs = orch._convert_mcp_tools_to_structured_definitions(
        method_catalogue=catalogue
    )

    first = orch._resolve_structured_tool_candidates(
        prompt="Please call write_tool_029 now.",
        context=[],
        stage="tool_call",
        workflow_action_id="tool_calling.plan",
        provider="openai",
        tool_definitions=tool_defs,
        method_catalogue=catalogue,
        required_prompt_tools=["write_tool_029"],
    )
    second = orch._resolve_structured_tool_candidates(
        prompt="Please call write_tool_029 now.",
        context=[],
        stage="tool_call",
        workflow_action_id="tool_calling.plan",
        provider="openai",
        tool_definitions=tool_defs,
        method_catalogue=catalogue,
        required_prompt_tools=["write_tool_029"],
    )

    lowered = {name.lower() for name in first.candidate_tool_names}
    assert "write_tool_029" in lowered
    assert not any(
        reason == "cap_truncation" and name.lower() == "write_tool_029"
        for name, reason in first.excluded_tools
    )
    assert second.candidate_tool_names == first.candidate_tool_names


def test_structured_candidate_resolver_prefers_workflow_testing_family_for_planner():
    """Workflow-testing prompts should not drag unrelated read tools into planning."""

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue = _build_workflow_testing_method_catalogue()
    gateway.describe_methods.return_value = catalogue

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions(
        method_catalogue=catalogue
    )
    resolution = orch._resolve_structured_tool_candidates(
        prompt=(
            "Run a meeting-invitation testing workflow, start the experiment, "
            "execute the target workflow, and compute the verdict."
        ),
        context=[],
        stage="tool_call",
        workflow_action_id="tool_calling.plan",
        provider="openai",
        tool_definitions=tool_defs,
        method_catalogue=catalogue,
        required_prompt_tools=[],
    )

    lowered = {name.lower() for name in resolution.candidate_tool_names}
    assert "testing_prepare_meeting_invitation_spec" in lowered
    assert "experiment_start_run" in lowered
    assert "experiment_execute_target_workflow" in lowered
    assert "experiment_compute_verdict" in lowered
    assert "workflow_execute" in lowered
    assert "gmail_list_messages" not in lowered
    assert "jira_search" not in lowered
    assert len(resolution.candidate_tool_names) < 20


def test_structured_candidate_resolver_uses_context_for_entity_relative_kb_hints():
    """Contextual routing guidance should hint KB/relation tools without paper->arxiv bias."""

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue = {
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base",
            "input_schema": {"required": {"query": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "resolve_concept_by_name": {
            "name": "resolve_concept_by_name",
            "description": "Resolve a concept by name",
            "input_schema": {"required": {"name": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "find_relations_with_argument": {
            "name": "find_relations_with_argument",
            "description": "Find relations for a concept argument",
            "input_schema": {
                "required": {"concept_id": str},
                "optional": {},
                "allow_unknown": True,
            },
            "output_schema": None,
            "category": "read",
        },
        "list_papers": {
            "name": "list_papers",
            "description": "List cached papers",
            "input_schema": {"required": {}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
    }
    gateway.describe_methods.return_value = catalogue

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions(
        method_catalogue=catalogue
    )
    resolution = orch._resolve_structured_tool_candidates(
        prompt="What papers of mine do you know about?",
        context=[
            {
                "role": "system",
                "content": (
                    "Expected answer contract for this turn:\n"
                    "- Selector guidance: Treat this as concept/relation retrieval "
                    "for the referenced entity.\n"
                    "- Grounding requirement: Use represented relation evidence."
                ),
            }
        ],
        stage="tool_call",
        workflow_action_id="tool_calling.plan",
        provider="openai",
        tool_definitions=tool_defs,
        method_catalogue=catalogue,
        required_prompt_tools=[],
    )

    lowered = {name.lower() for name in resolution.candidate_tool_names}
    assert "search_knowledge_base" in lowered
    assert "resolve_concept_by_name" in lowered
    assert "find_relations_with_argument" in lowered
    assert "vontology" not in resolution.hinted_families
    assert "arxiv" not in resolution.hinted_families


def test_structured_candidate_resolver_caps_hinted_kb_web_planner_sets():
    """Hybrid KB+web prompts should not drag the full hinted catalogue into planning."""

    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue: dict[str, dict[str, Any]] = {
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search represented knowledge",
            "input_schema": {"required": {"query": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "search_web": {
            "name": "search_web",
            "description": "Search the web",
            "input_schema": {"required": {"query": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
        "fetch_concept": {
            "name": "fetch_concept",
            "description": "Fetch one concept",
            "input_schema": {"required": {"concept_id": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        },
    }
    for index in range(40):
        catalogue[f"search_misc_{index:03d}"] = {
            "name": f"search_misc_{index:03d}",
            "description": "Generic search helper",
            "input_schema": {"required": {"query": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        }
        catalogue[f"rag_misc_{index:03d}"] = {
            "name": f"rag_misc_{index:03d}",
            "description": "Generic KB helper",
            "input_schema": {"required": {"query": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        }
        catalogue[f"concept_misc_{index:03d}"] = {
            "name": f"concept_misc_{index:03d}",
            "description": "Generic Vontology helper",
            "input_schema": {"required": {"query": str}, "optional": {}, "allow_unknown": True},
            "output_schema": None,
            "category": "read",
        }
    for index in range(20):
        catalogue[f"concept_write_{index:03d}"] = {
            "name": f"concept_write_{index:03d}",
            "description": "Generic Vontology mutation helper",
            "input_schema": {
                "required": {"concept_id": str},
                "optional": {},
                "allow_unknown": True,
            },
            "output_schema": None,
            "category": "write",
        }
    gateway.describe_methods.return_value = catalogue

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions(
        method_catalogue=catalogue
    )
    resolution = orch._resolve_structured_tool_candidates(
        prompt=(
            "What open-source projects released recently look most aligned with "
            "the research themes already in my KB?"
        ),
        context=[
            {
                "role": "system",
                "content": (
                    "Selector guidance: first use RAG/Vontology tools to extract "
                    "research themes from the user's KB, then use search_web to "
                    "find recent releases."
                ),
            }
        ],
        stage="tool_call",
        workflow_action_id="tool_calling.respond",
        provider="openai",
        tool_definitions=tool_defs,
        method_catalogue=catalogue,
        required_prompt_tools=[],
    )

    lowered = {name.lower() for name in resolution.candidate_tool_names}
    assert "search_knowledge_base" in lowered
    assert "search_web" in lowered
    assert "fetch_concept" in lowered
    assert not any(name.startswith("concept_write_") for name in lowered)
    assert len(resolution.candidate_tool_names) <= 16
    assert any(
        warning == "family_hint_cap_applied:16" for warning in resolution.warnings
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
