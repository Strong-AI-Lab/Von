from __future__ import annotations

import json

from flask import Flask
from typing import Protocol

from src.backend.services.prompt_template_service import _render_template

_REPRESENTED_SCREEN_BACKFILL_PROMPT = (
    "Represented screen backfill prompt for tests.\n\n"
    "Return exactly one <screen> block and follow the Vontology-authored "
    "screen-backfill policy."
)


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
        self.supervised_calls: list[dict] = []
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

    def execute_conversation_turn_supervised(self, **kwargs):
        payload = dict(kwargs)
        self.supervised_calls.append(payload)
        self.calls.append(payload)
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

    def _resolve_prompt_text_stub(self, concept_ids, *, fallback=None, **_kwargs):
        ids = tuple(concept_ids or ())
        if "#V#von_screen_content_prompt_for_witbrock" in ids:
            return (
                "#V#von_screen_content_prompt_for_witbrock",
                _REPRESENTED_SCREEN_BACKFILL_PROMPT,
            )
        if isinstance(fallback, str) and fallback.strip():
            return (None, fallback.strip())
        return (None, None)

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.PromptTemplateService.resolve_prompt_text",
        _resolve_prompt_text_stub,
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
            "workflow_stage_model": {
                "schema_version": "conversation_turn_stage_model.v1",
                "stages": [],
            },
            "workflow_stage_path": None,
            "stage_diagnostics": [],
            "timing_breakdown": None,
        },
    )

    def _finalise_llm_debug_info_stub(
        *, llm_debug_info, actor_concept_id=None, namespace=None, **_kwargs
    ):
        payload = dict(llm_debug_info)
        resolved_actor = (
            actor_concept_id
            if isinstance(actor_concept_id, str) and actor_concept_id.strip()
            else (
                namespace
                if isinstance(namespace, str) and namespace.strip()
                else payload.get("actor_concept_id")
            )
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


def _find_aux_event(
    llm_debug: dict,
    event_type: str,
    *,
    reason_code: str | None = None,
) -> dict:
    aux_llm_calls = llm_debug.get("aux_llm_calls")
    assert isinstance(aux_llm_calls, list)
    for event in aux_llm_calls:
        if not isinstance(event, dict):
            continue
        if event.get("type") != event_type:
            continue
        if reason_code is not None and event.get("reason_code") != reason_code:
            continue
        return event
    if reason_code is not None:
        raise AssertionError(
            f"Missing aux event: {event_type} with reason_code={reason_code}"
        )
    raise AssertionError(f"Missing aux event: {event_type}")


def _screen_backfill_context_payload(call: dict) -> dict:
    assert call["prompt"] == _REPRESENTED_SCREEN_BACKFILL_PROMPT
    context = call["context"]
    assert len(context) == 1
    assert context[0]["role"] == "user"
    payload = json.loads(context[0]["content"])
    assert payload["schema_version"] == "presenter_screen_backfill_context.v1"
    assert payload["stage_concept_id"] == "#V#screen_backfill_stage"
    assert payload["screen_prompt_concept_ids"] == [
        "#V#von_screen_content_prompt_for_witbrock"
    ]
    return payload


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
    assert "heuristic_preflight_enabled" not in buttonify
    assert "preflight_rejection_reason" not in buttonify
    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "success"
    assert buttonify_event["source_path"] == "llm"
    assert buttonify_event["options_emitted_count"] == 2
    assert "heuristic_preflight_enabled" not in buttonify_event["input_summary"]
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


def test_generate_buttonify_route_fallback_honours_prompt_contract_variables(
    monkeypatch,
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")

    llm = _StubLLMSequence(
        [
            'Please reply with one of: "Proceed", "Hold".',
            '["Proceed", "Hold"]',
        ]
    )
    app = _make_app(monkeypatch, llm)

    def _strict_buttonify_prompt(self, _concept_ids, *, variables=None, **_kwargs):
        rendered_text = _render_template(
            (
                "Return ONLY a JSON array with up to 4 quick-reply strings.\n\n"
                "User message:\n{user_message}\n\n"
                "Assistant response:\n{assistant_response}"
            ),
            dict(variables or {}),
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
        _strict_buttonify_prompt,
    )

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "llm"
    assert buttonify.get("options") == ["Proceed", "Hold"]

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "success"
    assert buttonify_event["source_path"] == "llm"
    assert len(llm.calls) == 2


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


def test_generate_coding_agent_turn_captures_narration_buttonify_and_layout(
    monkeypatch,
):
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
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.create_chat_session",
        lambda **_kwargs: {"session_name": None},
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
    assert (
        body["response_channels"]["screen"]
        == 'Please reply with one of: "Proceed", "Hold".'
    )

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


def test_generate_buttonify_can_be_skipped_by_request(monkeypatch):
    llm = _StubLLM("No options here.")
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "skip_buttonify": True},
    )

    assert resp.status_code == 200
    llm_debug = resp.get_json()["llm_debug"]
    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "skipped"
    assert buttonify_event["suppression_reason"] == "buttonify_skipped_by_request"
    assert buttonify_event["options_emitted_count"] == 0


