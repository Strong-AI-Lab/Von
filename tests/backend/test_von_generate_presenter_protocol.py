from __future__ import annotations

from flask import Flask
from typing import Protocol


class _LLMProtocol(Protocol):
    def generate(self, prompt, context, model) -> str:  # pragma: no cover
        ...


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return self._response_text


class _StubLLMSequence:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        if not self._responses:
            raise AssertionError("No stubbed LLM responses remaining")
        return self._responses.pop(0)


class _StubOrchestrator:
    def __init__(
        self,
        result,
        *,
        workflow_result=None,
        workflow_capable: bool = False,
    ):
        self._result = result
        self.calls: list[dict] = []
        self.workflow_calls: list[dict] = []
        self._workflow_result = workflow_result
        if workflow_capable:
            # Presence is used as capability check before workflow execution.
            self._run_llm_with_fallbacks = object()

    def configure_execution_caps(self, **_kwargs) -> None:
        return None

    def set_progress_callback(self, _callback) -> None:
        return None

    def _load_workflow_model_policy(self, *_args, **_kwargs):
        return (None, None)

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self._result

    def execute_workflow(self, *args, **kwargs):
        self.workflow_calls.append({"args": args, "kwargs": kwargs})
        return self._workflow_result


class _StubWorkflowResult:
    def __init__(self, data: dict, *, completed: bool = True, error: str | None = None):
        self.data = dict(data)
        self.completed = completed
        self.error = error


