from __future__ import annotations

import time
from types import SimpleNamespace

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
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
            name: {"category": category} for name, category in self._definitions.items()
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
    def __init__(
        self,
        *,
        category: str,
        input_schema,
        payload_factory,
        method_name: str = "strict_tool",
    ) -> None:
        super().__init__(
            definitions={method_name: category}, payload_factory=payload_factory
        )
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
                            "tool_arguments": {"message": {"$context_key": "message"}},
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


def test_workflow_mcp_action_preserves_typed_gateway_schema_failure() -> None:
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={"tool_name": "resolve_concept_by_name"},
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
            user_concept_id="#V#tester",
            org_concept_id="#V#test_org",
        ),
        workflow_id="#V#schema_failure_probe",
        workflow_state_id="invoke_invalid_argument",
    )

    assert result.status == "failed"
    assert result.outputs["mcp_result"] == {
        "success": False,
        "error": "Internal MCP arguments failed schema validation.",
        "error_code": "schema_validation_failed",
        "error_type": "invalid_arguments",
        "retryable": False,
        "validation_stage": "input_schema",
    }
    assert result.outputs["mcp_resolved_tool"] == "resolve_concept_by_name"
    assert result.outputs["workflow_actor_scope_enforced"] is True


def test_workflow_mcp_action_distinguishes_output_schema_failure() -> None:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="broken_output_tool",
            handler=lambda **_kwargs: {"success": True, "value": 42},
            input_schema=Schema(required={}, optional={}, allow_unknown=False),
            output_schema=Schema(
                required={"success": bool, "value": str},
                optional={},
                allow_unknown=False,
            ),
            category="read",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={"tool_name": "broken_output_tool"},
        context={},
        env=WorkflowEnvironment(llm_client=None, gateway=gateway),
    )

    assert result.status == "failed"
    assert result.outputs["mcp_result"] == {
        "success": False,
        "error": "Internal MCP output failed schema validation.",
        "error_code": "output_schema_validation_failed",
        "error_type": "invalid_tool_output",
        "retryable": False,
        "validation_stage": "output_schema",
    }


def test_workflow_mcp_action_preserves_typed_transport_timeout_and_timings(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow_mcp_mod,
        "validate_tool_target_contract",
        lambda **_kwargs: SimpleNamespace(ok=True),
    )
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="synthetic_slow_read",
            handler=lambda: time.sleep(0.15),
            input_schema=Schema(required={}, optional={}, allow_unknown=False),
            output_schema=None,
            category="read",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(
            read_timeout_sec=0.03,
            read_advisory_timeout_sec=0.01,
        ),
        enabled=True,
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    started_at = time.perf_counter()
    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={"tool_name": "synthetic_slow_read"},
        context={},
        env=WorkflowEnvironment(llm_client=None, gateway=gateway),
        workflow_id="#V#synthetic_timeout_recovery_workflow",
        workflow_state_id="invoke_slow_read",
    )
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.15
    assert result.status == "failed"
    assert result.outputs["mcp_result"]["error_code"] == "tool_timeout"
    assert result.outputs["mcp_result"]["retryable"] is True
    assert result.outputs["mcp_transport"]["outcome"] == "timed_out"
    assert result.outputs["mcp_transport"]["timeout_phase"] == "handler"
    assert result.outputs["mcp_transport"]["late_result_policy"] == (
        "discard_from_turn"
    )
    assert result.outputs["mcp_queue_duration_ms"] is not None
    assert result.outputs["mcp_handler_duration_ms"] is None
    assert result.outputs["mcp_transport_overhead_ms"] is not None


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


def test_workflow_mcp_action_strips_explicit_namespace_for_strict_schema():
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
            "tool_arguments": {
                "profile": "zhan-gmail",
                "namespace": "#V#tester",
            },
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


