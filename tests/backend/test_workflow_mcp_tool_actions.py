from __future__ import annotations

from types import SimpleNamespace

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.subworkflow_actions import register_subworkflow_actions
from src.backend.workflows.workflow_mcp_tool_actions import (
    WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
    register_workflow_mcp_tool_actions,
)
from src.backend.workflows import workflow_mcp_tool_actions as workflow_mcp_mod
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows import workflow_studio_service as studio_mod


class _FakeGateway:
    def __init__(self, *, definitions: dict[str, str], payload_factory) -> None:
        self._definitions = dict(definitions)
        self._payload_factory = payload_factory
        self.invocations: list[tuple[str, dict[str, object]]] = []
        self.enabled = True

    def describe_methods(self):
        return {
            name: {"category": category}
            for name, category in self._definitions.items()
        }

    def get_method_definition(self, method_name: str):
        category = self._definitions.get(method_name)
        if category is None:
            return None
        return SimpleNamespace(category=category)

    def invoke(self, method_name: str, payload=None):
        payload_dict = dict(payload or {})
        self.invocations.append((method_name, payload_dict))
        return SimpleNamespace(
            payload=self._payload_factory(method_name, payload_dict),
            duration_ms=4.0,
        )


class _SchemaAwareGateway(_FakeGateway):
    def __init__(self, *, category: str, input_schema, payload_factory) -> None:
        super().__init__(definitions={"strict_tool": category}, payload_factory=payload_factory)
        self._input_schema = input_schema

    def get_method_definition(self, method_name: str):
        category = self._definitions.get(method_name)
        if category is None:
            return None
        return SimpleNamespace(category=category, input_schema=self._input_schema)


def _always_true(_context):
    return True


def test_workflow_mcp_action_invokes_read_tool_and_maps_structured_output():
    gateway = _FakeGateway(
        definitions={"demo_echo": "read"},
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "echo": payload.get("message"),
            "namespace": payload.get("namespace"),
        },
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)
    definition = WorkflowDefinition(
        workflow_id="#V#workflow_mcp_echo",
        initial_state="fetch",
        states={
            "fetch": WorkflowStateSpec(
                state_id="fetch",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
                        inputs={
                            "tool_name": "demo.echo",
                            "tool_arguments": {
                                "message": {"$context_key": "message"}
                            },
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "result.echo",
                            "context_key": "echo_result",
                        }
                    ]
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
        data={"message": "hello"},
    )

    assert result.completed is True
    assert result.data["echo_result"] == "hello"
    assert gateway.invocations == [
        ("demo_echo", {"message": "hello", "namespace": "#V#tester"})
    ]


def test_workflow_mcp_action_omits_namespace_for_strict_schema_without_namespace():
    strict_schema = SimpleNamespace(required={}, optional={}, allow_unknown=False)
    gateway = _SchemaAwareGateway(
        category="read",
        input_schema=strict_schema,
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "payload": payload,
        },
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "strict_tool",
            "tool_arguments": {"profile": "zhan-gmail"},
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
    )

    assert result.status == "success"
    assert gateway.invocations == [("strict_tool", {"profile": "zhan-gmail"})]


def test_workflow_mcp_action_blocks_write_tool_without_represented_policy():
    gateway = _FakeGateway(
        definitions={"delete_concept": "write"},
        payload_factory=lambda _tool_name, _payload: {"success": True},
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    context: dict[str, object] = {}
    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "delete_concept",
            "tool_arguments": {"concept_id": "#V#unsafe"},
        },
        context=context,
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
        workflow_id="#V#workflow_mcp_write_guardrail",
        workflow_state_id="delete",
        workflow_state_metadata={},
    )

    assert result.status == "failed"
    assert result.error == "workflow_mcp_write_policy_missing:delete_concept"
    assert result.outputs["mutation_guardrail_blocked"] is True
    assert gateway.invocations == []