def _make_app(monkeypatch, llm: _LLMProtocol) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    def _render_prompt_stub(self, _concept_ids, *, variables=None, **_kwargs):
        vars_map = variables or {}
        user_message = str(vars_map.get("user_message") or "")
        assistant_response = str(vars_map.get("assistant_response") or "")
        rendered_text = (
            "Return ONLY a JSON array with up to 4 quick-reply strings.\n\n"
            f"User message:\n{user_message}\n\n"
            f"Assistant response:\n{assistant_response}"
        )
        return type(
            "RenderedPromptStub",
            (),
            {
                "prompt_id": "#V#buttonify_prompt_v1",
                "text": rendered_text,
                "truncated": False,
            },
        )()

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.PromptTemplateService.render_prompt",
        _render_prompt_stub,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._set_tool_progress",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._build_turn_execution_diagnostics",
        lambda **_kwargs: {
            "generated_at_utc": "2026-03-29T00:00:00Z",
            "request_id": None,
            "code_version": "test",
            "code_version_details": {"version": "test"},
            "elapsed_ms": 0,
            "prompt_preview": None,
            "latest_progress": None,
            "progress_events": [],
            "activity_history": [],
            "phase_history": [],
            "tool_history": [],
            "tool_call_count": 0,
            "tool_success_count": 0,
            "tool_failure_count": 0,
            "tool_pending_count": 0,
            "tool_call_start_count": 0,
            "tool_call_end_count": 0,
            "workflow_discovery": None,
            "workflow_routing_diagnostics": None,
            "workflow_stage_model": {"schema_version": "conversation_turn_stage_model.v1", "stages": []},
            "workflow_stage_path": None,
            "stage_diagnostics": [],
            "timing_breakdown": None,
        },
    )
    def _finalise_llm_debug_info_stub(*, llm_debug_info, actor_concept_id=None, namespace=None, **_kwargs):
        payload = dict(llm_debug_info)
        resolved_actor = (
            actor_concept_id
            if isinstance(actor_concept_id, str) and actor_concept_id.strip()
            else namespace
            if isinstance(namespace, str) and namespace.strip()
            else payload.get("actor_concept_id")
        )
        if isinstance(resolved_actor, str) and resolved_actor.strip():
            payload["actor_concept_id"] = resolved_actor.strip()
        payload["turn_execution_record"] = {
            "actor_concept_id": payload.get("actor_concept_id"),
            "execution": {
                "workflow_stage_path": {
                    "schema_version": "conversation_turn_stage_path.v1",
                    "path": [],
                }
            },
        }
        return payload

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._finalise_llm_debug_info",
        _finalise_llm_debug_info_stub,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: {
            "matches": [],
            "candidates": [],
            "match_count": 0,
            "candidate_count": 0,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def _find_transformation_event(llm_debug: dict, transform_name: str) -> dict:
    telemetry = llm_debug.get("response_transformations")
    assert isinstance(telemetry, dict)
    transformations = telemetry.get("transformations")
    assert isinstance(transformations, list)
    for event in transformations:
        if isinstance(event, dict) and event.get("transform_name") == transform_name:
            return event
    raise AssertionError(f"Missing transformation event: {transform_name}")


def test_generate_extracts_presenter_blocks_and_returns_response_channels(monkeypatch):
    llm = _StubLLM(
        "<spoken>Hello there.</spoken>\n<screen>Here is the on-screen content.</screen>"
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "Here is the on-screen content."
    assert body["response_channels"] == {
        "screen": "Here is the on-screen content.",
        "spoken": "Hello there.",
        "format": "tagged_blocks_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug["presenter_channels"]["screen"] == "Here is the on-screen content."
    assert llm_debug["presenter_channels"]["spoken"] == "Hello there."
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is False
    assert llm_debug.get("spoken_backfill_second_pass_reason") is None
    display_elements = body["display_elements"]
    assert display_elements["schema_version"] == "turn_display_elements_v1"
    assert display_elements["validation"]["valid"] is True
    screen_element = next(
        element
        for element in display_elements["elements"]
        if element["element_id"] == "screen_text"
    )
    spoken_element = next(
        element
        for element in display_elements["elements"]
        if element["element_id"] == "spoken_text"
    )
    assert screen_element["payload"]["text"] == "Here is the on-screen content."
    assert spoken_element["payload"]["text"] == "Hello there."
    assert llm_debug["display_elements"]["schema_version"] == "turn_display_elements_v1"
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "skipped"
    assert screen_backfill_event["suppression_reason"] == "not_required"
    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "skipped"
    assert spoken_backfill_event["suppression_reason"] == "not_required"

    assert len(llm.calls) == 1
    sent_context = llm.calls[0]["context"]
    assert sent_context
    assert sent_context[0]["role"] == "system"
    assert "PRESENTER MODE PROTOCOL" in sent_context[0]["content"]


def test_generate_preserves_fenced_code_inside_screen_block(monkeypatch):
    llm = _StubLLM(
        "<spoken>Talk track.</spoken>\n"
        "<screen>Strict JSON schema:\n"
        "```json\n"
        '{"mapping_type": "input"}\n'
        "```\n"
        "Done.</screen>"
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    expected_screen = (
        "Strict JSON schema:\n"
        "```json\n"
        '{"mapping_type": "input"}\n'
        "```\n"
        "Done."
    )
    assert body["response"] == expected_screen
    assert body["response_channels"]["screen"] == expected_screen
    assert body["response_channels"]["spoken"] == "Talk track."


def test_generate_buttonify_uses_llm_extraction(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")
    llm = _StubLLMSequence(
        [
            'Please reply with one of: "Proceed", "Hold".',
            '["Proceed", "Hold"]',
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "llm"
    assert buttonify.get("options") == ["Proceed", "Hold"]
    filtering_boundary = buttonify.get("filtering_boundary")
    assert isinstance(filtering_boundary, dict)
    assert filtering_boundary.get("schema_version") == "buttonify_filtering_boundary_v1"
    assert filtering_boundary.get("accepted_candidate_count") == 2
    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "success"
    assert buttonify_event["source_path"] == "llm"
    assert buttonify_event["options_emitted_count"] == 2
    assert isinstance(buttonify_event["output_summary"].get("filtering_boundary"), dict)
    assert "timestamp_utc" in buttonify_event

    # First call is assistant response, second call is buttonify extraction.
    assert len(llm.calls) == 2


def test_generate_buttonify_noops_when_vontology_prompt_unavailable(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")

    llm = _StubLLM('Please reply with one of: "Proceed", "Hold".')
    app = _make_app(monkeypatch, llm)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.PromptTemplateService.render_prompt",
        lambda self, *_args, **_kwargs: None,
    )

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "none"
    assert buttonify.get("options") == []
    assert buttonify.get("prompt_available") is False
    assert buttonify.get("suppression_reason") == "buttonify_prompt_unavailable"

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "no_op"
    assert buttonify_event["suppression_reason"] == "buttonify_prompt_unavailable"
    assert buttonify_event["options_emitted_count"] == 0

    # Prompt unavailable must suppress the extra buttonify model pass.
    assert len(llm.calls) == 1


def test_generate_buttonify_non_json_llm_output_noops(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")

    llm = _StubLLMSequence(
        [
            'Use these IDs: "#V#academic_conference", "#V#research_symposium".',
            'Use these labels: "Create concepts", "Show existing meetings".',
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "none"
    assert buttonify.get("options") == []
    assert buttonify.get("suppression_reason") == "no_candidates"
    filtering_boundary = buttonify.get("filtering_boundary")
    assert isinstance(filtering_boundary, dict)
    assert filtering_boundary.get("schema_version") == "buttonify_filtering_boundary_v1"

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "no_op"
    assert buttonify_event["source_path"] == "none"
    assert buttonify_event["suppression_reason"] == "no_candidates"
    assert buttonify_event["options_emitted_count"] == 0

    # First LLM call is the assistant response, second is buttonify extraction.
    assert len(llm.calls) == 2


def test_generate_coding_agent_turn_captures_narration_buttonify_and_layout(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")
    llm = _StubLLMSequence(
        [
            "<spoken>Proceed with the update.</spoken>\n"
            '<screen>Please reply with one of: "Proceed", "Hold".</screen>',
            '["Proceed", "Hold"]',
        ]
    )
    app = _make_app(monkeypatch, llm)

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#github_copilot_instance",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]

    assert body["response_channels"]["spoken"] == "Proceed with the update."
    assert body["response_channels"]["screen"] == 'Please reply with one of: "Proceed", "Hold".'

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "success"
    assert buttonify_event["source_path"] == "llm"
    assert buttonify_event["options_emitted_count"] == 2

    display_elements = body["display_elements"]
    assert display_elements["schema_version"] == "turn_display_elements_v1"
    assert display_elements["validation"]["valid"] is True
    element_ids = [element["element_id"] for element in display_elements["elements"]]
    assert "screen_text" in element_ids
    assert "spoken_text" in element_ids

    turn_execution_record = llm_debug.get("turn_execution_record")
    assert isinstance(turn_execution_record, dict)
    assert turn_execution_record.get("actor_concept_id") == "#V#github_copilot_instance"
    assert llm_debug.get("actor_concept_id") == "#V#github_copilot_instance"
    execution = turn_execution_record.get("execution")
    assert isinstance(execution, dict)
    workflow_stage_path = execution.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    assert isinstance(workflow_stage_path.get("path"), list)


def test_generate_buttonify_telemetry_reports_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        lambda: False,
    )
    llm = _StubLLM("No options here.")
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    llm_debug = resp.get_json()["llm_debug"]
    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "skipped"
    assert buttonify_event["suppression_reason"] == "buttonify_disabled"
    assert buttonify_event["options_emitted_count"] == 0


def test_generate_legacy_buttonify_preflight_symbol_lookup_does_not_crash(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    llm = _StubLLM("No options here.")
    app = _make_app(monkeypatch, llm)

    import src.backend.server.routes.von_routes as von_routes_module

    def _legacy_get_buttonify_model_enabled() -> bool:
        # Simulate a stale runtime path that still resolves the retired symbol
        # name at module scope.
        return bool(
            eval(
                "get_buttonify_heuristic_preflight_enabled()",
                dict(vars(von_routes_module)),
            )
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        _legacy_get_buttonify_model_enabled,
    )

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    llm_debug = resp.get_json()["llm_debug"]
    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "skipped"
    assert buttonify_event["suppression_reason"] == "buttonify_disabled"
    assert buttonify_event["options_emitted_count"] == 0


def test_generate_buttonify_uses_workflow_when_available(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult
    from src.backend.workflows import CHAT_BUTTONIFY_WORKFLOW_ID

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    llm = _StubLLM("unused")
    app = _make_app(monkeypatch, llm)

    orchestrator_result = OrchestratorResult(
        response_text="Assistant response with actionable options.",
        extra_messages=[],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    workflow_result = _StubWorkflowResult(
        {
            "buttonify_status": "success",
            "buttonify_options": ["Proceed", "Hold"],
            "buttonify_source": "llm",
            "buttonify_prompt_id": "#V#buttonify_prompt_v1",
            "buttonify_prompt_truncated": False,
            "buttonify_model_used": "test-model",
            "buttonify_model_attempted": True,
            "buttonify_error_class": None,
            "buttonify_suppression_reason": None,
            "output_transformation_contract": {
                "schema_version": "output_transformation_workflow_contract_v1",
                "transform_name": "buttonify",
            },
        }
    )
    stub_orchestrator = _StubOrchestrator(
        orchestrator_result,
        workflow_result=workflow_result,
        workflow_capable=True,
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = stub_orchestrator

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "llm"
    assert buttonify.get("options") == ["Proceed", "Hold"]
    assert buttonify.get("workflow_used") is True
    assert buttonify.get("workflow_available") is True
    assert isinstance(buttonify.get("workflow_contract"), dict)

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "success"
    assert buttonify_event["source_path"] == "llm"
    assert buttonify_event["options_emitted_count"] == 2

    assert stub_orchestrator.workflow_calls
    first_call = stub_orchestrator.workflow_calls[0]
    assert first_call["args"][0] == CHAT_BUTTONIFY_WORKFLOW_ID
    assert len(llm.calls) == 0


def test_generate_buttonify_workflow_invalid_output_noops(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    llm = _StubLLM("unused")
    app = _make_app(monkeypatch, llm)

    orchestrator_result = OrchestratorResult(
        response_text='Please reply with one of: "Proceed", "Hold".',
        extra_messages=[],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    workflow_result = _StubWorkflowResult(
        {
            "buttonify_status": "no_op",
            "buttonify_options": [],
            "buttonify_source": "none",
            "buttonify_prompt_available": True,
            "buttonify_prompt_error": None,
            "buttonify_model_used": "test-model",
            "buttonify_model_attempted": True,
            "buttonify_error_class": None,
            "buttonify_suppression_reason": None,
        }
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(
        orchestrator_result,
        workflow_result=workflow_result,
        workflow_capable=True,
    )

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    llm_debug = resp.get_json()["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "none"
    assert buttonify.get("options") == []

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "no_op"
    assert buttonify_event["suppression_reason"] == "no_candidates"
    assert buttonify_event["options_emitted_count"] == 0
    assert len(llm.calls) == 0


def test_history_debug_transformations_view_returns_lightweight_payload(monkeypatch):
    llm = _StubLLM("Hello")
    app = _make_app(monkeypatch, llm)

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#user"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._derive_namespace_for_user_org",
        lambda *_args, **_kwargs: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.resolve_chat_history_namespace",
        lambda *_args, **_kwargs: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        lambda **_kwargs: {
            "request_id": "req-123",
            "response_transformations": {
                "schema_version": "response_transformations_v1",
                "event_schema_version": "response_transformation_event_v1",
                "generated_at_utc": "2026-02-20T00:00:00Z",
                "transformations": [
                    {
                        "transform_name": "buttonify",
                        "transform_version": "v1",
                        "status": "success",
                        "input_summary": {},
                        "output_summary": {},
                        "options_emitted_count": 1,
                        "source_path": "llm",
                        "latency_ms": 1.0,
                        "model_id": None,
                        "suppression_reason": None,
                        "error_class": None,
                        "timestamp_utc": "2026-02-20T00:00:00Z",
                    }
                ],
            },
        },
    )

    client = app.test_client()
    resp = client.get(
        "/von/history/debug",
        query_string={
            "session_id": "session-1",
            "history_index": 0,
            "view": "transformations",
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert "llm_debug_data" not in body
    assert body["transformations_count"] == 1
    assert body["response_transformations"]["schema_version"] == "response_transformations_v1"


def test_extract_presenter_channels_ignores_tags_inside_fenced_blocks():
    from src.backend.server.routes.von_routes import _extract_presenter_channels

    text = (
        "```html\n"
        "<screen>Ignore this fake block.</screen>\n"
        "```\n"
        "<spoken>Real spoken.</spoken>\n"
        "<screen>Real screen.</screen>"
    )
    channels = _extract_presenter_channels(text)
    assert channels is not None
    assert channels["spoken"] == "Real spoken."
    assert channels["screen"] == "Real screen."


def test_generate_generates_spoken_when_only_screen_tag_present(monkeypatch):
    llm = _StubLLMSequence(
        [
            "<screen>Only screen.</screen>",
            "<spoken>Short talk track.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "Only screen."
    assert body["response_channels"] == {
        "screen": "Only screen.",
        "spoken": "Short talk track.",
        "format": "narration_fallback_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert llm_debug.get("spoken_backfill_second_pass_reason") == "missing_spoken"

    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_generate_backfills_screen_when_only_spoken_tag_present(monkeypatch):
    llm = _StubLLMSequence(
        [
            "<spoken>Short talk track.</spoken>",
            "<screen>Expanded on-screen answer with detail.</screen>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "Expanded on-screen answer with detail."
    assert body["response_channels"] == {
        "screen": "Expanded on-screen answer with detail.",
        "spoken": "Short talk track.",
        "format": "screen_backfill_from_tools_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is False

    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] == "Generate <screen> display content"


def test_generate_presenter_mode_falls_back_to_second_pass_spoken(monkeypatch):
    llm = _StubLLMSequence(
        [
            "This is the full on-screen answer with details.",
            "<spoken>Short summary for TTS.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "This is the full on-screen answer with details."
    assert body["response_channels"] == {
        "screen": "This is the full on-screen answer with details.",
        "spoken": "Short summary for TTS.",
        "format": "narration_fallback_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert (
        llm_debug.get("spoken_backfill_second_pass_reason")
        == "missing_presenter_channels"
    )
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "fallback_success"
    assert screen_backfill_event["source_path"] == "response_text"
    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_generate_presenter_mode_rewrites_internal_status_screen_backfill(monkeypatch):
    llm = _StubLLMSequence(
        [
            (
                "Execution status: requested mutation was not executed. "
                "Blocking effect IDs: effect_paper_representation_1. "
                "Unresolved preconditions: No required representation tool execution was observed. "
                "Failure codes: paper_representation_not_executed."
            ),
            (
                "<screen>The requested analysis did not complete cleanly, so there is no reliable on-screen answer yet. "
                "Please retry with the intended analysis workflow.</screen>"
            ),
            "<spoken>Short summary for TTS.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Please analyse paper status", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    expected_screen = (
        "The requested analysis did not complete cleanly, so there is no reliable "
        "on-screen answer yet. Please retry with the intended analysis workflow."
    )

    assert body["response"] == expected_screen
    assert body["response_channels"] == {
        "screen": expected_screen,
        "spoken": "Short summary for TTS.",
        "format": "narration_fallback_v1",
    }
    assert "Execution status:" not in body["response"]
    assert "Blocking effect IDs:" not in body["response"]
    assert "Failure codes:" not in body["response"]

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert (
        llm_debug.get("spoken_backfill_second_pass_reason")
        == "missing_presenter_channels"
    )

    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "success"
    assert screen_backfill_event["source_path"] == "llm_synthesis"

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 3
    assert llm.calls[1]["prompt"] == "Generate <screen> display content"
    assert (
        "internal execution-status text"
        in llm.calls[1]["context"][1]["content"]
    )
    assert llm.calls[2]["prompt"] == "Generate <spoken> talk track"


def test_generate_accepts_plain_text_from_narration_second_pass(monkeypatch):
    llm = _StubLLMSequence(
        [
            "This is the full on-screen answer with **markdown** and details.",
            "Short summary for TTS.",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert (
        body["response"]
        == "This is the full on-screen answer with **markdown** and details."
    )
    assert body["response_channels"] == {
        "screen": "This is the full on-screen answer with **markdown** and details.",
        "spoken": "Short summary for TTS.",
        "format": "narration_fallback_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert (
        llm_debug.get("spoken_backfill_second_pass_reason")
        == "missing_presenter_channels"
    )

    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_generate_narration_prompt_includes_preferred_and_max_speaking_seconds(
    monkeypatch,
):
    llm = _StubLLMSequence(
        [
            "This is the full on-screen answer with details.",
            "<spoken>Short talk track.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()

    with client.session_transaction() as sess:
        sess["client_capabilities_snapshot"] = {
            "kind": "client_capabilities",
            "client_reported": True,
            "speech_synthesis": {
                "supported": True,
                "voices_count": 1,
                "default_voice_lang": "en-NZ",
                "settings": {
                    "max_speaking_seconds": 40,
                    "preferred_speaking_seconds": 20,
                },
            },
        }

    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    assert len(llm.calls) == 2

    system_text = llm.calls[1]["context"][0]["content"]
    assert "Speech timing hint" in system_text, system_text
    assert "preferred_speaking_seconds=20" in system_text
    assert "max_speaking_seconds=40" in system_text


def test_presenter_mode_uses_tool_screen_when_screen_tag_missing(monkeypatch):
    import json

    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    tool_payload = {
        "tool": "fetch_concept",
        "status": "ok",
        "duration_ms": 12,
        "payload": {"concept_id": "#V#example"},
    }
    tool_message = {"role": "tool", "content": json.dumps(tool_payload)}
    orchestrator_result = OrchestratorResult(
        response_text="Plain response without presenter tags.",
        extra_messages=[tool_message],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    expected_screen = "Plain response without presenter tags."

    assert body["response"] == expected_screen
    assert body["response_channels"] == {
        "screen": expected_screen,
        "spoken": "Short talk track.",
        "format": "screen_backfill_from_response_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert llm_debug.get("spoken_backfill_second_pass_reason") == "missing_spoken"

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Generate <spoken> talk track"


def test_presenter_mode_uses_shared_follow_up_summary_for_incomplete_tool_turns(
    monkeypatch,
):
    import json

    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "payload": {"query": "paper", "results_count": 20},
                }
            ),
        },
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "payload": {"query": "paper", "results_count": 10},
                }
            ),
        },
    ]
    aux_llm_calls = (
        {
            "type": "turn_completion_gate",
            "decision": "escalation_required",
            "decision_reason": (
                "The tool path did not complete the requested paper-status analysis."
            ),
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "blocking_effect_ids": ["effect_paper_representation_1"],
        },
    )
    orchestrator_result = OrchestratorResult(
        response_text="Plain response without presenter tags.",
        extra_messages=tool_messages,
        tool_invocations=(),
        aux_llm_calls=aux_llm_calls,
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Analyse paper status", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert body["response"] == screen_text
    assert body["response_channels"]["spoken"] == "Short talk track."
    assert body["response_channels"]["format"].startswith("screen_backfill_")
    assert (
        "I ran tools for this request, but I do not have a reliable final answer yet."
        in screen_text
    )
    assert (
        "This turn still needs follow-up before it should be treated as complete."
        in screen_text
    )
    assert "The tool path did not complete the requested paper-status analysis." in screen_text
    assert "Tool activity diagnostics:" in screen_text
    assert "search_concepts — ok" in screen_text
    assert "Plain response without presenter tags." not in screen_text

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert llm_debug.get("spoken_backfill_second_pass_reason") == "missing_spoken"

    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "fallback_success"
    assert screen_backfill_event["source_path"] == "follow_up_summary"

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Generate <spoken> talk track"
    assert (
        "I ran tools for this request, but I do not have a reliable final answer yet."
        in llm.calls[0]["context"][1]["content"]
    )


def test_presenter_mode_rewrites_failed_workflow_status_screen_backfill(monkeypatch):
    llm = _StubLLMSequence(
        [
            (
                "Workflow SAIL Phd Student Onboarding Workflow failed "
                "(state: Onboarding Step: Collect Student Info Type)."
            ),
            (
                "<screen>I couldn't complete that request because the selected "
                "workflow failed before it produced a usable result.</screen>"
            ),
            "<spoken>I couldn't complete that request.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Analyse paper status", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert body["response"] == screen_text
    raw_failure_text = (
        "Workflow SAIL Phd Student Onboarding Workflow failed "
        "(state: Onboarding Step: Collect Student Info Type)."
    )
    assert raw_failure_text not in screen_text
    assert "workflow failed" in screen_text.lower()
    assert body["response_channels"]["spoken"] == "I couldn't complete that request."

    llm_debug = body["llm_debug"]
    screen_backfill_event = _find_transformation_event(
        llm_debug, "screen_backfill"
    )
    assert screen_backfill_event["status"] == "success"
    assert screen_backfill_event["source_path"] == "llm_synthesis"

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 3
    assert llm.calls[1]["prompt"] == "Generate <screen> display content"
    assert (
        "internal execution-status text"
        in llm.calls[1]["context"][1]["content"]
    )
    assert llm.calls[2]["prompt"] == "Generate <spoken> talk track"


def test_presenter_mode_rejects_hallucinated_description_write_in_screen_backfill(
    monkeypatch,
):
    import json

    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(
        [
            "<screen>Description updated: YES</screen>",
            "<spoken>Short talk track.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    tool_payload = {
        "tool": "add_relationship",
        "status": "ok",
        "duration_ms": 12,
        "payload": {
            "source_id": "#V#example",
            "predicate": "#V#is_a",
            "target": "#V#concept",
            "added": True,
        },
    }
    tool_message = {"role": "tool", "content": json.dumps(tool_payload)}
    orchestrator_result = OrchestratorResult(
        response_text=(
            "Tool results:\n\n" "```json\n" '{"tool": "add_relationship"}\n' "```"
        ),
        extra_messages=[tool_message],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert "Write activity (authoritative):" in screen_text
    assert "Relationship writes detected" in screen_text
    assert "Description updated: YES" not in screen_text
    assert "Description updated: NO" not in screen_text

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"

    assert len(llm.calls) == 2
    assert llm.calls[0]["prompt"] == "Generate <screen> display content"
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_presenter_mode_preserves_required_screen_json_fence_from_prompt(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    orchestrator_result = OrchestratorResult(
        response_text="Tool-grounded facts only.",
        extra_messages=[],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    prompt = (
        "Please include this exact fenced block verbatim:\n\n"
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "If anything fails, include exact error text/reason_code."
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": prompt, "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    screen_text = body["response_channels"]["screen"]
    expected_fence = (
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "```"
    )

    assert expected_fence in screen_text
    assert body["llm_debug"].get("screen_backfill_second_pass_attempted") is True
    assert body["llm_debug"].get("screen_backfill_second_pass_reason") in {
        "missing_screen",
        "missing_screen_fence",
    }
    display_elements = body["display_elements"]
    json_blocks = [
        element
        for element in display_elements["elements"]
        if element["element_type"] == "json_block"
    ]
    assert json_blocks
    assert any(element["payload"]["fence"] == expected_fence for element in json_blocks)


def test_tool_messages_prompt_blob_includes_create_concepts_canonical_ids():
    import json

    from src.backend.server.routes.von_routes import _build_tool_messages_prompt_blob

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "create_concepts",
                    "status": "ok",
                    "payload": {
                        "total": 2,
                        "successful": 2,
                        "results": [
                            {
                                "success": True,
                                "requested_name": "Otter Session",
                                "concept_id": "#V#otter_session",
                            },
                            {
                                "success": True,
                                "requested_name": "Otter Session 2",
                                "canonical_concept_id": "#V#otter_session_2",
                            },
                        ],
                    },
                }
            ),
        }
    ]

    blob = _build_tool_messages_prompt_blob(tool_messages)

    assert "TOOL WRITES LEDGER (authoritative):" in blob
    assert "Otter Session (#V#otter_session)" in blob
    assert "Otter Session 2 (#V#otter_session_2)" in blob


def test_presenter_screen_summary_includes_create_concepts_canonical_ids():
    import json

    from src.backend.server.routes.von_routes import (
        _build_presenter_screen_summary_from_tool_messages,
    )

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "create_concepts",
                    "status": "ok",
                    "payload": {
                        "total": 1,
                        "successful": 1,
                        "results": [
                            {
                                "success": True,
                                "requested_name": "Planck Mission",
                                "canonical_concept_id": "#V#planck_mission",
                            }
                        ],
                    },
                }
            ),
        }
    ]

    summary = _build_presenter_screen_summary_from_tool_messages(tool_messages)

    assert isinstance(summary, str)
    assert "Planck Mission (#V#planck_mission)" in summary


def test_presenter_mode_can_disable_legacy_screen_fence_insertion(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_DISPLAY_ELEMENTS_SCREEN_FENCE_COMPAT_ENABLE", "0")

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    orchestrator_result = OrchestratorResult(
        response_text="Tool-grounded facts only.",
        extra_messages=[],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    prompt = (
        "Please include this exact fenced block verbatim:\n\n"
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "If anything fails, include exact error text/reason_code."
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": prompt, "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    screen_text = body["response_channels"]["screen"]
    expected_fence = (
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "```"
    )

    # With compat disabled, screen text is no longer mutated to append the fence.
    assert expected_fence not in screen_text
    assert body["llm_debug"]["display_elements_screen_fence_compat_enabled"] is False

    display_elements = body["display_elements"]
    assert "required_screen_json_fence_appended" in display_elements["reason_codes"]
    json_blocks = [
        element
        for element in display_elements["elements"]
        if element["element_type"] == "json_block"
    ]
    assert json_blocks
    assert any(element["payload"]["fence"] == expected_fence for element in json_blocks)