def test_workflow_mcp_action_overwrites_actor_claims_and_binds_ambient_actor():
    observed_ambient: list[tuple[str | None, str | None]] = []

    def _payload_factory(_tool_name, payload):
        from src.backend.security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        observed_ambient.append(
            (
                get_effective_user_concept_id(),
                get_effective_organisation_concept_id(),
            )
        )
        return {"success": True, "payload": payload}

    actor_schema = Schema(
        required={},
        optional={
            "user_id": str,
            "user_concept_id": str,
            "org_id": str,
            "organisation_concept_id": str,
            "namespace": str,
            "user_namespace": str,
        },
        allow_unknown=False,
    )
    gateway = _SchemaAwareGateway(
        category="read",
        input_schema=actor_schema,
        payload_factory=_payload_factory,
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "strict_tool",
            "tool_arguments": {
                "user_id": "#V#forged_user",
                "user_concept_id": "#V#forged_user",
                "org_id": "#V#forged_org",
                "organisation_concept_id": "#V#forged_org",
                "namespace": "#V#forged_user@forged_org",
                "user_namespace": "#V#forged_user@forged_org",
            },
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#trusted_user@trusted_org",
            user_concept_id="#V#trusted_user",
            org_concept_id="#V#trusted_org",
        ),
    )

    assert result.status == "success"
    assert result.outputs["workflow_actor_scope_enforced"] is True
    assert gateway.invocations == [
        (
            "strict_tool",
            {
                "user_id": "#V#trusted_user",
                "user_concept_id": "#V#trusted_user",
                "org_id": "#V#trusted_org",
                "organisation_concept_id": "#V#trusted_org",
                "namespace": "#V#trusted_user@trusted_org",
                "user_namespace": "#V#trusted_user@trusted_org",
            },
        )
    ]
    assert observed_ambient == [("#V#trusted_user", "#V#trusted_org")]


def test_workflow_mcp_action_injects_default_gmail_profile_for_strict_schema():
    strict_schema = Schema(required={"profile": str}, optional={}, allow_unknown=False)
    gateway = _SchemaAwareGateway(
        category="read",
        input_schema=strict_schema,
        method_name="gmail_list_labels",
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
            "tool_name": "gmail_list_labels",
            "tool_arguments": {},
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
            default_gmail_profile="zhan-gmail",
        ),
    )

    assert result.status == "success"
    assert gateway.invocations == [("gmail_list_labels", {"profile": "zhan-gmail"})]