def test_workflow_mcp_output_can_feed_subworkflow_without_tool_batch_action():
    gateway = _FakeGateway(
        definitions={"message_fetch": "read"},
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "message_id": payload.get("message_id"),
            "subject": "Structured handback",
        },
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)
    registry.register(
        ActionSpec(
            action_id="child.capture",
            handler=lambda request: WorkflowActionResult(
                outputs={
                    "received_message_id": request.data.get("message_id"),
                }
            ),
        )
    )
    child_definition = WorkflowDefinition(
        workflow_id="#V#child_message_workflow",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="child.capture"),),
                terminal=True,
            )
        },
        termination_states=("capture",),
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            child_definition if workflow_id == "#V#child_message_workflow" else None
        ),
    )
    parent = WorkflowDefinition(
        workflow_id="#V#parent_message_workflow",
        initial_state="fetch",
        states={
            "fetch": WorkflowStateSpec(
                state_id="fetch",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
                        inputs={
                            "tool_name": "message.fetch",
                            "tool_arguments": {"message_id": "msg-1"},
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="run_child",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "result.message_id",
                            "context_key": "message_id",
                        }
                    ]
                },
            ),
            "run_child": WorkflowStateSpec(
                state_id="run_child",
                actions=(
                    WorkflowActionInvocation(
                        action_id="workflow_invoke_subworkflow",
                        inputs={
                            "workflow_id": "#V#child_message_workflow",
                            "message_id": {"$context_key": "message_id"},
                            "__parent_workflow_id": "#V#parent_message_workflow",
                            "__parent_state_id": "run_child",
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "result.received_message_id",
                            "context_key": "child_received_message_id",
                        }
                    ]
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )

    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None, gateway=gateway),
        data={},
    )

    assert not registry.has("turn_execution.execute_tool_batch")
    assert result.completed is True
    assert result.data["message_id"] == "msg-1"
    assert result.data["child_received_message_id"] == "msg-1"
    assert gateway.invocations == [("message_fetch", {"message_id": "msg-1"})]


def _patch_workflow_studio_runtime(monkeypatch, *, method_metadata):
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    class _WorkflowRegistry:
        def all_workflow_ids(self):
            return []

    monkeypatch.setattr(
        studio_mod,
        "_load_runtime_definition",
        lambda _workflow_id: (None, "unknown", object()),
    )
    monkeypatch.setattr(
        studio_mod,
        "get_shared_durable_action_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        studio_mod,
        "get_shared_workflow_registry_read_only",
        lambda **_kwargs: _WorkflowRegistry(),
    )
    monkeypatch.setattr(
        workflow_mcp_mod,
        "_default_mcp_method_metadata_by_name",
        lambda: method_metadata,
    )


def test_workflow_studio_accepts_generic_read_mcp_invocation(monkeypatch):
    _patch_workflow_studio_runtime(
        monkeypatch,
        method_metadata={"demo_read": {"category": "read"}},
    )

    result = studio_mod.preview_workflow_authoring_spec(
        "#V#studio_read_mcp_workflow",
        authoring_spec={
            "workflow_id": "#V#studio_read_mcp_workflow",
            "initial_state_key": "fetch",
            "steps": [
                {
                    "state_id": "fetch",
                    "action_id": WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
                    "static_input_bindings": [
                        {"tool_param": "tool_name", "value": "demo_read"},
                        {
                            "tool_param": "tool_arguments",
                            "value": {"query": "status"},
                        },
                    ],
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "result.answer",
                            "context_key": "answer",
                        }
                    ],
                    "next_state": "done",
                },
                {"state_id": "done", "terminal": True},
            ],
        },
    )

    contract_validation = result["preview"]["contract_validation"]
    assert contract_validation["valid"] is True
    assert contract_validation["unsupported_action_ids"] == []
    assert contract_validation["workflow_mcp_tool_invocation_issues"] == []


def test_workflow_studio_rejects_write_mcp_invocation_without_policy(monkeypatch):
    _patch_workflow_studio_runtime(
        monkeypatch,
        method_metadata={"delete_concept": {"category": "write"}},
    )

    result = studio_mod.preview_workflow_authoring_spec(
        "#V#studio_write_mcp_workflow",
        authoring_spec={
            "workflow_id": "#V#studio_write_mcp_workflow",
            "initial_state_key": "delete",
            "steps": [
                {
                    "state_id": "delete",
                    "action_id": WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
                    "static_input_bindings": [
                        {"tool_param": "tool_name", "value": "delete_concept"},
                        {
                            "tool_param": "tool_arguments",
                            "value": {"concept_id": "#V#unsafe"},
                        },
                    ],
                    "next_state": "done",
                },
                {"state_id": "done", "terminal": True},
            ],
        },
    )

    contract_validation = result["preview"]["contract_validation"]
    assert contract_validation["valid"] is False
    assert "workflow_mcp_tool_invocation_invalid" in contract_validation["errors"]
    assert contract_validation["workflow_mcp_tool_invocation_issues"] == [
        {
            "state_id": "delete",
            "action_id": WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
            "tool_name": "delete_concept",
            "resolved_tool_name": "delete_concept",
            "category": "write",
            "risk_class": "destructive",
            "reason_code": "mcp_destructive_write_policy_missing",
        }
    ]
