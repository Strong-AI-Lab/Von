import json
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from workflow_test_support import bootstrap_authoritative_conversation_turn_workflows


class _StubResult:
    def __init__(self, payload, duration_ms=1.0):
        self.payload = payload
        self.duration_ms = duration_ms


class _CapturingGateway:
    enabled = True

    def __init__(self):
        self.invocations = []

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        self.invocations.append({"tool": tool_name, "payload": payload})
        return _StubResult({"ok": True})


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, prompt, *, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if self._responses:
            return self._responses.pop(0)
        return "ok"


class _ResolutionGateway:
    enabled = True

    def __init__(
        self,
        *,
        resolve_sequence: list[dict[str, Any]] | None = None,
    ) -> None:
        self.invocations: list[dict[str, Any]] = []
        self.add_relationship_payloads: list[dict[str, Any]] = []
        self._resolve_sequence = list(resolve_sequence or [])

    def describe_methods(self):
        return {
            "add_relationship": {"category": "write"},
            "concept_exists": {"category": "read"},
            "resolve_concept_by_name": {"category": "read"},
        }

    def invoke(self, tool_name, payload=None):
        payload_copy = dict(payload or {})
        self.invocations.append({"tool": tool_name, "payload": payload_copy})

        if tool_name == "concept_exists":
            concept_id = payload_copy.get("concept_id")
            exists = concept_id in {
                "#V#phd_supervision_gal_gendron_university_of_auckland",
                "#V#has_co_supervisor",
                "#V#gillian_dobbie",
                "#V#date_of_event",
                "#V#2026_02_24",
            }
            return _StubResult(
                {
                    "success": True,
                    "concept_id": concept_id,
                    "exists": exists,
                    "accessible": True,
                }
            )

        if tool_name == "resolve_concept_by_name":
            if self._resolve_sequence:
                return _StubResult(self._resolve_sequence.pop(0))
            return _StubResult(
                {
                    "success": True,
                    "status": "resolved",
                    "resolved_concept_id": "#V#gillian_dobbie",
                }
            )

        if tool_name == "add_relationship":
            self.add_relationship_payloads.append(payload_copy)
            if (
                payload_copy.get("source_id")
                == "phd supervision gal gendron university of auckland"
            ):
                return _StubResult(
                    {
                        "success": False,
                        "error": "source_concept_not_found",
                        "error_code": "source_concept_not_found",
                        "error_details": {
                            "source_id": payload_copy.get("source_id"),
                            "predicate": payload_copy.get("predicate"),
                            "target": payload_copy.get("target"),
                            "details": {
                                "success": False,
                                "error": "source_concept_not_found",
                                "concept_id": "phd supervision gal gendron university of auckland",
                            },
                        },
                    }
                )
            if payload_copy.get("predicate") == "has co supervisor":
                return _StubResult(
                    {
                        "success": False,
                        "error": "predicate_concept_not_found",
                        "error_code": "predicate_concept_not_found",
                        "error_details": {
                            "source_id": payload_copy.get("source_id"),
                            "predicate": payload_copy.get("predicate"),
                            "target": payload_copy.get("target"),
                            "details": {
                                "success": False,
                                "error": "predicate_concept_not_found",
                                "concept_id": "has co supervisor",
                            },
                        },
                    }
                )
            if payload_copy.get("target") == "#V#gill_dobbie":
                return _StubResult(
                    {
                        "success": False,
                        "error": "target_not_found",
                        "error_code": "target_not_found",
                        "error_details": {
                            "source_id": payload_copy.get("source_id"),
                            "predicate": payload_copy.get("predicate"),
                            "target": payload_copy.get("target"),
                            "details": {
                                "success": False,
                                "error": "target_not_found",
                                "concept_id": "#V#gill_dobbie",
                            },
                        },
                    }
                )
            if payload_copy.get("target") == "2026-02-24":
                return _StubResult(
                    {
                        "success": False,
                        "error": "target_not_found",
                        "error_code": "target_not_found",
                        "error_details": {
                            "source_id": payload_copy.get("source_id"),
                            "predicate": payload_copy.get("predicate"),
                            "target": payload_copy.get("target"),
                            "details": {
                                "success": False,
                                "error": "target_not_found",
                                "concept_id": "2026-02-24",
                            },
                        },
                    }
                )
            return _StubResult({"success": True, "target": payload_copy.get("target")})

        return _StubResult({"ok": True})


def _bootstrap_authoritative_workflows() -> None:
    report = bootstrap_authoritative_conversation_turn_workflows()
    assert not report.get("graph_publication_errors")


def test_orchestrator_executes_all_tool_calls_in_single_list_response():
    """Regression test for JVNAUTOSCI-941.

    Previously, if the model emitted a list of >4 tool calls, the orchestrator
    executed only the first batch (cap=4) and then asked for a final answer.
    The model could respond with a summary that *assumed* all calls ran.

    We now execute the remaining tool calls from the same list before prompting
    for a final answer.
    """
    _bootstrap_authoritative_workflows()

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=20,
        max_context_chars=80_000,
    )

    tool_calls = [
        {"action": "call_tool", "tool": "dummy", "payload": {"i": i}} for i in range(9)
    ]

    llm = _CapturingLLM([json.dumps(tool_calls), "done"])

    result = orchestrator.run(
        prompt="link authors", context=[], llm_client=llm, model=None
    )

    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
    assert len(result.tool_invocations) == 9
    assert [inv.get("payload", {}).get("i") for inv in result.tool_invocations] == list(
        range(9)
    )
    assert len(gateway.invocations) == 9
    # The workflow-first tool pipeline may add an internal validation pass, but
    # it must not require the model to re-emit the executed tool batch.
    assert len(llm.calls) >= 2