def test_turn_recovery_tool_batch_omits_namespace_for_strict_mcp_fallback(
    monkeypatch,
):
    from src.backend.workflows.durable import registry_factory
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        register_turn_execution_actions,
    )

    strict_schema = SimpleNamespace(
        required={"profile": str},
        optional={},
        allow_unknown=False,
    )

    class _Gateway:
        enabled = True

        def __init__(self) -> None:
            self.invocations: list[tuple[str, dict[str, object]]] = []

        def describe_methods(self):
            return {"gmail_list_labels": {"category": "read"}}

        def get_method_definition(self, method_name: str):
            if method_name != "gmail_list_labels":
                return None
            return SimpleNamespace(category="read", input_schema=strict_schema)

        def invoke(self, method_name: str, payload=None):
            self.invocations.append((method_name, dict(payload or {})))
            return SimpleNamespace(
                payload={"success": True, "labels": []},
                duration_ms=3.0,
            )

    gateway = _Gateway()
    monkeypatch.setattr(
        registry_factory,
        "_get_or_build_durable_mcp_gateway",
        lambda: gateway,
    )
    registry = ActionRegistry()
    register_turn_execution_actions(registry)
    registry.set_fallback_handler(registry_factory._durable_mcp_fallback_action)

    result = registry.execute(
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        inputs={
            "tool_calls": [
                {
                    "tool": "gmail_list_labels",
                    "arguments": {"profile": "zhan-gmail"},
                }
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None, user_namespace="#V#tester"),
    )

    assert result.status == "success"
    assert gateway.invocations == [("gmail_list_labels", {"profile": "zhan-gmail"})]
    assert result.outputs["completion_report"]["status"] == "completed"


def test_turn_recovery_tool_batch_replaces_default_profile_and_strips_unknown_fields(
    monkeypatch,
):
    from src.backend.workflows.durable import registry_factory
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        register_turn_execution_actions,
    )

    strict_schema = Schema(
        required={"profile": str, "message_id": str},
        optional={"format": str},
        allow_unknown=False,
    )

    class _Gateway:
        enabled = True

        def __init__(self) -> None:
            self.invocations: list[tuple[str, dict[str, object]]] = []

        def describe_methods(self):
            return {"gmail_get_message": {"category": "read"}}

        def get_method_definition(self, method_name: str):
            if method_name != "gmail_get_message":
                return None
            return SimpleNamespace(category="read", input_schema=strict_schema)

        def invoke(self, method_name: str, payload=None):
            payload_dict = dict(payload or {})
            self.invocations.append((method_name, payload_dict))
            return SimpleNamespace(
                payload={
                    "success": True,
                    "message_id": payload_dict.get("message_id"),
                    "labelIds": ["INBOX"],
                },
                duration_ms=3.0,
            )

    gateway = _Gateway()
    monkeypatch.setattr(
        registry_factory,
        "_get_or_build_durable_mcp_gateway",
        lambda: gateway,
    )
    registry = ActionRegistry()
    register_turn_execution_actions(registry)
    registry.set_fallback_handler(registry_factory._durable_mcp_fallback_action)

    result = registry.execute(
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        inputs={
            "tool_calls": [
                {
                    "tool": "gmail_get_message",
                    "payload": {
                        "profile": "default",
                        "message_id": "msg-1",
                        "include_labels": True,
                    },
                }
            ]
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
            default_gmail_profile="zhan-gmail",
        ),
    )

    assert result.status == "success"
    assert gateway.invocations == [
        ("gmail_get_message", {"profile": "zhan-gmail", "message_id": "msg-1"})
    ]
    invocation = result.outputs["turn_recovery_tool_batch_execution"][
        "tool_invocations"
    ][0]
    assert invocation["payload"] == {"profile": "zhan-gmail", "message_id": "msg-1"}
    assert {
        (entry.get("field"), entry.get("source"))
        for entry in invocation["payload_bindings"]
    } >= {
        ("profile", "default_gmail_profile_placeholder_replacement"),
        ("include_labels", "removed_for_strict_tool_schema"),
    }


def test_turn_recovery_tool_batch_strips_unknown_fields_from_tolerant_schema(
    monkeypatch,
):
    from src.backend.workflows.durable import registry_factory
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        register_turn_execution_actions,
    )

    tolerant_schema = Schema(
        required={"query": str},
        optional={"match_type": str, "namespace": (str, type(None))},
        allow_unknown=True,
    )
    gateway = _SchemaAwareGateway(
        method_name="verification_lookup",
        category="read",
        input_schema=tolerant_schema,
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "results": [{"item_id": "item-1", "query": payload.get("query")}],
        },
    )
    monkeypatch.setattr(
        registry_factory,
        "_get_or_build_durable_mcp_gateway",
        lambda: gateway,
    )
    registry = ActionRegistry()
    register_turn_execution_actions(registry)
    registry.set_fallback_handler(registry_factory._durable_mcp_fallback_action)

    result = registry.execute(
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        inputs={
            "tool_calls": [
                {
                    "tool": "verification_lookup",
                    "payload": {
                        "query": "synthetic target",
                        "matching_policy": "exact",
                    },
                }
            ]
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester@test_org",
        ),
    )

    assert result.status == "success"
    assert gateway.invocations == [
        (
            "verification_lookup",
            {
                "query": "synthetic target",
                "namespace": "#V#tester@test_org",
            },
        )
    ]
    invocation = result.outputs["turn_recovery_tool_batch_execution"][
        "tool_invocations"
    ][0]
    assert {
        (entry.get("field"), entry.get("source"))
        for entry in invocation["payload_bindings"]
    } >= {("matching_policy", "removed_for_strict_tool_schema")}


