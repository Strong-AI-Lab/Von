from __future__ import annotations

import importlib
import sys
import types

import pytest


@pytest.fixture()
def app_client(monkeypatch):
    # Must be set before importing mongo_client so USE_MOCK_DB is computed correctly.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    import src.backend.db.mongo_client as mongo_client

    importlib.reload(mongo_client)

    # Stub Google auth dependencies pulled in by utils_flask -> auth_routes imports.
    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

    class _DummyFlow:
        def __init__(self, *args, **kwargs):
            self.credentials = types.SimpleNamespace(id_token="dummy-token")
            self.redirect_uri = kwargs.get("redirect_uri")
            self.client_config = {"web": {"redirect_uris": [self.redirect_uri]}}

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls(**kwargs)

        def authorization_url(self, *args, **kwargs):
            return "https://auth.example", "state-token"

        def fetch_token(self, *args, **kwargs):
            return None

    fake_flow_module.Flow = _DummyFlow  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {  # type: ignore[attr-defined]
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials  # type: ignore[attr-defined]

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials  # type: ignore[attr-defined]

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module  # type: ignore[attr-defined]
    fake_oauth2_package.credentials = fake_credentials_module  # type: ignore[attr-defined]
    fake_oauth2_package.service_account = fake_service_account_module  # type: ignore[attr-defined]

    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module

    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield client


def test_workflow_execution_routes_roundtrip(app_client):
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.trace_store import insert_workflow_execution_trace

    trace = WorkflowExecutionTrace(workflow_id="#V#chat_assistant_workflow")
    trace.user_namespace = "#V#unit_test_user"
    trace.start_step("llm.generate", inputs={"prompt": "hi"}).finish_success(
        {"response_preview": "hello"}
    )
    trace.finish_completed()

    insert_workflow_execution_trace(trace.to_storage_document())

    resp = app_client.get(f"/api/workflows/executions/{trace.execution_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["execution_id"] == trace.execution_id

    recent = app_client.get("/api/workflows/executions/recent?limit=5")
    assert recent.status_code == 200
    payload = recent.get_json()
    assert payload["count"] >= 1
    assert any(
        item.get("execution_id") == trace.execution_id for item in payload["items"]
    )


def test_workflow_definition_endpoint_uses_best_effort_text(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    def _fake_find_one(filter, projection=None):
        if filter.get("concept_id") == "#V#demo_workflow":
            return {
                "concept_id": "#V#demo_workflow",
                "name": "Demo workflow",
                "relationships": {
                    "hasInitialStep": ["#V#demo_step_1"],
                    "hasStep": ["#V#demo_step_1", "#V#demo_step_2", "#V#demo_step_3"],
                },
            }
        return None

    def _fake_find(filter, projection=None, sort=None, skip=0, limit=0):
        ids = filter.get("concept_id", {}).get("$in", [])
        docs = []
        for cid in ids:
            if cid == "#V#demo_step_1":
                docs.append(
                    {
                        "concept_id": "#V#demo_step_1",
                        "name": "Step 1",
                        "relationships": {
                            "invokesAction": ["#V#demo_action"],
                            "hasPrecondition": ["#V#demo_condition_1"],
                            "onTrueNextStep": ["#V#demo_step_2"],
                            "onFalseNextStep": ["#V#demo_step_3"],
                        },
                    }
                )
            if cid == "#V#demo_step_2":
                docs.append(
                    {
                        "concept_id": "#V#demo_step_2",
                        "name": "Step 2",
                        "relationships": {
                            "invokesAction": ["#V#demo_action_2"],
                        },
                    }
                )
            if cid == "#V#demo_step_3":
                docs.append(
                    {
                        "concept_id": "#V#demo_step_3",
                        "name": "Step 3",
                        "relationships": {
                            "invokesAction": ["#V#demo_action_3"],
                        },
                    }
                )
        return docs

    monkeypatch.setattr(workflows_routes.ConceptsRepository, "find_one", _fake_find_one)
    monkeypatch.setattr(workflows_routes.ConceptsRepository, "find", _fake_find)
    monkeypatch.setattr(
        workflows_routes,
        "resolve_workflow_narrative_text",
        lambda workflow_id: (None, "none"),
    )

    resp = app_client.get("/api/workflows/definitions/%23V%23demo_workflow")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["workflow_id"] == "#V#demo_workflow"
    assert data["definition"]["representation"] == "vontology_process_graph_v1"
    assert data["definition"]["initial_step"] == "#V#demo_step_1"
    assert any(
        step["step_id"] == "#V#demo_step_1" for step in data["definition"]["steps"]
    )
    step_1 = next(
        step
        for step in data["definition"]["steps"]
        if step["step_id"] == "#V#demo_step_1"
    )
    assert step_1["preconditions"] == ["#V#demo_condition_1"]
    assert step_1["control_flow"]["on_true"] == "#V#demo_step_2"
    assert step_1["control_flow"]["on_false"] == "#V#demo_step_3"
    assert any(
        edge["predicate"] == "onTrueNextStep" for edge in data["definition"]["edges"]
    )
    assert any(
        edge["predicate"] == "onFalseNextStep" for edge in data["definition"]["edges"]
    )
    assert data["raw"] is None
    assert data["raw_source"] == "none"


def test_workflow_definitions_list_endpoint_reads_registry(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory
    import src.backend.services.workflow_discovery_service as workflow_discovery_service

    class _FakeDefinition:
        def __init__(self, initial_state: str, purpose: str):
            self.initial_state = initial_state
            self.purpose = purpose

    class _FakeRegistration:
        def __init__(self, definition, purpose: str, source: str):
            self.definition = definition
            self.purpose = purpose
            self.source = source

    class _FakeRegistry:
        def __init__(self):
            self._ids = ["#V#alpha_workflow", "#V#beta_workflow"]
            self._definitions = {
                "#V#alpha_workflow": _FakeDefinition(
                    initial_state="alpha_start",
                    purpose="Alpha fallback purpose",
                ),
                "#V#beta_workflow": _FakeDefinition(
                    initial_state="beta_start",
                    purpose="Beta fallback purpose",
                ),
            }
            self._registrations = {
                "#V#alpha_workflow": _FakeRegistration(
                    definition=self._definitions["#V#alpha_workflow"],
                    purpose="Alpha explicit purpose",
                    source="built_in",
                ),
                "#V#beta_workflow": _FakeRegistration(
                    definition=self._definitions["#V#beta_workflow"],
                    purpose="",
                    source="vontology",
                ),
            }

        def all_workflow_ids(self):
            return list(self._ids)

        def get(self, workflow_id):
            return self._definitions.get(workflow_id)

        def get_registration(self, workflow_id):
            return self._registrations.get(workflow_id)

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda: _FakeRegistry(),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_workflow_registry_inventory_snapshot",
        lambda: {
            "counts": {"registry": 2, "vontology_discovered": 3},
            "vontology_only_workflow_ids": ["#V#salient_predicate_governance_workflow"],
            "diagnostics": {
                "drift_detected": True,
                "reason_codes": ["vontology_only"],
            },
        },
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_usage_aggregates_for_workflows",
        lambda workflow_ids: {
            "#V#alpha_workflow": {
                "attempts": 8,
                "completions": 6,
                "completion_rate": 0.75,
                "last_episode_at": "2026-02-13T10:22:00+00:00",
            },
            "#V#beta_workflow": {
                "attempts": 2,
                "completions": 1,
                "completion_rate": 0.5,
                "last_episode_at": "2026-02-13T11:00:00+00:00",
            },
        },
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_episode_counts_for_workflows",
        lambda workflow_ids, namespace=None, session_id=None, turn_id=None: {
            "#V#alpha_workflow": 3,
            "#V#beta_workflow": 0,
        },
    )
    classification_map = {
        "#V#alpha_workflow": (
            True,
            "executable_now",
            None,
        ),
        "#V#beta_workflow": (
            False,
            "workflow_step_partially_vacuous",
            (
                "workflow_step_contract_issue:"
                "count=2,total=3,first_step=#V#alpha_step"
            ),
        ),
    }

    def _fake_classify_workflow_executability(workflow_id: str):
        return classification_map.get(
            workflow_id,
            (False, "non_executable_design_artifact", None),
        )

    monkeypatch.setattr(
        workflow_discovery_service,
        "classify_workflow_concept_executability",
        _fake_classify_workflow_executability,
    )

    resp = app_client.get("/api/workflows/definitions?limit=10")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["count"] == 2
    assert payload["total"] == 2
    assert payload["episodes_scope"] == {
        "namespace": None,
        "session_id": None,
        "turn_id": None,
    }
    assert payload["parity_inventory"]["counts"]["registry"] == 2
    assert "#V#salient_predicate_governance_workflow" in payload["parity_inventory"]["vontology_only_workflow_ids"]

    items = payload["items"]
    assert items[0]["workflow_id"] == "#V#alpha_workflow"
    assert items[0]["description"] == "Alpha explicit purpose"
    assert items[0]["description_source"] == "registration.purpose"
    assert items[0]["initial_state"] == "alpha_start"
    assert items[0]["source"] == "built_in"
    assert items[0]["attempts"] == 8
    assert items[0]["completions"] == 6
    assert items[0]["completion_rate"] == pytest.approx(0.75)
    assert items[0]["last_episode_at"] == "2026-02-13T10:22:00+00:00"
    assert items[0]["episodes_count"] == 3
    assert items[0]["is_executable"] is True
    assert items[0]["executability_reason"] == "executable_now"
    assert items[0]["executability_detail"] is None

    assert items[1]["workflow_id"] == "#V#beta_workflow"
    # Falls back to definition.purpose when registration purpose is empty.
    assert items[1]["description"] == "Beta fallback purpose"
    assert items[1]["description_source"] == "definition.purpose"
    assert items[1]["initial_state"] == "beta_start"
    assert items[1]["source"] == "vontology"
    assert items[1]["attempts"] == 2
    assert items[1]["completions"] == 1
    assert items[1]["completion_rate"] == pytest.approx(0.5)
    assert items[1]["last_episode_at"] == "2026-02-13T11:00:00+00:00"
    assert items[1]["episodes_count"] == 0
    assert items[1]["is_executable"] is False
    assert items[1]["executability_reason"] == "workflow_step_partially_vacuous"
    assert (
        items[1]["executability_detail"]
        == "workflow_step_contract_issue:count=2,total=3,first_step=#V#alpha_step"
    )


def test_workflow_definitions_list_includes_relation_description_source(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory
    import src.backend.services.workflow_discovery_service as workflow_discovery_service

    class _FakeDefinition:
        def __init__(self, initial_state: str, purpose: str):
            self.initial_state = initial_state
            self.purpose = purpose

    class _FakeRegistration:
        def __init__(self, definition, purpose: str, source: str):
            self.definition = definition
            self.purpose = purpose
            self.source = source

    class _FakeRegistry:
        def __init__(self):
            self._ids = ["#V#legacy_workflow"]
            self._definitions = {
                "#V#legacy_workflow": _FakeDefinition(
                    initial_state="legacy_start",
                    purpose="",
                )
            }
            self._registrations = {
                "#V#legacy_workflow": _FakeRegistration(
                    definition=self._definitions["#V#legacy_workflow"],
                    purpose="",
                    source="vontology",
                )
            }

        def all_workflow_ids(self):
            return list(self._ids)

        def get(self, workflow_id):
            return self._definitions.get(workflow_id)

        def get_registration(self, workflow_id):
            return self._registrations.get(workflow_id)

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda: _FakeRegistry(),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_workflow_registry_inventory_snapshot",
        lambda: {},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_usage_aggregates_for_workflows",
        lambda workflow_ids: {},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_episode_counts_for_workflows",
        lambda workflow_ids, namespace=None, session_id=None, turn_id=None: {
            "#V#legacy_workflow": 0,
        },
    )
    monkeypatch.setattr(
        workflow_discovery_service,
        "classify_workflow_concept_executability",
        lambda workflow_id: (False, "non_executable_design_artifact", None),
    )
    monkeypatch.setattr(
        workflows_routes,
        "resolve_workflow_description",
        lambda workflow_id, **kwargs: (
            "Workflow description from relation",
            "text_relation:hasDescription",
        ),
    )

    resp = app_client.get("/api/workflows/definitions?limit=10")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["workflow_id"] == "#V#legacy_workflow"
    assert item["description"] == "Workflow description from relation"
    assert item["description_source"] == "text_relation:hasDescription"


def test_workflow_episodes_list_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    expected_items = [
        {
            "episode_id": "wfep_1",
            "workflow_id": "#V#alpha_workflow",
            "completed": False,
            "terminal_stage": "tool_execution",
            "termination_reason": {
                "code": "tool_timeout",
                "detail": "Tool invocation exceeded timeout",
            },
            "turn_id": "turn-123",
            "attempt_started_at": "2026-02-13T12:00:00+00:00",
        },
        {
            "episode_id": "wfep_2",
            "workflow_id": "#V#alpha_workflow",
            "completed": True,
            "terminal_stage": "completed",
            "termination_reason": {"code": "completed", "detail": None},
            "turn_id": "turn-124",
            "attempt_started_at": "2026-02-13T12:05:00+00:00",
        },
    ]

    def _fake_list_workflow_use_episodes(**kwargs):
        assert kwargs["workflow_id"] == "#V#alpha_workflow"
        assert kwargs["namespace"] == "#V#user/#V#org"
        assert kwargs["session_id"] == "session-1"
        assert kwargs["turn_id"] == "turn-123"
        assert kwargs["limit"] == 25
        return expected_items

    def _fake_count_workflow_use_episodes(**kwargs):
        assert kwargs["workflow_id"] == "#V#alpha_workflow"
        assert kwargs["namespace"] == "#V#user/#V#org"
        assert kwargs["session_id"] == "session-1"
        assert kwargs["turn_id"] == "turn-123"
        return 3

    monkeypatch.setattr(
        workflows_routes,
        "list_workflow_use_episodes",
        _fake_list_workflow_use_episodes,
    )
    monkeypatch.setattr(
        workflows_routes,
        "count_workflow_use_episodes",
        _fake_count_workflow_use_episodes,
    )

    resp = app_client.get(
        (
            "/api/workflows/episodes?"
            "workflow_id=%23V%23alpha_workflow&namespace=%23V%23user/%23V%23org"
            "&session_id=session-1&turn_id=turn-123&limit=25"
        )
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["count"] == 2
    assert payload["total"] == 3
    assert payload["has_more"] is True
    assert payload["items"] == expected_items
    assert payload["filters"]["workflow_id"] == "#V#alpha_workflow"
    assert payload["filters"]["namespace"] == "#V#user/#V#org"
    assert payload["filters"]["session_id"] == "session-1"
    assert payload["filters"]["turn_id"] == "turn-123"


def test_create_workflow_instance_route_rejects_unrunnable_workflow(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    manager = object()
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    def _fake_submit_verified_workflow_instance(**kwargs):
        assert kwargs.get("manager") is manager
        assert kwargs.get("workflow_id") == "#V#non_runnable_workflow"
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id="#V#non_runnable_workflow",
            status="rejected_preflight",
            instance_id=None,
            error_code="workflow_not_runnable",
            error="workflow not runnable",
            verification={
                "preflight": {"errors": ["workflow_definition_not_registered"]},
            },
        )

    monkeypatch.setattr(
        workflows_routes,
        "submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )

    response = app_client.post(
        "/api/workflows/instances",
        json={
            "workflow_id": "#V#non_runnable_workflow",
            "user_id": "user-1",
            "org_id": "org-1",
            "namespace": "user-1/org-1",
            "inputs": {"seed": "value"},
        },
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["status"] == "rejected_preflight"
    assert payload["error_code"] == "workflow_not_runnable"
    assert "workflow_definition_not_registered" in payload["verification"]["preflight"][
        "errors"
    ]


def test_trigger_workflow_schedule_route_rejects_unrunnable_workflow(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    schedule = types.SimpleNamespace(
        schedule_id="#V#schedule_test_1",
        workflow_id="#V#non_runnable_workflow",
        user_id="user-1",
        org_id="org-1",
        namespace="user-1/org-1",
        default_inputs={"seed": "value"},
    )

    manager = types.SimpleNamespace(get_schedule=lambda _: schedule)
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    def _fake_submit_verified_workflow_instance(**kwargs):
        assert kwargs.get("manager") is manager
        assert kwargs.get("workflow_id") == "#V#non_runnable_workflow"
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id="#V#non_runnable_workflow",
            status="rejected_preflight",
            instance_id=None,
            error_code="workflow_not_runnable",
            error="workflow not runnable",
            verification={
                "preflight": {"errors": ["workflow_definition_not_registered"]},
            },
        )

    monkeypatch.setattr(
        workflows_routes,
        "submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )

    response = app_client.post(
        "/api/workflows/schedules/%23V%23schedule_test_1/trigger",
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["schedule_id"] == "#V#schedule_test_1"
    assert payload["status"] == "rejected_preflight"
    assert payload["error_code"] == "workflow_not_runnable"
    assert "workflow_definition_not_registered" in payload["verification"]["preflight"][
        "errors"
    ]
