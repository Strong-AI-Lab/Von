from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from flask import Flask

from orchestrator_test_harness import (
    _stub_stage_model_snapshot,
    _stub_stage_path,
    build_db_independent_orchestrator,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)

SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID = (
    "#V#scholarly_paper_representation_workflow"
)
_TEST_BASE_PROMPT = (
    "You have access to internal MCP tools.\n\n"
    "{auth_status}\n"
    "Available tools:\n"
    "{listing}"
)


def _build_discovery_result(
    *,
    prompt_text: str,
    workflow_id: str,
    name: str,
    description: str,
    role: str = "execution",
) -> dict[str, Any]:
    entry = {
        "concept_id": workflow_id,
        "name": name,
        "description": description,
        "is_executable": True,
        "executability_reason": "executable_now",
        "is_policy_safe": True,
        "routing_eligible": True,
        "routing_profile": {"role": role},
    }
    return {
        "query": prompt_text,
        "requested_query": prompt_text,
        "search_sources": ["capability_index"],
        "candidate_count": 1,
        "match_count": 1,
        "matches": [dict(entry)],
        "candidates": [dict(entry)],
        "routing_matches": [dict(entry)],
    }


def _tool_calling_discovery(prompt_text: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _build_discovery_result(
        prompt_text=prompt_text,
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        name="Tool Calling Workflow",
        description="General conversational tool workflow.",
    )


def _paper_representation_discovery(
    prompt_text: str,
    *_args: Any,
    **_kwargs: Any,
) -> dict[str, Any]:
    return _build_discovery_result(
        prompt_text=prompt_text,
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        name="Scholarly Paper Representation Workflow",
        description=(
            "Canonical durable workflow for representing scholarly papers from "
            "file-copy artefacts, metadata, and verification requirements."
        ),
    )


class _IdentityLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer from grounded identity context only.",'
                '"grounding_requirement":"Only state identity details grounded in the authenticated context.",'
                '"precision_policy":"Prefer explicit uncertainty over speculation.",'
                '"selector_guidance":"Prefer retrieval or verification only when grounded identity context is insufficient.",'
                '"answering_guidance":"If grounded identity context is available, answer directly and concisely.",'
                '"reasoning":"Authenticated identity questions should be answered from grounded actor or organisation context."}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#chat_assistant_workflow",'
                '"confidence":0.91,'
                '"reasoning":"The authenticated identity request is a grounded direct-response turn."}'
            )
        if isinstance(prompt, str) and "organisation" in prompt.lower():
            return "You are in Test Org (#V#test_org)."
        return "You are Test User (#V#test_user)."


class _AuthorshipLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer only with papers that can be grounded to the user.",'
                '"grounding_requirement":"Only mention papers when authorship or ownership is grounded.",'
                '"precision_policy":"Prefer omission or explicit uncertainty over speculative recall.",'
                '"selector_guidance":"Prefer grounded retrieval or verification only when the current context is insufficient.",'
                '"answering_guidance":"List only grounded papers and say clearly when the available context is incomplete.",'
                '"reasoning":"Ownership-style paper questions are precision-sensitive and should not include unsupported papers."}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#chat_assistant_workflow",'
                '"confidence":0.88,'
                '"reasoning":"This can be answered directly from the currently accessible grounded context if the answer stays precise."}'
            )
        return (
            "I can only confirm papers that are grounded in the current context. "
            "From what I can verify here, the available context is incomplete, and "
            "I would rather say that clearly than guess."
        )


class _GatewayStub:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


class _EntityLookupGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "test.lookup_current_user_papers": {
                "description": "Return grounded paper records for the current user.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_concept_id": {"type": "string"},
                    },
                },
            }
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name != "test.lookup_current_user_papers":
            raise AssertionError(f"Unexpected tool call: {tool_name}")
        return SimpleNamespace(
            payload={
                "success": True,
                "papers": [
                    {
                        "concept_id": "#V#paper_test_1",
                        "title": "Test Paper",
                    }
                ],
                "response_text": "Grounded paper retrieved for Test User.",
            },
            duration_ms=5,
        )


class _AffiliationLookupGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "test.lookup_entity_affiliation": {
                "description": "Return grounded affiliation facts for a named entity.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entity_name": {"type": "string"},
                    },
                },
            }
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name != "test.lookup_entity_affiliation":
            raise AssertionError(f"Unexpected tool call: {tool_name}")
        return SimpleNamespace(
            payload={
                "success": True,
                "entity_name": "Michael Witbrock",
                "organisation": "Test Org",
                "response_text": "Michael Witbrock is affiliated with Test Org.",
            },
            duration_ms=5,
        )


class _EntityRelativeToolPipelineLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer using grounded represented facts about the authenticated user.",'
                '"grounding_requirement":"Use represented user-linked evidence before claiming authorship or ownership.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported attribution.",'
                '"selector_guidance":"Prefer tool-based concept or relation retrieval when grounded user-linked facts are not already explicit in context.",'
                '"answering_guidance":"Retrieve grounded user-linked facts before answering, and state clearly when no grounded facts are found.",'
                '"reasoning":"Entity-relative KB lookup turns should retrieve represented relations instead of asking the user for identifiers when authenticated context exists."}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.93,'
                '"reasoning":"This is an entity-relative KB lookup that should execute grounded retrieval before answering."}'
            )
        if prompt == "What papers of mine do you know about?":
            if (
                "Expected answer contract for this turn" in context_text
                and "CURRENT USER CONTEXT: Test User (#V#test_user)" in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"test.lookup_current_user_papers",'
                    '"payload":{"user_concept_id":"#V#test_user"}}'
                )
            return "I don’t have enough information to identify any of your papers yet."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if "Expected answer contract for this turn" not in context_text:
                return "I don’t have enough information to identify any of your papers yet."
            return "I know about one grounded paper for you: Test Paper."
        return "I know about one grounded paper for you: Test Paper."


class _ExplicitEntityRelationLookupLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer with grounded represented facts about the named entity.",'
                '"grounding_requirement":"Only state relationships that are grounded in represented facts or retrieved evidence.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported relationship claims.",'
                '"selector_guidance":"Prefer concept, relation, or tool-based retrieval over generic chat when represented lookup is required.",'
                '"answering_guidance":"Retrieve the grounded relationship before answering, and say clearly when no grounded relation is found.",'
                '"reasoning":"Explicit entity-relation lookup turns should retrieve represented facts instead of answering from unsupported recall."}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.94,'
                '"reasoning":"This is a grounded represented-knowledge lookup that should retrieve the relation before answering."}'
            )
        if (
            prompt
            == "Which organisation is Michael Witbrock affiliated with in the represented knowledge?"
        ):
            if "Expected answer contract for this turn" in context_text:
                return (
                    '{"action":"call_tool","tool":"test.lookup_entity_affiliation",'
                    '"payload":{"entity_name":"Michael Witbrock"}}'
                )
            return "I don’t have enough grounded information yet."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if "Expected answer contract for this turn" not in context_text:
                return "I don’t have enough grounded information yet."
            return "Michael Witbrock is affiliated with Test Org."
        return "Michael Witbrock is affiliated with Test Org."