def test_turn_recovery_tool_batch_binds_predicate_target_alias_to_contract_type(
    monkeypatch,
):
    from src.backend.workflows.durable import registry_factory
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        register_turn_execution_actions,
    )

    predicate_schema = Schema(
        required={},
        optional={
            "concept_id": (str, type(None)),
            "instance_of": (str, type(None)),
            "argument_index": (str, int, type(None)),
            "relation_kind": (str, type(None)),
            "include_argument_type_counts": (bool, type(None)),
            "include_concept_preview": (bool, type(None)),
            "limit": (int, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=False,
    )
    gateway = _SchemaAwareGateway(
        method_name="get_predicate_incidence",
        category="read",
        input_schema=predicate_schema,
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "mode": "type",
            "instance_of": payload.get("instance_of"),
            "total_predicates": 0,
            "predicates": [],
            "paging": {},
        },
    )
    monkeypatch.setattr(
        registry_factory,
        "_get_or_build_durable_mcp_gateway",
        lambda: gateway,
    )
    registry = ActionRegistry()
    register_turn_execution_actions(registry)
    registry.set_fallback_handler(registry_factory._durable_mcp_fallback_action)

    result = registry.execute(
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        inputs={
            "tool_calls": [
                {
                    "tool": "get_predicate_incidence",
                    "payload": {"target": "#V#scientific_paper"},
                }
            ]
        },
        context={
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract.v1",
                "required_tools": ["get_predicate_incidence"],
                "target_type_ids": ["#V#scientific_paper"],
            }
        },
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
    )

    assert result.status == "success"
    assert gateway.invocations == [
        (
            "get_predicate_incidence",
            {
                "instance_of": "#V#scientific_paper",
                "argument_index": "subject",
                "relation_kind": "binary",
                "include_argument_type_counts": True,
                "include_concept_preview": False,
                "limit": 12,
                "namespace": "#V#tester",
            },
        )
    ]
    invocation = result.outputs["turn_recovery_tool_batch_execution"][
        "tool_invocations"
    ][0]
    assert invocation["status"] == "ok"
    assert invocation["payload"]["instance_of"] == "#V#scientific_paper"
    assert "target" not in invocation["payload"]
    assert {
        (entry.get("field"), entry.get("source"))
        for entry in invocation["payload_bindings"]
        if isinstance(entry, dict)
    } >= {
        ("instance_of", "turn_expected_outcome.target_type_ids_alias"),
        ("argument_index", "tool_metadata_default_payload"),
    }


def test_turn_recovery_tool_batch_blocks_symbolic_target_contract_mismatch(
    monkeypatch,
):
    from src.backend.workflows.durable import registry_factory
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        register_turn_execution_actions,
    )

    gateway = _FakeGateway(
        definitions={"get_predicate_incidence": "read"},
        payload_factory=lambda _tool_name, _payload: {"success": True},
    )
    monkeypatch.setattr(
        registry_factory,
        "_get_or_build_durable_mcp_gateway",
        lambda: gateway,
    )
    registry = ActionRegistry()
    register_turn_execution_actions(registry)
    registry.set_fallback_handler(registry_factory._durable_mcp_fallback_action)

    result = registry.execute(
        TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
        inputs={
            "tool_calls": [
                {
                    "tool": "get_predicate_incidence",
                    "payload": {"concept_id": "#V#michael_witbrock"},
                }
            ]
        },
        context={
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract.v1",
                "required_tools": ["get_predicate_incidence"],
                "target_type_ids": ["#V#scientific_paper"],
            }
        },
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
    )

    assert result.status == "success"
    assert gateway.invocations == []
    invocation = result.outputs["turn_recovery_tool_batch_execution"][
        "tool_invocations"
    ][0]
    assert invocation["status"] == "failed"
    assert invocation["error"] == "target_contract_symbolic_mismatch"
    assert invocation["tool_call_validation_diagnostics"][0]["error_code"] == (
        "target_contract_symbolic_mismatch"
    )


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


