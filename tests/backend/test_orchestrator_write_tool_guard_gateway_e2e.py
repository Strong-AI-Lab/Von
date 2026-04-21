"""Gateway-backed write-tool safety tests through InternalMCPGateway.invoke()."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

from orchestrator_test_harness import build_db_independent_orchestrator

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services import settings_service

_TOOL_NAME = "create_dummy_concept"


class _CapturingLLM:
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


def _build_gateway_with_write_tool() -> tuple[InternalMCPGateway, list[dict[str, Any]]]:
    captured_payloads: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()

    def _write_handler(**kwargs):
        captured_payloads.append(dict(kwargs))
        return {
            "success": True,
            "saved": True,
            "echo": kwargs.get("value"),
        }

    catalogue.register(
        MethodDefinition(
            name=_TOOL_NAME,
            handler=_write_handler,
            input_schema=Schema(
                required={"value": str},
                optional={},
                allow_unknown=True,
                description="dummy write payload",
            ),
            output_schema=Schema(
                required={"success": bool, "saved": bool},
                optional={"echo": (str, type(None))},
                allow_unknown=True,
                description="dummy write result",
            ),
            category="write",
            description="Dummy write tool for orchestrator safety tests.",
        )
    )

    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    return gateway, captured_payloads


def test_orchestrator_blocks_write_tool_on_read_only_prompt_with_real_gateway(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )

    gateway, captured_payloads = _build_gateway_with_write_tool()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            f'{{"action":"call_tool","tool":"{_TOOL_NAME}","payload":{{"value":"should-not-write"}}}}',
            '{"schema_version":"write_tool_request_evidence.v1","tool_evidence":[{"tool_name":"create_dummy_concept","request_state":"low_confidence","confirmation_state":"low_confidence","denial_state":"explicit_denial","rationale":"current prompt explicitly says do not create anything"}]}',
            "Understood.",
        ]
    )

    result = orchestrator.run(
        prompt="Search the Vontology for rollout notes; do not create anything.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert captured_payloads == []
    blocked = [
        record
        for record in result.tool_invocations
        if record.get("tool") == _TOOL_NAME
    ]
    assert blocked, "Expected blocked write-tool record"
    assert all(record.get("blocked") for record in blocked)
    assert all(
        record.get("write_policy_blocked_reason") == "explicit_write_denial_detected"
        for record in blocked
    )

    diagnostics = gateway.get_diagnostics()
    assert diagnostics["methods"][_TOOL_NAME]["calls"] == 0


def test_orchestrator_allows_write_tool_when_user_requests_vontology_mutation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )

    gateway, captured_payloads = _build_gateway_with_write_tool()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            f'{{"action":"call_tool","tool":"{_TOOL_NAME}","payload":{{"value":"allowed-write"}}}}',
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Add a concept in the Vontology for rollout controls.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert len(captured_payloads) == 1
    assert captured_payloads[0]["value"] == "allowed-write"
    records = [
        record
        for record in result.tool_invocations
        if record.get("tool") == _TOOL_NAME
    ]
    assert records, "Expected write-tool invocation record"
    assert all(not record.get("blocked", False) for record in records)

    diagnostics = gateway.get_diagnostics()
    method_metrics = diagnostics["methods"][_TOOL_NAME]
    assert method_metrics["calls"] == 1
    assert method_metrics["failures"] == 0
