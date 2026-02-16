from __future__ import annotations

from flask import Flask


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("LLM generate() should not be called in these tests")


class _StubOrchestrator:
    def __init__(self, result):
        self._result = result

    def configure_execution_caps(self, **_kwargs) -> None:
        return None

    def _extract_json_blob(self, _text: str):
        return None

    def run(self, **_kwargs):
        return self._result


class _StubTaskStatus:
    def __init__(self, *, status: str, result=None, error: str | None = None):
        self.status = status
        self.result = result
        self.error = error


class _StubTaskRegistry:
    def __init__(self, task_status):
        self._task_status = task_status

    def get_task_status(self, _task_id: str):
        return self._task_status


def _make_app(monkeypatch, orchestrator) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: _DummyLLM(),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()
    return app


def test_generate_debug_omits_render_plan_when_not_present(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Hello"})
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    debug = body.get("llm_debug")
    assert isinstance(debug, dict)
    assert "render_plan" not in debug


def test_generate_debug_includes_concept_backed_render_plan(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    render_plan = {
        "enabled": True,
        "reason": "resolved",
        "render_mode": "spoken+screen",
        "should_narrate": True,
        "request_payload_object_kind": "concept",
        "request_payload_selected_concept_id": "#V#task_123",
    }
    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        render_plan=render_plan,
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Hello"})
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    debug = body.get("llm_debug")
    assert isinstance(debug, dict)
    assert debug.get("render_plan") == render_plan


def test_generate_debug_includes_screen_only_fallback_render_plan(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    render_plan = {
        "enabled": True,
        "reason": "renderer_definition_ids_missing",
        "render_mode": "screen_only",
        "should_narrate": False,
    }
    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        render_plan=render_plan,
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Hello"})
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    debug = body.get("llm_debug")
    assert isinstance(debug, dict)
    assert debug.get("render_plan") == render_plan


def test_generate_builds_display_tables_from_render_plan_record_sets(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    render_plan = {
        "enabled": True,
        "reason": "resolved",
        "render_mode": "screen_only",
        "should_narrate": False,
        "screen_table_record_sets": [
            {
                "element_id": "screen_task_table",
                "intent": "structured_tabular_view",
                "records": [
                    {
                        "task_id": "task_alpha",
                        "task_name": "Alpha",
                        "status": "done",
                        "source": {"source_concept_id": "#V#task_alpha"},
                    }
                ],
                "columns": [
                    {
                        "column_id": "task_name",
                        "label": "Task",
                        "source_key": "task_name",
                        "data_type": "text",
                    },
                    {
                        "column_id": "status",
                        "label": "Status",
                        "source_key": "status",
                        "data_type": "text",
                    },
                ],
                "row_id_field": "task_id",
                "row_provenance_field": "source",
                "default_sort_column_id": "task_name",
            },
            {
                "element_id": "screen_predicate_extent_table",
                "intent": "structured_tabular_view",
                "records": [
                    {
                        "assertion_id": "assertion_1",
                        "subject": "#V#task_alpha",
                        "predicate": "#V#depends_on",
                        "object": "#V#task_beta",
                        "assertion_meta": {"assertion_id": "assertion_1"},
                    }
                ],
                "columns": [
                    {
                        "column_id": "subject",
                        "label": "Subject",
                        "source_key": "subject",
                        "data_type": "text",
                    },
                    {
                        "column_id": "predicate",
                        "label": "Predicate",
                        "source_key": "predicate",
                        "data_type": "text",
                    },
                    {
                        "column_id": "object",
                        "label": "Object",
                        "source_key": "object",
                        "data_type": "text",
                    },
                ],
                "row_id_field": "assertion_id",
                "row_provenance_field": "assertion_meta",
                "default_sort_column_id": "subject",
                "pagination_enabled": False,
            },
        ],
    }
    orchestrator_result = OrchestratorResult(
        response_text="Rendered summary",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        render_plan=render_plan,
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Show tables"})
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    display_elements = body.get("display_elements")
    assert isinstance(display_elements, dict)
    assert display_elements.get("validation", {}).get("valid") is True
    assert "screen_structured_tables_supplied" in display_elements.get("reason_codes", [])

    table_elements = [
        element
        for element in display_elements.get("elements", [])
        if isinstance(element, dict) and element.get("element_type") == "table"
    ]
    assert len(table_elements) == 2

    task_table = next(
        element for element in table_elements if element.get("element_id") == "screen_task_table"
    )
    assert task_table["payload"]["rows"][0]["row_id"] == "task_alpha"
    assert (
        task_table["payload"]["rows"][0]["provenance"]["source_concept_id"]
        == "#V#task_alpha"
    )

    predicate_table = next(
        element
        for element in table_elements
        if element.get("element_id") == "screen_predicate_extent_table"
    )
    assert predicate_table["payload"]["rows"][0]["row_id"] == "assertion_1"
    assert (
        predicate_table["payload"]["rows"][0]["provenance"]["assertion_id"]
        == "assertion_1"
    )


def test_generate_drops_invalid_tables_from_render_plan_record_sets(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    # columns=[] is accepted by route-level extraction but rejected by display
    # contract validation, so the supplied table should be dropped safely.
    render_plan = {
        "enabled": True,
        "reason": "resolved",
        "render_mode": "screen_only",
        "should_narrate": False,
        "screen_table_record_sets": [
            {
                "element_id": "screen_invalid_task_table",
                "intent": "structured_tabular_view",
                "records": [
                    {
                        "task_id": "task_alpha",
                        "task_name": "Alpha",
                        "status": "done",
                        "source": {"source_concept_id": "#V#task_alpha"},
                    }
                ],
                "columns": [],
                "row_id_field": "task_id",
                "row_provenance_field": "source",
            }
        ],
    }
    orchestrator_result = OrchestratorResult(
        response_text="Rendered summary",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        render_plan=render_plan,
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Show tables"})
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    display_elements = body.get("display_elements")
    assert isinstance(display_elements, dict)
    assert display_elements.get("validation", {}).get("valid") is True
    assert "screen_structured_tables_invalid_dropped" in display_elements.get(
        "reason_codes", []
    )
    assert "screen_structured_tables_supplied" not in display_elements.get(
        "reason_codes", []
    )

    table_elements = [
        element
        for element in display_elements.get("elements", [])
        if isinstance(element, dict) and element.get("element_type") == "table"
    ]
    assert not table_elements


def test_task_result_includes_render_plan_when_present(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    render_plan = {
        "enabled": True,
        "reason": "resolved",
        "render_mode": "spoken+screen",
        "should_narrate": True,
    }
    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        render_plan=render_plan,
    )
    registry = _StubTaskRegistry(
        _StubTaskStatus(status="completed", result=orchestrator_result)
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.background_task_registry",
        registry,
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.get("/von/api/task/result/task-1")
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    result = body.get("result")
    assert isinstance(result, dict)
    assert result.get("render_plan") == render_plan


def test_task_result_omits_render_plan_when_absent(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    registry = _StubTaskRegistry(
        _StubTaskStatus(status="completed", result=orchestrator_result)
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.background_task_registry",
        registry,
    )
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result))

    client = app.test_client()
    response = client.get("/von/api/task/result/task-2")
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    result = body.get("result")
    assert isinstance(result, dict)
    assert "render_plan" not in result