def test_workflow_mcp_action_blocks_symbolic_target_contract_mismatch():
    gateway = _FakeGateway(
        definitions={"get_predicate_incidence": "read"},
        payload_factory=lambda _tool_name, _payload: {"success": True},
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "get_predicate_incidence",
            "tool_arguments": {"concept_id": "#V#michael_witbrock"},
        },
        context={
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract.v1",
                "required_tools": ["get_predicate_incidence"],
                "target_type_ids": ["#V#scientific_paper"],
            }
        },
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
    )

    assert result.status == "failed"
    assert result.error == (
        "workflow_mcp_target_contract_validation_failed:"
        "get_predicate_incidence:target_contract_symbolic_mismatch"
    )
    assert result.outputs["target_contract_validation_failed"] is True
    assert result.outputs["tool_call_validation_diagnostics"][0]["error_code"] == (
        "target_contract_symbolic_mismatch"
    )
    assert gateway.invocations == []


def test_workflow_mcp_action_uses_prior_search_result_to_authorise_focal_fetch():
    gateway = _FakeGateway(
        definitions={"fetch_concept": "read"},
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "concept_id": payload.get("concept_id"),
        },
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)
    target_contract_state = {
        "schema_version": "turn_expected_outcome_contract.v1",
        "target_contracts": [
            {
                "kind": "natural_language",
                "binding_kind": "entity",
                "text": "the entity named by the user",
                "resolution_status": "unresolved",
            }
        ],
    }

    blocked_result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "fetch_concept",
            "tool_arguments": {"concept_id": "#V#grounded_candidate"},
        },
        context={"turn_expected_outcome_contract_state": target_contract_state},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
    )

    assert blocked_result.status == "failed"
    assert blocked_result.error == (
        "workflow_mcp_target_contract_validation_failed:"
        "fetch_concept:target_contract_unresolved_for_symbolic_tool"
    )
    assert gateway.invocations == []

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "fetch_concept",
            "tool_arguments": {"concept_id": "#V#grounded_candidate"},
        },
        context={
            "turn_expected_outcome_contract_state": target_contract_state,
            "invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "call_id": "call-search-1",
                    "effective_payload": {
                        "results": [
                            {"concept_id": "#V#grounded_candidate"},
                        ]
                    },
                }
            ],
        },
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
    )

    assert result.status == "success"
    assert result.error is None
    assert gateway.invocations == [
        (
            "fetch_concept",
            {
                "concept_id": "#V#grounded_candidate",
                "namespace": "#V#tester",
            },
        )
    ]


def test_workflow_mcp_action_invokes_gmail_send_with_represented_external_policy():
    from src.backend.workflows.write_tool_policy import (
        WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
        WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION,
    )

    gateway = _FakeGateway(
        definitions={"gmail_send_message": "write"},
        payload_factory=lambda _tool_name, payload: {
            "success": True,
            "id": "gmail-msg-1",
            "message_id": "gmail-msg-1",
            "profile": payload.get("profile"),
            "to": payload.get("to"),
            "subject": payload.get("subject"),
        },
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "gmail_send_message",
            "tool_arguments": {
                "profile": "zhan-gmail",
                "to": "witbrock@gmail.com",
                "subject": "Hi From Von",
                "body_text": "A short authorised test body.",
                "allow_send": True,
            },
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#tester",
        ),
        workflow_id="#V#gmail_send_capability_workflow",
        workflow_state_id="send",
        workflow_state_metadata={
            "mutation_authority": {
                "schema_version": WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION,
                "maximum_level": "external_system_guarded",
            },
            "workflow_execution_side_effect_policy": {
                "schema_version": WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
                "mode": "theory_bounded",
                "testing_theory_id": "#V#gmail_send_capability_test",
                "allowed_write_tools": ["gmail_send_message"],
            },
        },
    )

    assert result.status == "success"
    assert result.outputs["result"]["message_id"] == "gmail-msg-1"
    assert result.outputs["mcp_resolved_tool"] == "gmail_send_message"
    assert gateway.invocations == [
        (
            "gmail_send_message",
            {
                "profile": "zhan-gmail",
                "to": "witbrock@gmail.com",
                "subject": "Hi From Von",
                "body_text": "A short authorised test body.",
                "allow_send": True,
                "namespace": "#V#tester",
            },
        )
    ]


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
        "_load_authoring_runtime_definition",
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
