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
from src.backend.services.workflow_event_integration_service import (
    current_event_workflow_launch_suppression_reason,
)
from src.backend.workflows import workflow_mcp_tool_actions as workflow_mcp_mod
from src.backend.workflows import workflow_studio_service as studio_mod
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.workflow_mcp_tool_actions import (
    EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REQUESTED_OUTPUT,
    SUPPRESS_EVENT_WORKFLOW_LAUNCHES_INPUT,
    WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
    register_workflow_mcp_tool_actions,
)


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


def test_workflow_mcp_event_launch_control_propagates_without_payload_forwarding() -> (
    None
):
    observations: list[tuple[str, str | None]] = []

    def _handler(*, message: str) -> dict[str, object]:
        observations.append(
            (message, current_event_workflow_launch_suppression_reason())
        )
        return {"success": True, "message": message}

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="synthetic_event_suppression_probe",
            handler=_handler,
            input_schema=Schema(
                required={"message": str},
                optional={},
                allow_unknown=False,
            ),
            output_schema=None,
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

    default_result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "synthetic_event_suppression_probe",
            "tool_arguments": {"message": "default"},
        },
        context={},
        env=WorkflowEnvironment(llm_client=None, gateway=gateway),
    )
    suppressed_result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "synthetic_event_suppression_probe",
            "tool_arguments": {
                "message": "suppressed",
                SUPPRESS_EVENT_WORKFLOW_LAUNCHES_INPUT: True,
            },
            SUPPRESS_EVENT_WORKFLOW_LAUNCHES_INPUT: True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None, gateway=gateway),
    )

    assert default_result.status == "success"
    assert (
        default_result.outputs[EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REQUESTED_OUTPUT]
        is False
    )
    assert suppressed_result.status == "success"
    assert (
        suppressed_result.outputs[EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REQUESTED_OUTPUT]
        is True
    )
    assert observations == [
        ("default", None),
        ("suppressed", "workflow_mcp.invoke_tool_owned_mutation"),
    ]
    assert current_event_workflow_launch_suppression_reason() is None


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


def test_workflow_mcp_action_preserves_advisory_crossing_and_timings() -> None:
    def _slow_read() -> dict[str, object]:
        time.sleep(0.05)
        return {"success": True, "value": "completed_after_advisory"}

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="synthetic_slow_read",
            handler=_slow_read,
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

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={"tool_name": "synthetic_slow_read"},
        context={},
        env=WorkflowEnvironment(llm_client=None, gateway=gateway),
        workflow_id="#V#synthetic_timeout_recovery_workflow",
        workflow_state_id="invoke_slow_read",
    )
    assert result.status == "success"
    assert result.outputs["mcp_result"] == {
        "success": True,
        "value": "completed_after_advisory",
    }
    assert result.outputs["mcp_transport"]["outcome"] == "completed"
    assert result.outputs["mcp_transport"]["advisory_budget_exceeded"] is True
    assert result.outputs["mcp_transport"]["advisory_timeout_sec"] == 0.01
    assert result.outputs["mcp_transport"]["timeout_sec"] is None
    assert result.outputs["mcp_transport"]["timeout_phase"] is None
    assert result.outputs["mcp_transport"]["late_result_policy"] == (
        "discard_from_turn"
    )
    assert result.outputs["mcp_queue_duration_ms"] is not None
    assert result.outputs["mcp_handler_duration_ms"] is not None
    assert result.outputs["mcp_transport_overhead_ms"] is not None


def test_workflow_mcp_bridge_preserves_dispatched_write_timeout_as_unknown() -> None:
    from src.backend.workflows.mcp_tool_bridge import (
        workflow_action_result_from_mcp_payload,
    )

    result = workflow_action_result_from_mcp_payload(
        tool_name="synthetic_slow_write",
        payload={
            "success": False,
            "status": "timed_out",
            "error": "The dispatched write exceeded its hard liveness limit.",
            "error_code": "tool_timeout_outcome_unknown",
            "mutation_outcome": "unknown",
            "retryable": False,
            "outcome_finality": "terminal_for_turn",
        },
        duration_ms=30.0,
        transport_metadata={
            "schema_version": "internal_mcp_transport.v1",
            "outcome": "timed_out",
            "timeout_phase": "handler",
            "late_result_policy": "discard_from_turn",
        },
    )

    assert result.status == "unknown"
    assert result.outputs["mcp_result"]["error_code"] == (
        "tool_timeout_outcome_unknown"
    )
    assert result.outputs["mcp_result"]["mutation_outcome"] == "unknown"
    assert result.outputs["mcp_transport"]["outcome"] == "timed_out"
    assert result.outputs["mcp_transport"]["timeout_phase"] == "handler"


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


def test_workflow_mcp_action_preserves_actor_authority_for_gmail(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.services import mail_profile_resource_vontology_service

    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "resolve_authorised_gmail_profile_for_user",
        lambda **kwargs: {
            "success": kwargs == {
                "user_concept_id": "#V#trusted_user",
                "requested_profile_id": "actor-mail",
            },
            "reason_code": "authorised_mail_profile_resolved",
            "profile_id": "actor-mail",
            "profile_resource_concept_id": "#V#gmail_profile_actor_mail",
        },
    )
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: {
            "actor-mail": gmail_service.GmailProfile(
                profile_id="actor-mail",
                token_path="/nonexistent/actor-mail.json",
            )
        },
    )
    observed: dict[str, object] = {}

    def _list_labels(**kwargs):
        observed.update(kwargs)
        return {"labels": []}

    monkeypatch.setattr(gmail_service, "list_labels", _list_labels)
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = registry.execute(
        WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
        inputs={
            "tool_name": "gmail_list_labels",
            "tool_arguments": {
                "profile": "actor-mail",
                "namespace": "#V#forged_user@forged_org",
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
    assert observed["profile_id"] == "actor-mail"
    assert observed["audit_context"] == {
        "namespace": "#V#trusted_user@trusted_org",
        "source": "internal_mcp_gateway",
        "tool": "gmail_list_labels",
    }


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


def test_workflow_mcp_action_allows_direct_read_with_unresolved_semantic_hint():
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

    result = registry.execute(
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
        "get_supported_durable_workflow_action_ids",
        lambda: (WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,),
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