def test_write_preflight_resolves_missing_concept_id_before_add_relationship():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _ResolutionGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=4,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "add_relationship",
                    "payload": {
                        "source_id": "#V#phd_supervision_gal_gendron_university_of_auckland",
                        "predicate": "#V#has_co_supervisor",
                        "target": "#V#gill_dobbie",
                    },
                }
            ),
            "done",
        ]
    )

    result = orchestrator.run(
        prompt="Attach co-supervisor", context=[], llm_client=llm, model=None
    )

    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
    assert len(gateway.add_relationship_payloads) == 1
    assert gateway.add_relationship_payloads[0]["target"] == "#V#gillian_dobbie"
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0].get("payload", {}).get("target") == "#V#gillian_dobbie"


def test_add_relationship_target_not_found_retries_once_with_resolved_target():
    _bootstrap_authoritative_workflows()
    gateway = cast(
        Any,
        _ResolutionGateway(
            resolve_sequence=[
                {"success": True, "status": "not_found"},
                {"success": True, "status": "not_found"},
                {
                    "success": True,
                    "status": "resolved",
                    "resolved_concept_id": "#V#gillian_dobbie",
                },
            ]
        ),
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=4,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "add_relationship",
                    "payload": {
                        "source_id": "#V#phd_supervision_gal_gendron_university_of_auckland",
                        "predicate": "#V#has_co_supervisor",
                        "target": "#V#gill_dobbie",
                    },
                }
            ),
            "done",
        ]
    )

    result = orchestrator.run(
        prompt="Attach co-supervisor", context=[], llm_client=llm, model=None
    )

    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
    assert len(gateway.add_relationship_payloads) == 2
    assert gateway.add_relationship_payloads[0]["target"] == "#V#gill_dobbie"
    assert gateway.add_relationship_payloads[1]["target"] == "#V#gillian_dobbie"
    assert len(result.tool_invocations) == 1
    auto_retry = result.tool_invocations[0].get("auto_retry")
    assert isinstance(auto_retry, dict)
    assert auto_retry.get("reason") == "target_not_found"
    assert auto_retry.get("to") == "#V#gillian_dobbie"


def test_add_relationship_target_not_found_literal_date_retries_with_canonical_target():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _ResolutionGateway(resolve_sequence=[{"success": True, "status": "not_found"}]))
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=4,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "add_relationship",
                    "payload": {
                        "source_id": "#V#phd_supervision_gal_gendron_university_of_auckland",
                        "predicate": "#V#date_of_event",
                        "target": "2026-02-24",
                    },
                }
            ),
            "done",
            "done",
        ]
    )

    result = orchestrator.run(
        prompt="Link meeting date", context=[], llm_client=llm, model=None
    )

    assert len(gateway.add_relationship_payloads) == 2
    assert gateway.add_relationship_payloads[0]["target"] == "2026-02-24"
    assert gateway.add_relationship_payloads[1]["target"] == "#V#2026_02_24"
    assert len(result.tool_invocations) == 1
    auto_retry = result.tool_invocations[0].get("auto_retry")
    assert isinstance(auto_retry, dict)
    assert auto_retry.get("reason") == "target_not_found"
    assert auto_retry.get("strategy") == "canonicalised_literal_value"
    assert auto_retry.get("to") == "#V#2026_02_24"


def test_add_relationship_predicate_not_found_literal_retries_with_canonical_predicate():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _ResolutionGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=4,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "add_relationship",
                    "payload": {
                        "source_id": "#V#phd_supervision_gal_gendron_university_of_auckland",
                        "predicate": "has co supervisor",
                        "target": "#V#gillian_dobbie",
                    },
                }
            ),
            "done",
            "done",
        ]
    )

    result = orchestrator.run(
        prompt="Attach co-supervisor", context=[], llm_client=llm, model=None
    )

    assert len(gateway.add_relationship_payloads) == 2
    assert gateway.add_relationship_payloads[0]["predicate"] == "has co supervisor"
    assert gateway.add_relationship_payloads[1]["predicate"] == "#V#has_co_supervisor"
    assert len(result.tool_invocations) == 1
    auto_retry = result.tool_invocations[0].get("auto_retry")
    assert isinstance(auto_retry, dict)
    assert auto_retry.get("reason") == "predicate_concept_not_found"
    assert auto_retry.get("field") == "predicate"
    assert auto_retry.get("strategy") == "canonicalised_literal_value"
    assert auto_retry.get("to") == "#V#has_co_supervisor"


def test_add_relationship_source_not_found_literal_retries_with_canonical_source():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _ResolutionGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=4,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "add_relationship",
                    "payload": {
                        "source_id": "phd supervision gal gendron university of auckland",
                        "predicate": "#V#has_co_supervisor",
                        "target": "#V#gillian_dobbie",
                    },
                }
            ),
            "done",
            "done",
        ]
    )

    result = orchestrator.run(
        prompt="Attach co-supervisor", context=[], llm_client=llm, model=None
    )

    assert len(gateway.add_relationship_payloads) == 2
    assert (
        gateway.add_relationship_payloads[0]["source_id"]
        == "phd supervision gal gendron university of auckland"
    )
    assert (
        gateway.add_relationship_payloads[1]["source_id"]
        == "#V#phd_supervision_gal_gendron_university_of_auckland"
    )
    assert len(result.tool_invocations) == 1
    auto_retry = result.tool_invocations[0].get("auto_retry")
    assert isinstance(auto_retry, dict)
    assert auto_retry.get("reason") == "source_concept_not_found"
    assert auto_retry.get("field") == "source_id"
    assert auto_retry.get("strategy") == "canonicalised_literal_value"


