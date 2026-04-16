from __future__ import annotations

from types import SimpleNamespace

from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.registry_factory import _durable_mcp_fallback_action
from src.backend.workflows.durable.workflow_creation_workflow import (
    _gateway_fallback_action,
)
from src.backend.workflows.write_tool_policy import (
    WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
)
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


class _FakeGateway:
    def __init__(self, *, definitions: dict[str, str], payload_factory=None) -> None:
        self._definitions = dict(definitions)
        self._payload_factory = payload_factory or (lambda tool_name, payload: payload)
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
            duration_ms=3.0,
        )


def _theory_bounded_policy(*, allow_write_tools=None):
    payload: dict[str, object] = {
        "schema_version": WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
        "mode": "theory_bounded",
        "testing_theory_id": "#V#theory_guardrail_test",
    }
    if allow_write_tools:
        payload["allowed_write_tools"] = list(allow_write_tools)
    return payload


def test_durable_fallback_blocks_resolved_write_alias_under_theory_scope(monkeypatch):
    fake_gateway = _FakeGateway(definitions={"jira_create_issue": "write"})
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
        lambda: fake_gateway,
    )

    request = WorkflowActionRequest(
        action_id="jira.create.issue",
        inputs={"summary": "Sandbox escape attempt"},
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=fake_gateway,
            user_namespace="#V#sandbox_ns",
            user_concept_id="#V#tester",
        ),
        data={"workflow_execution_side_effect_policy": _theory_bounded_policy()},
        workflow_id="#V#capability_test_execution_workflow",
        workflow_state_id="execute_candidate",
        workflow_state_metadata={
            "mutation_authority": {
                "schema_version": "workflow_step_mutation_authority.v1",
                "maximum_level": "external_system_guarded",
            }
        },
    )

    result = _durable_mcp_fallback_action(request)

    assert result.status == "failed"
    assert "mutation_guardrail_blocked:jira_create_issue" in (result.error or "")
    assert fake_gateway.invocations == []
    assert result.outputs["mutation_guardrail_blocked"] is True
    events = result.outputs["mutation_guardrail_events"]
    assert len(events) == 1
    assert events[0]["tool_name"] == "jira_create_issue"
    assert events[0]["authority_block_source"] == "execution_scope"


def test_workflow_creation_fallback_blocks_write_tool_under_theory_scope():
    fake_gateway = _FakeGateway(definitions={"workflow_create_schedule": "write"})
    request = WorkflowActionRequest(
        action_id="workflow_create_schedule",
        inputs={"workflow_id": "#V#candidate_workflow", "schedule_type": "once"},
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=fake_gateway,
            user_namespace="#V#sandbox_ns",
            user_concept_id="#V#tester",
        ),
        data={"workflow_execution_side_effect_policy": _theory_bounded_policy()},
        workflow_id="#V#workflow_creation_workflow",
        workflow_state_id="verify_discoverability",
        workflow_state_metadata={
            "mutation_authority": {
                "schema_version": "workflow_step_mutation_authority.v1",
                "maximum_level": "external_system_guarded",
            }
        },
    )

    result = _gateway_fallback_action(request)

    assert result.status == "failed"
    assert "mutation_guardrail_blocked:workflow_create_schedule" in (result.error or "")
    assert fake_gateway.invocations == []
    assert isinstance(request.data.get("mutation_guardrail_events"), list)
    assert request.data["mutation_guardrail_events"][0]["tool_name"] == (
        "workflow_create_schedule"
    )


def test_workflow_creation_fallback_allows_read_tool_in_theory_scope():
    fake_gateway = _FakeGateway(
        definitions={"fetch_concept": "read"},
        payload_factory=lambda tool_name, payload: {
            "success": True,
            "tool_name": tool_name,
            "concept_id": payload.get("concept_id"),
        },
    )
    request = WorkflowActionRequest(
        action_id="fetch_concept",
        inputs={"concept_id": "#V#paper"},
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=fake_gateway,
            user_namespace="#V#sandbox_ns",
            user_concept_id="#V#tester",
        ),
        data={"workflow_execution_side_effect_policy": _theory_bounded_policy()},
        workflow_id="#V#workflow_creation_workflow",
        workflow_state_id="verify_discoverability",
        workflow_state_metadata={},
    )

    result = _gateway_fallback_action(request)

    assert result.status == "success"
    assert fake_gateway.invocations == [
        ("fetch_concept", {"concept_id": "#V#paper", "namespace": "#V#sandbox_ns"})
    ]


def test_durable_fallback_coerces_numeric_like_tool_payload_fields(monkeypatch):
    observed: dict[str, object] = {}

    def _search_handler(
        *, query: str, top_k: int | None = None, namespace: str | None = None
    ) -> dict[str, object]:
        observed["query"] = query
        observed["top_k"] = top_k
        observed["top_k_type"] = type(top_k).__name__
        observed["namespace"] = namespace
        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "namespace": namespace,
        }

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="search_knowledge_base",
            handler=_search_handler,
            input_schema=Schema(
                required={"query": str},
                optional={"top_k": int, "namespace": str},
                allow_unknown=False,
                description="Search KB input.",
            ),
            output_schema=Schema(
                required={"success": bool, "query": str},
                optional={
                    "top_k": (int, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=False,
                description="Search KB output.",
            ),
            category="read",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
        lambda: gateway,
    )

    request = WorkflowActionRequest(
        action_id="search_knowledge_base",
        inputs={"query": "papers of mine", "top_k": "10"},
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#sandbox_ns",
            user_concept_id="#V#tester",
        ),
        data={},
        workflow_id="#V#conversation_turn_execution_workflow",
        workflow_state_id="apply_recovery_tool_batch",
        workflow_state_metadata={},
    )

    result = _durable_mcp_fallback_action(request)

    assert result.status == "success"
    assert observed["query"] == "papers of mine"
    assert observed["top_k"] == 10
    assert observed["top_k_type"] == "int"
    assert observed["namespace"] == "#V#sandbox_ns"
    assert result.outputs["mcp_result"]["top_k"] == 10