def test_generate_buttonify_skips_in_agent_test_instance(monkeypatch):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    llm = _StubLLM("No options here.")
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    llm_debug = resp.get_json()["llm_debug"]
    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "skipped"
    assert buttonify_event["suppression_reason"] == "agent_test_instance"
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
    assert "heuristic_preflight_enabled" not in buttonify
    assert "preflight_rejection_reason" not in buttonify

    buttonify_event = _find_transformation_event(llm_debug, "buttonify")
    assert buttonify_event["status"] == "success"
    assert buttonify_event["source_path"] == "llm"
    assert buttonify_event["options_emitted_count"] == 2
    assert "heuristic_preflight_enabled" not in buttonify_event["input_summary"]

    assert stub_orchestrator.workflow_calls
    first_call = stub_orchestrator.workflow_calls[0]
    assert first_call["args"][0] == CHAT_BUTTONIFY_WORKFLOW_ID
    assert first_call["kwargs"]["data"]["prefer_default_model"] is True
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
    assert (
        body["response_transformations"]["schema_version"]
        == "response_transformations_v1"
    )


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
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "success"
    assert screen_backfill_event["source_path"] == "represented_screen_prompt"
    payload = _screen_backfill_context_payload(llm.calls[1])
    assert payload["backfill_reason"] == "missing_screen"
    assert payload["model_response"] == "<spoken>Short talk track.</spoken>"


def test_presenter_screen_backfill_fails_closed_when_represented_prompt_missing(
    monkeypatch,
):
    llm = _StubLLM("<spoken>Short talk track.</spoken>")
    app = _make_app(monkeypatch, llm)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.PromptTemplateService.resolve_prompt_text",
        lambda *_args, **_kwargs: (None, None),
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "no_op"
    assert (
        screen_backfill_event["source_path"]
        == "represented_screen_prompt_unavailable"
    )
    assert (
        screen_backfill_event["suppression_reason"]
        == "represented_screen_prompt_unavailable"
    )
    authority_gate_event = _find_aux_event(
        llm_debug,
        "presenter_screen_backfill",
        reason_code="screen_backfill_prompt_unavailable",
    )
    assert authority_gate_event["decision_class"] == "presenter_authority_gate"
    assert authority_gate_event["decision_source"] == "represented_prompt_resolution"
    assert authority_gate_event["possible_inappropriate_python_code_use"] is False
    assert len(llm.calls) == 1