def _make_app(
    monkeypatch,
    *,
    llm: (
        _IdentityLLM
        | _AuthorshipLLM
        | _EntityRelativeToolPipelineLLM
        | _ExplicitEntityRelationLookupLLM
    ),
    gateway_override: Any | None = None,
    discovery_override: Any | None = None,
) -> Flask:
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        registry_factory, "_launch_deferred_registry_work", lambda **_kwargs: None
    )

    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_path",
        _stub_stage_path,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id: {
            "organisation_id": "#V#test_org",
            "chat_session_id": "window-session",
            "role": "member",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        discovery_override or (lambda prompt_text, *_args, **_kwargs: _build_discovery_result(
            prompt_text=prompt_text,
            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
            name="Chat Assistant Workflow",
            description="General conversational workflow.",
        )),
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        lambda concept_id: {
            "#V#test_user": {"name": "Test User"},
            "#V#test_org": {"name": "Test Org"},
        }.get(concept_id),
    )
    gateway = gateway_override if gateway_override is not None else _GatewayStub()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        selector_enabled=True,
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (
            _TEST_BASE_PROMPT,
            "#V#test_base_prompt",
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.register_blueprint(von_bp, url_prefix="/von")
    return app


def test_generate_authenticated_identity_turn_uses_direct_response_and_records_plain_response_telemetry(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "You are Test User (#V#test_user)."

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("verdict") in {"rag_selected", "rag_default"}
    assert workflow_routing.get("source") == "selector"

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    workflow_routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    dispatch = workflow_routing_diagnostics.get("dispatch") or {}
    assert dispatch.get("selected_execution_mode") == "direct_response"
    assert dispatch.get("dispatch_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert dispatch.get("dispatch_terminal_failure_reason") in {None, ""}

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert completion_report.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert completion_report.get("response_text") == "You are Test User (#V#test_user)."
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    assert selected_workflow_trace.get("child_workflow_final_state") == "plain_response"

    assert len(llm.calls) >= 2


def test_generate_authenticated_organisation_turn_uses_direct_response_and_preserves_org_context_telemetry(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "Which organisation am I in?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "You are in Test Org (#V#test_org)."

    llm_debug = body.get("llm_debug") or {}
    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    workflow_routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    dispatch = workflow_routing_diagnostics.get("dispatch") or {}
    assert dispatch.get("selected_execution_mode") == "direct_response"
    assert dispatch.get("dispatch_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert (
        completion_report.get("response_text") == "You are in Test Org (#V#test_org)."
    )
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    assert selected_workflow_trace.get("child_workflow_final_state") == "plain_response"

    context_text = "\n".join(
        str(message.get("content") or "")
        for call in llm.calls
        for message in (call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "CURRENT ORGANISATION CONTEXT: Test Org (#V#test_org)" in context_text


def test_generate_authorship_turn_prefers_grounded_omission_over_unsupported_paper_inclusion(
    monkeypatch,
) -> None:
    llm = _AuthorshipLLM()
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda prompt_text, *_args, **_kwargs: {
            "query": prompt_text,
            "requested_query": prompt_text,
            "search_sources": ["capability_index"],
            "candidate_count": 1,
            "match_count": 1,
            "matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
            "candidates": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
            "routing_matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
        },
        raising=False,
    )
    app = _make_app(monkeypatch, llm=llm)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [
            {"role": "user", "content": "Check my papers"},
            {"role": "assistant", "content": "I need more grounded context first."},
        ],
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Titans" not in response_text
    assert "incomplete" in response_text.lower()

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("source") == "selector"

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    stage_model = diagnostics.get("workflow_stage_model") or {}
    stage_entries = stage_model.get("stages") or []
    stage_by_id = {
        str(entry.get("stage_id")): entry
        for entry in stage_entries
        if isinstance(entry, dict) and entry.get("stage_id")
    }
    assert "expected_outcome_inference" in stage_by_id
    assert "selector_preparation" in stage_by_id
    assert "selector_decision" in stage_by_id
    stage_diagnostics = diagnostics.get("stage_diagnostics") or []
    if stage_diagnostics:
        stage_diagnostic_ids = {
            str(entry.get("stage_id"))
            for entry in stage_diagnostics
            if isinstance(entry, dict) and entry.get("stage_id")
        }
        assert "expected_outcome_inference" in stage_diagnostic_ids
        assert "selector_preparation" in stage_diagnostic_ids
        assert "selector_decision" in stage_diagnostic_ids
    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    direct_response_context_lineage = (
        selected_workflow_trace.get("direct_response_context_lineage") or {}
    )
    assert direct_response_context_lineage.get("base_context_source") == (
        "augmented_context"
    )
    assert (direct_response_context_lineage.get("stage_added_message_count") or 0) >= 1
    assert any(
        "Current turn request" in str(message.get("content_preview") or "")
        and "What papers of mine do you know about?"
        in str(message.get("content_preview") or "")
        for message in (
            direct_response_context_lineage.get("stage_added_messages") or []
        )
        if isinstance(message, dict)
    )
    assert any(
        "Expected answer contract for this turn"
        in str(message.get("content_preview") or "")
        for message in (
            direct_response_context_lineage.get("stage_added_messages") or []
        )
        if isinstance(message, dict)
    )
    direct_response_call = next(
        call
        for call in llm.calls
        if call.get("prompt") == "What papers of mine do you know about?"
    )
    direct_response_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (direct_response_call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "Check my papers" in direct_response_context_text
    assert "Current turn request" in direct_response_context_text
    assert "What papers of mine do you know about?" in direct_response_context_text
    expected_outcome_contract = (
        selected_workflow_trace.get("expected_outcome_contract") or {}
    )
    assert expected_outcome_contract.get("precision_policy") == (
        "Prefer omission or explicit uncertainty over speculative recall."
    )


def test_generate_entity_relative_tool_pipeline_threads_expected_contract_into_tool_planning(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Test Paper" in response_text

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    lookup_records = [
        record
        for record in tool_invocations
        if isinstance(record, dict)
        and (record.get("tool") or record.get("method"))
        == "test.lookup_current_user_papers"
    ]
    assert lookup_records

    assert gateway.invocations

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"

    tool_plan_call = next(
        call
        for call in llm.calls
        if call.get("prompt") == "What papers of mine do you know about?"
    )
    tool_plan_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (tool_plan_call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "Expected answer contract for this turn" in tool_plan_context_text
    assert "CURRENT USER CONTEXT: Test User (#V#test_user)" in tool_plan_context_text


def test_generate_entity_relative_lookup_excludes_non_launchable_representation_workflow(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_paper_representation_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Test Paper" in response_text
    assert gateway.invocations

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    assert any(
        isinstance(record, dict)
        and (record.get("tool") or record.get("method"))
        == "test.lookup_current_user_papers"
        for record in tool_invocations
    )

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    discovery = routing_diagnostics.get("discovery") or {}
    assert SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID in (
        discovery.get("candidate_ids") or []
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"
    assert SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID not in (
        selected_workflow_trace.get("selector_candidate_ids") or []
    )
    assert SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID in (
        selected_workflow_trace.get("excluded_candidate_ids") or []
    )


def test_generate_explicit_entity_relation_lookup_uses_grounded_tool_pipeline(
    monkeypatch,
) -> None:
    llm = _ExplicitEntityRelationLookupLLM()
    gateway = _AffiliationLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": (
                "Which organisation is Michael Witbrock affiliated with in the "
                "represented knowledge?"
            )
        },
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "Michael Witbrock is affiliated with Test Org."

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    assert any(
        isinstance(record, dict)
        and (record.get("tool") or record.get("method"))
        == "test.lookup_entity_affiliation"
        for record in tool_invocations
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"


def test_generate_threads_window_session_header_into_conversation_session_resolution(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    captured: dict[str, Any] = {}

    def _capture_generate_session(**kwargs: Any) -> tuple[str, str | None, bool]:
        captured.update(kwargs)
        return "window-session-generated", None, False

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._ensure_generate_conversation_session",
        _capture_generate_session,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        headers={"X-Von-Window-Session": "ws-browser-1910"},
        json={"prompt": "Who am I?"},
    )

    assert response.status_code == 200
    assert captured["window_session_id"] == "ws-browser-1910"
    assert captured["request_conversation_session_id"] is None


def test_generate_threads_resolved_namespace_into_chat_history_reads(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    resolved_namespace = "#V#test_user@test_org"
    captured_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_generate_namespace_context",
        lambda **_kwargs: {
            "namespace": resolved_namespace,
            "namespace_source": "test_override",
            "effective_context_source": "test",
            "effective_context_namespace": resolved_namespace,
            "session_namespace": None,
            "candidates": [
                {"namespace": resolved_namespace, "source": "test_override"}
            ],
            "mismatch_detected": False,
            "org_scope_preferred": True,
        },
    )

    def _capture_chat_history(
        user_id: str,
        session_id: str,
        *,
        namespace: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        captured_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "namespace": namespace,
            }
        )
        return []

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        _capture_chat_history,
    )

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})

    assert response.status_code == 200
    assert len(captured_calls) >= 2
    assert all(call["namespace"] == resolved_namespace for call in captured_calls)


def test_generate_live_response_persistence_skips_best_effort_rag_indexing(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    captured_calls: list[dict[str, Any]] = []

    def _capture_add_message_to_history(
        user_id: str,
        session_id: str,
        message: dict[str, Any],
        llm_debug_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        captured_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "message": dict(message),
                "llm_debug_data": llm_debug_data,
                "skip_rag_indexing": kwargs.get("skip_rag_indexing"),
            }
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _capture_add_message_to_history,
    )

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})

    assert response.status_code == 200
    assert captured_calls
    early_user_call = next(
        call for call in captured_calls if call["message"].get("role") == "user"
    )
    assert early_user_call["skip_rag_indexing"] is True
    assistant_call = next(
        call for call in captured_calls if call["message"].get("role") == "assistant"
    )
    assert assistant_call["skip_rag_indexing"] is True


def test_generate_tool_pipeline_persistence_skips_best_effort_rag_indexing(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    captured_calls: list[dict[str, Any]] = []

    def _capture_add_message_to_history(
        user_id: str,
        session_id: str,
        message: dict[str, Any],
        llm_debug_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        captured_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "message": dict(message),
                "llm_debug_data": llm_debug_data,
                "skip_rag_indexing": kwargs.get("skip_rag_indexing"),
            }
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _capture_add_message_to_history,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )

    assert response.status_code == 200
    assert captured_calls
    assert all(call["skip_rag_indexing"] is True for call in captured_calls)
    assert any(call["message"].get("role") == "assistant" for call in captured_calls)