def test_presenter_mode_reconstructs_tool_messages_from_invocations_when_missing(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "0")

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    aux_llm_calls = (
        {
            "type": "turn_completion_gate",
            "decision": "escalation_required",
            "decision_reason": (
                "The Gmail retrieval ran, but the final answer still needs follow-up."
            ),
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "blocking_effect_ids": ["effect_email_resolution_1"],
        },
    )
    tool_invocations = (
        {
            "tool": "gmail_get_message",
            "status": "ok",
            "duration_ms": 18,
            "effective_payload": {
                "message_id": "msg-123",
                "subject": "FW: NeurIPS 2026 has received a new review",
                "labels": ["INBOX", "IMPORTANT"],
            },
            "result_summary": "Message: FW: NeurIPS 2026 has received a new revi",
        },
    )
    orchestrator_result = OrchestratorResult(
        response_text=(
            "Execution status: follow_up_required\n"
            "Unresolved preconditions: missing presenter channels"
        ),
        extra_messages=[],
        tool_invocations=tool_invocations,
        aux_llm_calls=aux_llm_calls,
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Show me the latest email", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert "I ran tools for this request" in screen_text
    assert "Tool activity diagnostics:" in screen_text
    assert "gmail_get_message" in screen_text

    llm_debug = body["llm_debug"]
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "fallback_success"
    assert screen_backfill_event["source_path"] == "follow_up_summary"
    assert screen_backfill_event["input_summary"]["tool_message_count"] == 1

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"


def test_presenter_mode_uses_workflow_execution_evidence_for_follow_up_screen(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "0")

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    aux_llm_calls = (
        {
            "type": "workflow_execution",
            "workflow_id": "#V#example_workflow",
            "execution_summary": {
                "workflow_id": "#V#example_workflow",
                "workflow_instance_id": "instance-123",
                "completed": True,
                "effective_completed": True,
                "terminal_status": "completed",
                "final_state": "#V#workflow_done",
                "action_completed_count": 11,
                "action_success_count": 11,
                "action_failure_count": 0,
                "terminal_effect_count": 0,
                "durable_side_effect_count": 0,
            },
        },
        {
            "type": "turn_completion_gate",
            "decision": "escalation_required",
            "decision_reason": "Required mutation was not executed.",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "blocking_effect_ids": ["effect_required_tool_obligations_1"],
            "blocking_failure_codes": [
                "required_tool_not_planned",
                "mutation_succeeded_readback_missing",
            ],
            "unresolved_preconditions": [
                {
                    "effect_type": "required_evidence",
                    "status": "not_executed",
                    "status_reason": "Required read-back evidence was missing.",
                }
            ],
        },
    )
    tool_invocations = (
        {"tool": "workflow_execute", "status": "ok", "duration_ms": 18},
    )
    orchestrator_result = OrchestratorResult(
        response_text=(
            "I do not yet have a complete workflow-backed answer.\n\n"
            "Required mutation was not executed."
        ),
        extra_messages=[],
        tool_invocations=tool_invocations,
        aux_llm_calls=aux_llm_calls,
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Run the represented workflow", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert "Workflow-backed outcome:" in screen_text
    assert "terminal status `completed`" in screen_text
    assert "instance `instance-123`" in screen_text
    assert "Verification still needs follow-up" in screen_text
    assert "required_tool_not_planned" in screen_text
    assert "I do not yet have a complete workflow-backed answer" not in screen_text
    assert "Required mutation was not executed" not in screen_text

    screen_backfill_event = _find_transformation_event(
        body["llm_debug"],
        "screen_backfill",
    )
    assert screen_backfill_event["source_path"] == "follow_up_summary"


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
    assert body["turn_output_health"] == {
        "schema_version": "turn_output_health_v1",
        "status": "ok",
        "issues": [],
    }

    llm_debug = body["llm_debug"]
    assert llm_debug["turn_output_health"] == body["turn_output_health"]
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


def test_turn_output_health_flags_missing_spoken_channel():
    from src.backend.services.turn_output_health_service import build_turn_output_health

    health = build_turn_output_health(
        {
            "presenter_channels": {
                "screen": "This is the on-screen answer with enough detail.",
                "spoken": "",
                "format": "tagged_blocks_v1",
            },
            "spoken_backfill_second_pass_attempted": False,
        }
    )

    assert health["schema_version"] == "turn_output_health_v1"
    assert health["status"] == "degraded"
    assert {
        "category": "presenter_output",
        "code": "missing_spoken_channel",
        "severity": "warning",
        "message": "Presenter output missing spoken channel; text-to-speech will fall back to screen text.",
        "fallback_used": "screen_text_for_tts",
    } in health["issues"]


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
    assert screen_backfill_event["source_path"] == "represented_screen_prompt"
    detector_event = _find_aux_event(
        llm_debug,
        "presenter_detector",
        reason_code="response_candidate_internal_status_rejected",
    )
    assert detector_event["decision_class"] == "presenter_detector"
    assert detector_event["decision_source"] == "structural_pattern_detection"
    assert detector_event["possible_inappropriate_python_code_use"] is True

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 3
    payload = _screen_backfill_context_payload(llm.calls[1])
    serialised_screen_call = json.dumps(llm.calls[1], sort_keys=True)
    assert "internal execution-status text" not in serialised_screen_call
    assert "Execution status:" in payload["model_response"]
    assert payload["rejected_candidate_reasons"][
        "response_candidate_internal_status"
    ] is True
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
    assert "Plain response without presenter tags." in screen_text
    assert "Operational summary:" in screen_text
    assert (
        "This turn still needs follow-up before it should be treated as complete."
        in screen_text
    )
    assert (
        "The tool path did not complete the requested paper-status analysis."
        in screen_text
    )
    assert "Tool activity diagnostics:" in screen_text
    assert "search_concepts — ok" in screen_text

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert llm_debug.get("spoken_backfill_second_pass_reason") == "missing_spoken"

    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "fallback_success"
    assert (
        screen_backfill_event["source_path"] == "response_text_plus_follow_up_summary"
    )

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Generate <spoken> talk track"
    assert (
        "I ran tools for this request, but I do not have a reliable final answer yet."
        in llm.calls[0]["context"][1]["content"]
    )


def test_presenter_mode_fails_closed_without_represented_nested_progress_facts(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "0")

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "workflow_control.for_each",
                    "status": "ok",
                    "payload": {
                        "iteration_results": [
                            {
                                "completed": True,
                                "final_state": "#V#workflow_done",
                                "tool_invocations": [
                                    {
                                        "tool": "workflow_invoke_subworkflow",
                                        "status": "ok",
                                        "payload": {
                                            "result": {
                                                "last_action_id": "scholarly_paper.verify_representation",
                                                "last_action_status": "success",
                                                "last_action_outputs": {
                                                    "scholarly_representation_verified": True,
                                                    "paper_concept_id": "#V#paper_nested",
                                                    "file_copy_concept_id": "#V#file_nested",
                                                },
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
        },
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "workflow_mcp.invoke_tool",
                    "status": "ok",
                    "payload": {
                        "mcp_tool": "gmail_modify_labels",
                        "mcp_result": {
                            "success": False,
                            "error_code": "gmail_api_error",
                            "error": "Gmail modify labels failed: Profile scopes do not include gmail.modify",
                        },
                    },
                    "error": "gmail_api_error",
                }
            ),
        },
    ]
    aux_llm_calls = (
        {
            "type": "turn_completion_gate",
            "decision": "failed",
            "decision_reason": "Mutation attempt failed or was blocked.",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "blocking_failure_codes": ["required_tool_attempt_failed"],
        },
    )
    orchestrator_result = OrchestratorResult(
        response_text=(
            "I ran tools for this request, but I do not have a reliable final answer yet.\n\n"
            "The required workflow_execute-based retrieval/mutation evidence was not executed."
        ),
        extra_messages=tool_messages,
        tool_invocations=(),
        aux_llm_calls=aux_llm_calls,
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Represent recent arXiv email papers", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    screen_text = body["response_channels"]["screen"]

    assert "Nested workflow payloads included no represented progress facts" in screen_text
    assert "Representation/read-back verified" not in screen_text
    assert "#V#paper_nested" not in screen_text
    assert "`gmail_modify_labels` reported gmail_api_error" not in screen_text
    assert "Profile scopes do not include gmail.modify" not in screen_text
    assert "required workflow_execute-based" not in screen_text


def test_presenter_mode_surfaces_represented_nested_progress_facts(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "0")

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    progress_facts = [
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "represented_readback",
            "label": "Represented read-back",
            "status": "available",
            "present": True,
            "value": "#V#paper_nested",
            "value_kind": "concept_id",
            "workflow_id": "#V#paper_representation_workflow",
            "state_id": "read_back",
            "action_id": "verify_representation",
            "contract_id": "#V#represented_readback_fact",
        },
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "label_mutation_blocker",
            "label": "Label mutation blocker",
            "status": "available",
            "present": True,
            "value": "gmail_api_error: Profile scopes do not include gmail.modify",
            "value_kind": "error",
            "workflow_id": "#V#gmail_label_workflow",
            "state_id": "modify_labels",
            "action_id": "gmail_modify_labels",
            "contract_id": "#V#label_mutation_blocker_fact",
        },
    ]
    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "workflow_control.for_each",
                    "status": "ok",
                    "payload": {
                        "iteration_results": [
                            {
                                "completed": True,
                                "tool_invocations": [
                                    {
                                        "tool": "workflow_invoke_subworkflow",
                                        "status": "ok",
                                        "payload": {
                                            "result": {
                                                "last_action_outputs": {
                                                    "progress_facts": progress_facts,
                                                    "paper_concept_id": "#V#not_policy",
                                                },
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
        }
    ]
    orchestrator_result = OrchestratorResult(
        response_text=(
            "I ran tools for this request, but I do not have a reliable final answer yet."
        ),
        extra_messages=tool_messages,
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Represent recent arXiv email papers", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    screen_text = body["response_channels"]["screen"]

    assert "Represented workflow progress facts:" in screen_text
    assert "Represented read-back: `#V#paper_nested`" in screen_text
    assert "#V#represented_readback_fact" in screen_text
    assert "Label mutation blocker" in screen_text
    assert "Profile scopes do not include gmail.modify" in screen_text
    assert "#V#not_policy" not in screen_text
    assert "Representation/read-back verified" not in screen_text
    assert "`gmail_modify_labels` reported gmail_api_error" not in screen_text

    backfill_event = _find_aux_event(
        body["llm_debug"],
        "presenter_screen_backfill",
        reason_code="tool_activity_summary_fallback",
    )
    projection = backfill_event["represented_evidence_projection"]
    assert projection["contract_ids"] == [
        "#V#represented_readback_fact",
        "#V#label_mutation_blocker_fact",
    ]
    assert projection["fact_count"] == 2


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
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "success"
    assert screen_backfill_event["source_path"] == "represented_screen_prompt"

    spoken_backfill_event = _find_transformation_event(llm_debug, "spoken_backfill")
    assert spoken_backfill_event["status"] == "success"
    assert spoken_backfill_event["source_path"] == "llm_synthesis"

    assert len(llm.calls) == 3
    payload = _screen_backfill_context_payload(llm.calls[1])
    serialised_screen_call = json.dumps(llm.calls[1], sort_keys=True)
    assert "internal execution-status text" not in serialised_screen_call
    assert raw_failure_text in payload["model_response"]
    assert payload["rejected_candidate_reasons"][
        "response_candidate_internal_status"
    ] is True
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
    assert "Tool results:" in screen_text
    assert "Description updated: YES" not in screen_text
    assert "Description updated: NO" not in screen_text

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    screen_backfill_event = _find_transformation_event(llm_debug, "screen_backfill")
    assert screen_backfill_event["status"] == "failure"
    assert (
        screen_backfill_event["source_path"]
        == "represented_screen_prompt_suppressed_claim"
    )
    assert (
        screen_backfill_event["suppression_reason"]
        == "description_claim_without_tool_evidence_suppressed"
    )
    tool_dump_detector = _find_aux_event(
        llm_debug,
        "presenter_detector",
        reason_code="response_candidate_tool_dump_rejected",
    )
    assert tool_dump_detector["detector"] == "screen_tool_dump"
    assert tool_dump_detector["context"] == "response_candidate_reuse"

    assert len(llm.calls) == 2
    payload = _screen_backfill_context_payload(llm.calls[0])
    assert payload["tool_evidence_summary"]
    assert payload["rejected_candidate_reasons"]["response_candidate_tool_dump"] is True
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_presenter_mode_tool_summary_fallback_surfaces_verified_artefact_handle(
    monkeypatch,
):
    import json

    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "0")

    llm = _StubLLM("<spoken>Short talk track.</spoken>")
    app = _make_app(monkeypatch, llm)

    tool_payload = {
        "tool": "dataset_representation.verify",
        "status": "ok",
        "duration_ms": 12,
        "payload": {
            "dataset_concept_id": "#V#symmetry_dataset_representation",
            "dataset_verified": True,
            "verification_failures": [],
        },
    }
    tool_message = {"role": "tool", "content": json.dumps(tool_payload)}
    orchestrator_result = OrchestratorResult(
        response_text="<spoken>The representation is ready.</spoken>",
        extra_messages=[tool_message],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Represent this dataset", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert "Surfaceable artefact handles:" in screen_text
    assert "Verified dataset concept: #V#symmetry_dataset_representation" in screen_text
    assert "Write activity notes:" in screen_text
    assert "No write activity was detected" not in screen_text
    assert "Description updated: NO" not in screen_text


def test_follow_up_summary_suppresses_internal_status_reason_with_detector_event():
    from src.backend.server.routes.von_routes import (
        _build_presenter_follow_up_summary_from_tool_messages,
    )

    aux_llm_calls: list[dict] = []
    summary = _build_presenter_follow_up_summary_from_tool_messages(
        [],
        completion_gate={
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "decision_reason": (
                "Execution status: requested mutation was not executed. "
                "Failure codes: paper_representation_not_executed."
            ),
        },
        auxiliary_llm_calls=aux_llm_calls,
    )

    assert isinstance(summary, str)
    assert "Execution status:" not in summary
    detector_event = next(
        entry
        for entry in aux_llm_calls
        if entry.get("type") == "presenter_detector"
        and entry.get("reason_code") == "follow_up_decision_reason_suppressed"
    )
    assert detector_event["detector"] == "internal_status_diagnostic"
    assert detector_event["context"] == "follow_up_decision_reason"


def test_follow_up_summary_reconciles_completed_workflow_execution_with_open_verification():
    from src.backend.server.routes.von_routes import (
        _build_presenter_follow_up_summary_from_tool_messages,
    )

    aux_llm_calls: list[dict] = [
        {
            "type": "workflow_execution",
            "workflow_id": "#V#example_workflow",
            "execution_summary": {
                "workflow_id": "#V#example_workflow",
                "workflow_instance_id": "instance-123",
                "completed": True,
                "effective_completed": True,
                "terminal_status": "completed",
                "final_state": "#V#workflow_done",
                "action_completed_count": 11,
                "action_success_count": 11,
                "action_failure_count": 0,
                "terminal_effect_count": 0,
                "durable_side_effect_count": 0,
            },
        }
    ]

    summary = _build_presenter_follow_up_summary_from_tool_messages(
        [],
        completion_gate={
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "decision_reason": "Required mutation was not executed.",
            "blocking_failure_codes": [
                "required_tool_not_planned",
                "mutation_succeeded_readback_missing",
            ],
            "unresolved_preconditions": [
                {
                    "effect_type": "required_evidence",
                    "status": "not_executed",
                    "status_reason": "Required read-back evidence was missing.",
                }
            ],
        },
        auxiliary_llm_calls=aux_llm_calls,
    )

    assert isinstance(summary, str)
    assert "Workflow-backed outcome:" in summary
    assert "terminal status `completed`" in summary
    assert "instance `instance-123`" in summary
    assert "11 completed" in summary
    assert "Durable side effects observed: 0" in summary
    assert "Verification still needs follow-up" in summary
    assert "required_tool_not_planned" in summary
    assert "Required mutation was not executed" not in summary
    detector_event = next(
        entry
        for entry in aux_llm_calls
        if entry.get("type") == "presenter_detector"
        and entry.get("reason_code")
        == "overbroad_mutation_status_replaced_by_workflow_evidence"
    )
    assert detector_event["detector"] == "workflow_execution_reconciled"


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


def test_tool_messages_prompt_blob_includes_relation_evidence_concept_ids():
    import json

    from src.backend.server.routes.von_routes import _build_tool_messages_prompt_blob

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "find_relations_with_argument",
                    "status": "ok",
                    "payload": {
                        "total_hits": 1,
                        "hits": [
                            {
                                "source_concept_id": "#V#example_subject",
                                "source_name": "Example Subject",
                                "predicate_concept_id": "#V#example_predicate",
                                "target_concept_id": "#V#example_object",
                                "target_name": "Example Object",
                            }
                        ],
                    },
                }
            ),
        }
    ]

    blob = _build_tool_messages_prompt_blob(tool_messages)

    assert "TOOL RELATION EVIDENCE (authoritative):" in blob
    assert "source_concept_id=#V#example_subject" in blob
    assert "predicate_concept_id=#V#example_predicate" in blob
    assert "target_concept_id=#V#example_object" in blob


def test_tool_messages_prompt_blob_recovers_target_concept_id_from_preview_and_value():
    import json

    from src.backend.server.routes.von_routes import _build_tool_messages_prompt_blob

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "find_relations_with_argument",
                    "status": "ok",
                    "payload": {
                        "total_hits": 1,
                        "hits": [
                            {
                                "source_concept_id": "#V#example_subject",
                                "source_name": "Example Subject",
                                "predicate_concept_id": "#V#example_predicate",
                                "target_value": "Scholarly Paper For File Copy V Arxiv Pdf File 60060",
                                "target_value_preview": "Learning to Tell Two Spirals Apart #V#learning_to_tell_two_spirals_apart",
                                "target_name": "Learning to Tell Two Spirals Apart",
                            }
                        ],
                    },
                }
            ),
        }
    ]

    blob = _build_tool_messages_prompt_blob(tool_messages)

    assert "TOOL RELATION EVIDENCE (authoritative):" in blob
    assert "source_concept_id=#V#example_subject" in blob
    assert "predicate_concept_id=#V#example_predicate" in blob
    assert "target_concept_id=#V#learning_to_tell_two_spirals_apart" in blob


def test_tool_messages_prompt_blob_surfaces_verified_artefact_handles_first():
    import json

    from src.backend.server.routes.von_routes import _build_tool_messages_prompt_blob

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "scholarly_paper.verify_representation",
                    "status": "ok",
                    "payload": {
                        "result_snapshot": {
                            "paper_concept_id": "#V#on_the_ability_of_deep_networks_to_learn_symmetries_from_data_a_neural_kernel_theory",
                            "file_copy_concept_id": "#V#arxiv_pdf_file_c89705d1eb8849608b7f64ccfc7fb953",
                            "scholarly_representation_verified": True,
                            "verification_failures": [],
                        }
                    },
                }
            ),
        }
    ]

    blob = _build_tool_messages_prompt_blob(tool_messages)

    artefact_section = blob.index("SURFACEABLE ARTEFACT HANDLES")
    write_section = blob.index("TOOL WRITES LEDGER")
    writes_not_detected_section = blob.index("WRITES NOT DETECTED")
    assert artefact_section < write_section < writes_not_detected_section
    assert (
        "#V#on_the_ability_of_deep_networks_to_learn_symmetries_from_data_a_neural_kernel_theory"
        in blob
    )
    assert "#V#arxiv_pdf_file_c89705d1eb8849608b7f64ccfc7fb953" in blob
    assert "verified=true" in blob
    assert "verification_key=scholarly_representation_verified" in blob
    assert "Description updated: NO" not in blob
    assert "surfaceable artefact handles above" in blob


def test_tool_messages_prompt_blob_surfaces_generic_verified_concept_handle():
    import json

    from src.backend.server.routes.von_routes import _build_tool_messages_prompt_blob

    tool_messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool": "dataset_representation.verify",
                    "status": "ok",
                    "payload": {
                        "dataset_concept_id": "#V#symmetry_dataset_representation",
                        "dataset_verified": True,
                        "verification_failures": [],
                    },
                }
            ),
        }
    ]

    blob = _build_tool_messages_prompt_blob(tool_messages)

    assert "SURFACEABLE ARTEFACT HANDLES" in blob
    assert "Verified dataset concept: #V#symmetry_dataset_representation" in blob
    assert "source=dataset_concept_id" in blob


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
