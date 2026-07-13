from __future__ import annotations

import importlib
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from pymongo.errors import PyMongoError


@pytest.fixture()
def app_client(monkeypatch):
    # Must be set before importing mongo_client so USE_MOCK_DB is computed correctly.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS", "0")

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


@pytest.fixture(autouse=True)
def _clear_workflow_capability_index_status_cache():
    try:
        import src.backend.server.routes.workflows_routes as workflows_routes

        with workflows_routes._WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_LOCK:
            workflows_routes._WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.clear()
    except Exception:
        pass
    yield
    try:
        import src.backend.server.routes.workflows_routes as workflows_routes

        with workflows_routes._WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_LOCK:
            workflows_routes._WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.clear()
    except Exception:
        pass


def _allow_synthetic_workflow_access(monkeypatch) -> None:
    """Keep synthetic route fixtures explicit about their access authority."""

    import src.backend.workflows.workflow_listing_service as listing_service

    monkeypatch.setattr(
        listing_service,
        "filter_accessible_concept_ids",
        lambda workflow_ids: set(workflow_ids),
    )


def _authenticate_workflow_studio_client(
    app_client,
    *,
    user_id: str = "#V#studio_author",
    org_id: str = "#V#studio_org",
) -> None:
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = user_id
        flask_session["organisation_concept_id"] = org_id


def test_workflow_execution_routes_roundtrip(monkeypatch, app_client):
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.trace_store import insert_workflow_execution_trace
    import src.backend.server.routes.workflows_routes as workflows_routes

    trace = WorkflowExecutionTrace(workflow_id="#V#chat_assistant_workflow")
    trace.user_namespace = "#V#unit_test_user@unit_test_org"
    trace.org_id = "#V#unit_test_org"
    trace.start_step("llm.generate", inputs={"prompt": "hi"}).finish_success(
        {"response_preview": "hello"}
    )
    trace.finish_completed()

    insert_workflow_execution_trace(trace.to_storage_document())
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: [
            item for item in workflow_ids if isinstance(item, str)
        ],
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#unit_test_user"
        flask_session["organisation_concept_id"] = "#V#unit_test_org"

    resp = app_client.get(f"/api/workflows/executions/{trace.execution_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["execution_id"] == trace.execution_id

    recent = app_client.get(
        "/api/workflows/executions/recent",
        query_string={"limit": 5, "workflow_id": "#V#chat_assistant_workflow"},
    )
    assert recent.status_code == 200
    payload = recent.get_json()
    assert payload["count"] >= 1
    assert any(
        item.get("execution_id") == trace.execution_id for item in payload["items"]
    )


def test_workflow_execution_routes_conceal_cross_org_traces(
    monkeypatch,
    app_client,
):
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.trace_store import insert_workflow_execution_trace
    import src.backend.server.routes.workflows_routes as workflows_routes

    workflow_id = "#V#actor_scoped_trace_workflow"
    own_trace = WorkflowExecutionTrace(workflow_id=workflow_id)
    own_trace.user_namespace = "#V#same_user@org_a"
    own_trace.org_id = "#V#org_a"
    own_trace.finish_completed()
    foreign_trace = WorkflowExecutionTrace(workflow_id=workflow_id)
    foreign_trace.user_namespace = "#V#same_user@org_b"
    foreign_trace.org_id = "#V#org_b"
    foreign_trace.finish_completed()
    insert_workflow_execution_trace(own_trace.to_storage_document())
    insert_workflow_execution_trace(foreign_trace.to_storage_document())

    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#same_user"
        flask_session["organisation_concept_id"] = "#V#org_a"

    own = app_client.get(f"/api/workflows/executions/{own_trace.execution_id}")
    assert own.status_code == 200
    foreign = app_client.get(
        f"/api/workflows/executions/{foreign_trace.execution_id}"
    )
    assert foreign.status_code == 404
    assert foreign.get_json() == {
        "error": "workflow_execution_not_found",
        "execution_id": foreign_trace.execution_id,
    }

    forged = app_client.get(
        "/api/workflows/executions/recent",
        query_string={"namespace": "#V#same_user/#V#org_b"},
    )
    assert forged.status_code == 403
    recent = app_client.get(
        "/api/workflows/executions/recent",
        query_string={"limit": 20, "workflow_id": workflow_id},
    )
    assert recent.status_code == 200
    execution_ids = {
        item.get("execution_id") for item in recent.get_json()["items"]
    }
    assert own_trace.execution_id in execution_ids
    assert foreign_trace.execution_id not in execution_ids


def test_workflow_prediction_forces_ambient_actor_namespace(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    calls: list[dict[str, object]] = []

    def _fake_build_workflow_prediction_envelope(**kwargs):
        calls.append(dict(kwargs))
        return {"success": True, "workflow_id": kwargs["workflow_id"]}

    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_prediction_envelope",
        _fake_build_workflow_prediction_envelope,
    )
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#prediction_user"
        flask_session["organisation_concept_id"] = "#V#prediction_org"

    forged = app_client.get(
        "/api/workflows/predictions/envelope",
        query_string={
            "workflow_id": "#V#prediction_workflow",
            "namespace": "#V#prediction_user/#V#foreign_org",
        },
    )
    assert forged.status_code == 403
    assert calls == []

    response = app_client.get(
        "/api/workflows/predictions/envelope",
        query_string={"workflow_id": "#V#prediction_workflow"},
    )
    assert response.status_code == 200
    assert calls == [
        {
            "workflow_id": "#V#prediction_workflow",
            "namespace": "#V#prediction_user@prediction_org",
            "model": None,
            "provider": None,
            "limit": 50,
        }
    ]


def test_workflow_definition_endpoint_uses_best_effort_text(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    monkeypatch.setattr(
        workflows_routes,
        "resolve_workflow_definition_from_authority",
        lambda *_args, **_kwargs: types.SimpleNamespace(definition=object()),
    )
    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_process_graph",
        lambda workflow_id: (
            {
                "representation": "vontology_process_graph_v1",
                "initial_step": "#V#demo_step_1",
                "steps": [
                    {
                        "step_id": "#V#demo_step_1",
                        "preconditions": ["#V#demo_condition_1"],
                        "control_flow": {
                            "on_true": "#V#demo_step_2",
                            "on_false": "#V#demo_step_3",
                        },
                    },
                    {"step_id": "#V#demo_step_2"},
                    {"step_id": "#V#demo_step_3"},
                ],
                "edges": [
                    {"predicate": "onTrueNextStep"},
                    {"predicate": "onFalseNextStep"},
                ],
            },
            [],
        ),
    )
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
    assert data["definition_identity"]["schema_version"] == "workflow_definition_identity.v1"
    assert data["definition_identity"]["source"] == "vontology"
    assert data["definition_identity"]["definition_hash"]
    assert data["raw"] is None
    assert data["raw_source"] == "none"


def test_workflow_definition_endpoint_conceals_hidden_existing_workflow(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda _workflow_ids: [],
    )
    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_process_graph",
        lambda _workflow_id: (_ for _ in ()).throw(
            AssertionError("hidden workflow graph must not be loaded")
        ),
    )
    monkeypatch.setattr(
        workflows_routes,
        "resolve_workflow_narrative_text",
        lambda _workflow_id: (_ for _ in ()).throw(
            AssertionError("hidden workflow narrative must not be loaded")
        ),
    )

    response = app_client.get(
        "/api/workflows/definitions/%23V%23hidden_existing_workflow"
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "workflow_definition_not_found"


def test_workflow_definition_endpoint_conceals_visible_root_with_hidden_child(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    monkeypatch.setattr(
        workflows_routes,
        "resolve_workflow_definition_from_authority",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            definition=None,
            error_code="workflow_definition_not_loadable_for_actor",
        ),
    )
    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_process_graph",
        lambda _workflow_id: (_ for _ in ()).throw(
            AssertionError("raw graph must not load after hidden-child denial")
        ),
    )
    monkeypatch.setattr(
        workflows_routes,
        "resolve_workflow_narrative_text",
        lambda _workflow_id: (_ for _ in ()).throw(
            AssertionError("narrative must not reveal a denied graph")
        ),
    )

    response = app_client.get(
        "/api/workflows/definitions/%23V%23visible_root_hidden_child"
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "workflow_definition_not_found",
        "workflow_id": "#V#visible_root_hidden_child",
    }


def test_workflow_definitions_list_endpoint_reads_registry(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory
    import src.backend.services.workflow_discovery_service as workflow_discovery_service

    _allow_synthetic_workflow_access(monkeypatch)

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

        def peek_registration(self, workflow_id):
            return self.get_registration(workflow_id)

        def get_registration_source(self, workflow_id, *, resolve_lazy: bool = False):
            registration = self.get_registration(workflow_id)
            return getattr(registration, "source", None)

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda **_kwargs: _FakeRegistry(),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_or_build_workflow_registry_inventory_snapshot",
        lambda *args, **kwargs: {
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
        lambda workflow_ids, **_kwargs: {
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
        lambda workflow_ids, **_kwargs: {
            "#V#alpha_workflow": 3,
            "#V#beta_workflow": 0,
        },
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_capability_index_readiness_report",
        lambda: {
            "ready": False,
            "status": "error",
            "summary": "#V#hidden_workflow",
            "detail": "Last build failed for #V#hidden_workflow",
            "last_error": "failed to index #V#hidden_workflow",
            "last_invalidation_reason": (
                "workflow_routing_text_relation_changed:#V#hidden_workflow"
            ),
            "last_manifest_path": "/private/#V#hidden_workflow.json",
            "size": 41,
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

    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#catalogue_user"
        flask_session["organisation_concept_id"] = "#V#catalogue_org"

    resp = app_client.get("/api/workflows/definitions?limit=10")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["count"] == 2
    assert payload["total"] == 2
    assert payload["episodes_scope"] == {
        "namespace": "#V#catalogue_user@catalogue_org",
        "session_id": None,
        "turn_id": None,
    }
    assert payload["capability_index"]["ready"] is False
    assert payload["capability_index"]["status"] == "error"
    assert payload["capability_index"]["visible_workflow_count"] == 2
    assert payload["capability_index"]["workflow_count_scope"] == "actor_visible"
    assert "size" not in payload["capability_index"]
    assert "last_error" not in payload["capability_index"]
    assert "last_invalidation_reason" not in payload["capability_index"]
    assert "last_manifest_path" not in payload["capability_index"]
    assert "#V#hidden_workflow" not in resp.get_data(as_text=True)
    assert payload["parity_inventory"]["counts"]["registry"] == 2
    assert "#V#salient_predicate_governance_workflow" in payload["parity_inventory"]["vontology_only_workflow_ids"]

    items = payload["items"]
    assert items[0]["workflow_id"] == "#V#alpha_workflow"
    assert items[0]["description"] == "Alpha explicit purpose"
    assert items[0]["description_source"] == "registration.purpose"
    assert items[0]["initial_state"] == "alpha_start"
    assert items[0]["source"] == "built_in"
    assert items[0]["definition_identity"]["schema_version"] == "workflow_definition_identity.v1"
    assert items[0]["definition_identity"]["source"] == "built_in"
    assert items[0]["definition_identity"]["definition_hash"]
    assert items[0]["attempts"] == 8
    assert items[0]["completions"] == 6
    assert items[0]["completion_rate"] == 0.75
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
    assert items[1]["definition_identity"]["source"] == "vontology"
    assert items[1]["definition_identity"]["definition_hash"]
    assert items[1]["attempts"] == 2
    assert items[1]["completions"] == 1
    assert items[1]["completion_rate"] == 0.5
    assert items[1]["last_episode_at"] == "2026-02-13T11:00:00+00:00"
    assert items[1]["episodes_count"] == 0
    assert items[1]["is_executable"] is False
    assert items[1]["executability_reason"] == "workflow_step_partially_vacuous"
    assert (
        items[1]["executability_detail"]
        == "workflow_step_contract_issue:count=2,total=3,first_step=#V#alpha_step"
    )


def test_workflow_capability_index_status_endpoint_returns_readiness_report(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(
        workflows_routes,
        "_actor_visible_workflow_count",
        lambda: 3,
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_capability_index_readiness_report",
        lambda: {
            "ready": False,
            "status": "timeout",
            "summary": "Workflow capability index still not ready after startup check.",
            "detail": "Workflow discovery is waiting on the authoritative capability index to finish building.",
            "size": 0,
            "build_in_progress": True,
        },
    )

    resp = app_client.get("/api/workflows/capability-index/status")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ready"] is False
    assert payload["status"] == "timeout"
    assert payload["build_in_progress"] is True
    assert payload["visible_workflow_count"] == 3
    assert payload["workflow_count_scope"] == "actor_visible"
    assert "size" not in payload
    assert payload["cache"]["state"] == "computed"


def test_workflow_capability_index_status_endpoint_caches_repeated_reads(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    with workflows_routes._WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_LOCK:
        workflows_routes._WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.clear()
    monkeypatch.setenv("VON_WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(
        workflows_routes,
        "_actor_visible_workflow_count",
        lambda: 2,
    )
    calls = {"count": 0}

    def _fake_report():
        calls["count"] += 1
        return {
            "ready": calls["count"] >= 2,
            "status": "building" if calls["count"] == 1 else "ready",
            "summary": "Workflow capability index status.",
        }

    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_capability_index_readiness_report",
        _fake_report,
    )

    first = app_client.get("/api/workflows/capability-index/status")
    second = app_client.get("/api/workflows/capability-index/status")
    bypass = app_client.get("/api/workflows/capability-index/status?nocache=1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert bypass.status_code == 200
    assert calls["count"] == 2
    first_payload = first.get_json()
    second_payload = second.get_json()
    bypass_payload = bypass.get_json()
    assert first_payload["status"] == "building"
    assert first_payload["cache"]["state"] == "computed"
    assert second_payload["status"] == "building"
    assert second_payload["cache"]["state"] == "fresh"
    assert bypass_payload["status"] == "ready"
    assert bypass_payload["cache"]["state"] == "computed"


def test_workflow_capability_index_status_redacts_global_hidden_diagnostics(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    hidden_workflow_id = "#V#hidden_sail_workflow"
    monkeypatch.setattr(
        workflows_routes,
        "_actor_visible_workflow_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_capability_index_readiness_report",
        lambda: {
            "ready": False,
            "status": "error",
            "size": 41,
            "last_built_size": 41,
            "last_invalidation_reason": (
                f"workflow_routing_text_relation_changed:{hidden_workflow_id}"
            ),
            "last_error": f"failed to index {hidden_workflow_id}",
            "query_surface_last_error": hidden_workflow_id,
            "last_manifest_path": f"/private/{hidden_workflow_id}",
            "last_manifest_detail": hidden_workflow_id,
            "summary": hidden_workflow_id,
            "detail": hidden_workflow_id,
            "checked_at_utc": hidden_workflow_id,
        },
    )

    response = app_client.get("/api/workflows/capability-index/status?nocache=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "error"
    assert payload["visible_workflow_count"] == 1
    assert payload["visible_workflow_count_available"] is True
    assert payload["checked_at_utc"] is None
    assert hidden_workflow_id not in response.get_data(as_text=True)
    assert "last_invalidation_reason" not in payload
    assert "last_error" not in payload
    assert "last_manifest_path" not in payload
    assert "size" not in payload


def test_workflow_definitions_list_includes_relation_description_source(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory
    import src.backend.services.workflow_discovery_service as workflow_discovery_service

    _allow_synthetic_workflow_access(monkeypatch)

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

        def peek_registration(self, workflow_id):
            return self.get_registration(workflow_id)

        def get_registration_source(self, workflow_id, *, resolve_lazy: bool = False):
            registration = self.get_registration(workflow_id)
            return getattr(registration, "source", None)

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda **_kwargs: _FakeRegistry(),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_or_build_workflow_registry_inventory_snapshot",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_usage_aggregates_for_workflows",
        lambda workflow_ids, **_kwargs: {},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_episode_counts_for_workflows",
        lambda workflow_ids, **_kwargs: {
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
        "build_workflow_listing_entry",
        lambda **_kwargs: {
            "workflow_id": "#V#legacy_workflow",
            "description": "Workflow description from relation",
            "description_source": "text_relation:hasDescription",
            "description_quality": {
                "quality_label": "retrieval_ready",
                "score": 7,
            },
            "initial_state": "legacy_start",
            "source": "vontology",
            "background_launch_policy": None,
            "background_launch_policy_source": "none",
            "definition_identity": {"source": "vontology"},
            "definition_loaded": True,
        },
    )

    resp = app_client.get("/api/workflows/definitions?limit=10")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["workflow_id"] == "#V#legacy_workflow"
    assert item["description"] == "Workflow description from relation"
    assert item["description_source"] == "text_relation:hasDescription"
    assert item["definition_identity"]["source"] == "vontology"


def test_workflow_episodes_list_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory

    class _Registry:
        @staticmethod
        def all_workflow_ids():
            return ["#V#alpha_workflow"]

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda **_kwargs: _Registry(),
    )
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#user"
        flask_session["organisation_concept_id"] = "#V#org"

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
        assert kwargs["workflow_ids"] == ["#V#alpha_workflow"]
        assert kwargs["namespace"] == "#V#user@org"
        assert kwargs["session_id"] == "session-1"
        assert kwargs["turn_id"] == "turn-123"
        assert kwargs["limit"] == 25
        return expected_items

    def _fake_count_workflow_use_episodes(**kwargs):
        assert kwargs["workflow_id"] == "#V#alpha_workflow"
        assert kwargs["workflow_ids"] == ["#V#alpha_workflow"]
        assert kwargs["namespace"] == "#V#user@org"
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
    assert payload["filters"]["namespace"] == "#V#user@org"
    assert payload["filters"]["session_id"] == "session-1"
    assert payload["filters"]["turn_id"] == "turn-123"


def test_workflow_episodes_list_endpoint_returns_structured_diagnostics_on_failure(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory

    class _Registry:
        @staticmethod
        def all_workflow_ids():
            return ["#V#alpha_workflow"]

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda **_kwargs: _Registry(),
    )
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#user"
        flask_session["organisation_concept_id"] = "#V#org"

    def _boom(**_kwargs):
        raise RuntimeError("mongodb timeout while reading workflow episodes")

    monkeypatch.setattr(
        workflows_routes,
        "list_workflow_use_episodes",
        _boom,
    )

    response = app_client.get(
        (
            "/api/workflows/episodes?"
            "workflow_id=%23V%23alpha_workflow&namespace=%23V%23user/%23V%23org"
            "&session_id=session-1&turn_id=turn-123&limit=25"
        )
    )

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"] == "workflow_episodes_fetch_failed"
    assert payload["error_type"] == "RuntimeError"
    assert payload["filters"]["workflow_id"] == "#V#alpha_workflow"
    assert payload["filters"]["namespace"] == "#V#user@org"
    assert payload["filters"]["session_id"] == "session-1"
    assert payload["filters"]["turn_id"] == "turn-123"
    assert payload["diagnostics"]["namespace"] == "#V#user@org"
    assert "mongodb timeout" in payload["diagnostics"]["backend_reason"]


def test_workflow_episodes_reject_forged_actor_scope_before_store_read(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        workflows_routes,
        "list_workflow_use_episodes",
        lambda **kwargs: calls.append(dict(kwargs)) or [],
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#episode_user"
        flask_session["organisation_concept_id"] = "#V#org_a"

    response = app_client.get(
        "/api/workflows/episodes",
        query_string={
            "workflow_id": "#V#restricted_workflow",
            "namespace": "#V#episode_user/#V#org_b",
        },
    )
    assert response.status_code == 403
    assert calls == []


def test_workflow_instance_stream_always_binds_ambient_actor(
    monkeypatch,
    app_client,
):
    import src.backend.services.durable_workflow_stream_service as stream_module
    import src.backend.server.routes.workflows_routes as workflows_routes

    captured: list[dict[str, object]] = []

    class _StreamService:
        def subscribe(self, **kwargs):
            captured.append(dict(kwargs))
            return object()

        @staticmethod
        def generate_events(_subscriber):
            yield "data: {}\n\n"

        @staticmethod
        def unsubscribe(_subscriber):
            return None

    monkeypatch.setattr(
        stream_module,
        "get_workflow_stream_service",
        lambda: _StreamService(),
    )
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    unauthenticated = app_client.get("/api/workflows/instances/stream")
    assert unauthenticated.status_code == 403
    assert captured == []

    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#stream_user"
        flask_session["organisation_concept_id"] = "#V#stream_org"

    forged = app_client.get(
        "/api/workflows/instances/stream",
        query_string={"org_id": "#V#foreign_org"},
    )
    assert forged.status_code == 403
    assert captured == []

    response = app_client.get(
        "/api/workflows/instances/stream",
        query_string={
            "workflow_id": "#V#stream_workflow",
            "status": "running,completed",
        },
    )
    assert response.status_code == 200
    assert captured == [
        {
            "user_id": "#V#stream_user",
            "org_id": "#V#stream_org",
            "namespace": "#V#stream_user@stream_org",
            "workflow_id": "#V#stream_workflow",
            "instance_id": None,
            "statuses": {"running", "completed"},
            "allowed_workflow_ids": {"#V#stream_workflow"},
        }
    ]


def test_revoked_workflow_visibility_hides_trace_prediction_episode_and_stream_surfaces(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.services.durable_workflow_stream_service as stream_module
    import src.backend.workflows.durable.registry_factory as registry_factory

    workflow_id = "#V#revoked_workflow"
    trace = {
        "execution_id": "trace-revoked",
        "workflow_id": workflow_id,
        "user_namespace": "#V#actor@org",
        "user_id": "#V#actor",
        "org_id": "#V#org",
    }
    prediction_calls: list[object] = []
    episode_calls: list[dict[str, object]] = []
    stream_calls: list[object] = []

    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda _workflow_ids: [],
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_execution_trace",
        lambda _execution_id: dict(trace),
    )
    monkeypatch.setattr(
        workflows_routes,
        "list_recent_workflow_execution_traces",
        lambda **_kwargs: [dict(trace)],
    )
    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_prediction_envelope",
        lambda **_kwargs: prediction_calls.append(object()),
    )
    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda **_kwargs: types.SimpleNamespace(
            all_workflow_ids=lambda: [workflow_id]
        ),
    )
    monkeypatch.setattr(
        workflows_routes,
        "list_workflow_use_episodes",
        lambda **kwargs: episode_calls.append(dict(kwargs)) or [],
    )
    monkeypatch.setattr(
        workflows_routes,
        "count_workflow_use_episodes",
        lambda **kwargs: episode_calls.append(dict(kwargs)) or 0,
    )
    monkeypatch.setattr(
        stream_module,
        "get_workflow_stream_service",
        lambda: stream_calls.append(object()),
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#actor"
        flask_session["organisation_concept_id"] = "#V#org"

    exact = app_client.get("/api/workflows/executions/trace-revoked")
    recent = app_client.get("/api/workflows/executions/recent")
    prediction = app_client.get(
        "/api/workflows/predictions/envelope",
        query_string={"workflow_id": workflow_id},
    )
    episodes = app_client.get(
        "/api/workflows/episodes",
        query_string={"workflow_id": workflow_id},
    )
    stream = app_client.get(
        "/api/workflows/instances/stream",
        query_string={"workflow_id": workflow_id},
    )

    assert exact.status_code == 404
    assert recent.get_json() == {"items": [], "count": 0}
    assert prediction.status_code == 404
    assert prediction_calls == []
    assert episodes.status_code == 200
    assert episodes.get_json()["items"] == []
    assert all(call["workflow_ids"] == [] for call in episode_calls)
    assert stream.status_code == 404
    assert stream_calls == []


def test_create_workflow_instance_route_rejects_unrunnable_workflow(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    manager = object()
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

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

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.post(
        "/api/workflows/instances",
        json={
            "workflow_id": "#V#non_runnable_workflow",
            "user_id": "#V#user_1",
            "org_id": "#V#org_1",
            "namespace": "#V#user_1/#V#org_1",
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


def test_create_workflow_instance_rejects_body_actor_impersonation(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    def _unexpected_submission(**_kwargs):
        raise AssertionError("forged actor must be rejected before submission")

    monkeypatch.setattr(
        workflows_routes,
        "submit_verified_workflow_instance",
        _unexpected_submission,
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#outsider"

    response = app_client.post(
        "/api/workflows/instances",
        json={
            "workflow_id": "#V#restricted_workflow",
            "user_id": "#V#trusted_member",
            "org_id": "#V#trusted_org",
            "namespace": "#V#trusted_member@trusted_org",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == "workflow_actor_scope_mismatch"


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
        user_id="#V#user_1",
        org_id="#V#org_1",
        namespace="#V#user_1@org_1",
        default_inputs={"seed": "value"},
    )

    manager = types.SimpleNamespace(get_schedule=lambda _: schedule)
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

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

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

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


def test_create_workflow_schedule_route_canonicalises_legacy_namespace(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    captured: dict[str, object] = {}

    def _create_schedule(schedule):
        captured["schedule"] = schedule
        return schedule.schedule_id

    manager = types.SimpleNamespace(create_schedule=_create_schedule)
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.post(
        "/api/workflows/schedules",
        json={
            "workflow_id": "#V#alpha_workflow",
            "user_id": "#V#user_1",
            "org_id": "#V#org_1",
            "namespace": "#V#user_1/#V#org_1",
            "schedule_type": "interval",
            "interval_seconds": 60,
            "default_inputs": {"seed": "value"},
        },
    )

    assert response.status_code == 201
    schedule = captured["schedule"]
    assert getattr(schedule, "namespace", None) == "#V#user_1@org_1"


def test_create_workflow_schedule_rejects_body_actor_impersonation(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    manager = types.SimpleNamespace(
        create_schedule=lambda _schedule: (_ for _ in ()).throw(
            AssertionError("forged actor must be rejected before persistence")
        )
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#outsider"
        session["organisation_concept_id"] = "#V#outsider_org"

    response = app_client.post(
        "/api/workflows/schedules",
        json={
            "workflow_id": "#V#restricted_workflow",
            "user_id": "#V#trusted_member",
            "org_id": "#V#trusted_org",
            "namespace": "#V#trusted_member@trusted_org",
            "schedule_type": "interval",
            "interval_seconds": 60,
        },
    )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == "workflow_actor_scope_mismatch"


def test_list_workflow_schedules_filters_to_exact_request_actor(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    own_schedule = types.SimpleNamespace(
        schedule_id="schedule-own",
        workflow_id="#V#visible_workflow",
        user_id="#V#user_1",
        org_id="#V#org_1",
        namespace="#V#user_1@org_1",
        to_status_dict=lambda: {"schedule_id": "schedule-own"},
    )
    foreign_schedule = types.SimpleNamespace(
        schedule_id="schedule-foreign",
        workflow_id="#V#visible_workflow",
        user_id="#V#other_user",
        org_id="#V#other_org",
        namespace="#V#other_user@other_org",
        to_status_dict=lambda: {"schedule_id": "schedule-foreign"},
    )

    def _list_schedules(**kwargs):
        assert kwargs["user_id"] == "#V#user_1"
        return [own_schedule, foreign_schedule]

    manager = types.SimpleNamespace(list_schedules=_list_schedules)
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.get("/api/workflows/schedules")

    assert response.status_code == 200
    assert response.get_json() == {
        "items": [{"schedule_id": "schedule-own"}],
        "count": 1,
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/workflows/schedules/schedule-foreign", None),
        (
            "PUT",
            "/api/workflows/schedules/schedule-foreign/enabled",
            {"enabled": False},
        ),
        ("DELETE", "/api/workflows/schedules/schedule-foreign", None),
        ("POST", "/api/workflows/schedules/schedule-foreign/trigger", None),
    ],
)
def test_workflow_schedule_routes_hide_other_actor_schedule(
    monkeypatch, app_client, method, path, body
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    foreign_schedule = types.SimpleNamespace(
        schedule_id="schedule-foreign",
        workflow_id="#V#restricted_workflow",
        user_id="#V#other_user",
        org_id="#V#other_org",
        namespace="#V#other_user@other_org",
        default_inputs={},
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("foreign schedule must not be read, mutated, or triggered")

    manager = types.SimpleNamespace(
        get_schedule=lambda _schedule_id: foreign_schedule,
        set_schedule_enabled=_unexpected,
        delete_schedule=_unexpected,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "submit_verified_workflow_instance",
        _unexpected,
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.open(path, method=method, json=body)

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "schedule_not_found",
        "schedule_id": "schedule-foreign",
    }


@pytest.mark.parametrize(
    ("persisted_org_id", "persisted_namespace"),
    [
        ("#V#org_a", "#V#same_user@org_a"),
        (None, None),
    ],
)
def test_direct_schedule_id_requires_complete_exact_actor_envelope(
    monkeypatch,
    app_client,
    persisted_org_id,
    persisted_namespace,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    schedule = types.SimpleNamespace(
        schedule_id="schedule-cross-org",
        workflow_id="#V#visible_workflow",
        user_id="#V#same_user",
        org_id=persisted_org_id,
        namespace=persisted_namespace,
    )
    manager = types.SimpleNamespace(
        get_schedule=lambda _schedule_id: schedule,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#same_user"
        session["organisation_concept_id"] = "#V#org_b"

    response = app_client.get("/api/workflows/schedules/schedule-cross-org")

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "schedule_not_found",
        "schedule_id": "schedule-cross-org",
    }


def test_list_workflow_instances_returns_retryable_degraded_payload_for_mongo_timeout(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    manager = types.SimpleNamespace(
        list_instance_status_dicts=lambda **kwargs: (_ for _ in ()).throw(
            PyMongoError("server selection timeout while reading workflow_instances")
        )
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.get("/api/workflows/instances")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["error"] == "Workflow monitor temporarily unavailable; please retry."


def test_get_workflow_instance_returns_retryable_degraded_payload_for_mongo_timeout(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    manager = types.SimpleNamespace(
        get_instance=lambda _instance_id: (_ for _ in ()).throw(
            PyMongoError("server selection timeout while reading workflow_instances")
        )
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    response = app_client.get("/api/workflows/instances/inst-timeout")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["instance_id"] == "inst-timeout"
    assert payload["error"] == "Workflow instance temporarily unavailable; please retry."


def test_retry_workflow_instance_returns_retryable_degraded_payload_for_mongo_timeout(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    manager = types.SimpleNamespace(
        get_instance=lambda _instance_id: (_ for _ in ()).throw(
            PyMongoError("server selection timeout while reading workflow_instances")
        )
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    response = app_client.post("/api/workflows/instances/inst-timeout/retry")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["instance_id"] == "inst-timeout"
    assert payload["error"] == "Workflow retry temporarily unavailable; please retry."


def test_list_workflow_instances_accepts_comma_separated_status_filters(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    captured: dict[str, object] = {}

    def _list_instance_status_dicts(**kwargs):
        captured.update(kwargs)
        return [
            {
                "instance_id": "inst-1",
                "workflow_id": "#V#test_workflow",
                "status": "running",
                "current_state": "step_a",
                "step_index": 1,
                "created_at": "2026-03-20T00:00:00+00:00",
                "started_at": "2026-03-20T00:00:01+00:00",
                "completed_at": None,
                "progress": {
                    "current": 1,
                    "total": 3,
                    "message": "running",
                    "updated_at": None,
                },
                "error": None,
                "has_outputs": False,
                "retry_count": 0,
                "max_retries": 3,
                "source_event_type": None,
                "source_event_id": None,
                "event_idempotency_key": None,
            }
        ]

    manager = types.SimpleNamespace(list_instance_status_dicts=_list_instance_status_dicts)
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.get(
        "/api/workflows/instances",
        query_string={
            "namespace": "#V#user_1@org_1",
            "status": "pending,running,paused",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["items"][0]["instance_id"] == "inst-1"
    assert captured["namespace"] == "#V#user_1@org_1"
    statuses = cast(list[workflows_routes.WorkflowInstanceStatus], captured["status"])
    assert [status.value for status in statuses] == [
        "pending",
        "running",
        "paused",
    ]


def test_list_workflow_instances_keeps_running_rows_visible_during_pending_backlog(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
    from src.backend.workflows.durable.models import WorkflowInstanceStatus

    manager = WorkflowInstanceManager()
    user_id = "#V#user_monitor"
    org_id = "#V#org_1"
    namespace = "#V#user_monitor@org_1"

    running_alpha = manager.create_instance(
        "#V#alpha_workflow",
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
    )
    pending_alpha_old = manager.create_instance(
        "#V#alpha_workflow",
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
    )
    pending_alpha_new = manager.create_instance(
        "#V#alpha_workflow",
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
    )
    running_beta = manager.create_instance(
        "#V#beta_workflow",
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
    )

    collection = manager._get_instances_collection()
    assert collection is not None
    now = datetime.now(timezone.utc)
    collection.update_one(
        {"instance_id": running_alpha},
        {
            "$set": {
                "status": WorkflowInstanceStatus.RUNNING.value,
                "created_at": now - timedelta(minutes=4),
                "started_at": now - timedelta(minutes=4),
                "progress_message": "running alpha",
            }
        },
    )
    collection.update_one(
        {"instance_id": pending_alpha_old},
        {
            "$set": {
                "created_at": now - timedelta(minutes=2),
                "progress_message": "queued alpha old",
            }
        },
    )
    collection.update_one(
        {"instance_id": pending_alpha_new},
        {
            "$set": {
                "created_at": now - timedelta(minutes=1),
                "progress_message": "queued alpha new",
            }
        },
    )
    collection.update_one(
        {"instance_id": running_beta},
        {
            "$set": {
                "status": WorkflowInstanceStatus.RUNNING.value,
                "created_at": now - timedelta(minutes=3),
                "started_at": now - timedelta(minutes=3),
                "progress_message": "running beta",
            }
        },
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = user_id
        session["organisation_concept_id"] = org_id

    response = app_client.get(
        "/api/workflows/instances",
        query_string={
            "user_id": user_id,
            "status": "pending,running,paused",
            "limit": 3,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 3
    alpha_statuses = {
        item["status"]
        for item in payload["items"]
        if item["workflow_id"] == "#V#alpha_workflow"
    }
    assert WorkflowInstanceStatus.RUNNING.value in alpha_statuses


def test_list_workflow_instances_rejects_invalid_comma_separated_status_filters(
    app_client,
):
    response = app_client.get(
        "/api/workflows/instances",
        query_string={"status": "running,not_a_real_status"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert "Invalid status" in payload["error"]


def test_cancel_workflow_instance_route_authorises_before_mutation(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    instance = types.SimpleNamespace(
        workflow_id="#V#visible_workflow",
        user_id="#V#user_1",
        org_id="#V#org_1",
        namespace="#V#user_1@org_1",
        status=types.SimpleNamespace(is_terminal=lambda: False),
    )
    manager = types.SimpleNamespace(
        mark_cancelled=lambda instance_id: instance_id == "inst-1",
        get_instance=lambda _instance_id: instance,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.post("/api/workflows/instances/inst-1/cancel")

    assert response.status_code == 200
    assert response.get_json() == {"status": "cancelled", "instance_id": "inst-1"}


def test_cancel_workflow_instance_returns_retryable_degraded_payload_for_timeout(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    instance = types.SimpleNamespace(
        workflow_id="#V#visible_workflow",
        user_id="#V#user_1",
        org_id="#V#org_1",
        namespace="#V#user_1@org_1",
    )
    manager = types.SimpleNamespace(
        get_instance=lambda _instance_id: instance,
        mark_cancelled=lambda _instance_id: (_ for _ in ()).throw(
            PyMongoError("timed out while waiting for majority write acknowledgement")
        )
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.post("/api/workflows/instances/inst-timeout/cancel")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["instance_id"] == "inst-timeout"
    assert payload["operation"] == "cancel"
    assert (
        payload["error"]
        == "Workflow cancellation temporarily unavailable; please retry."
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/workflows/instances/instance-foreign"),
        ("POST", "/api/workflows/instances/instance-foreign/cancel"),
        ("POST", "/api/workflows/instances/instance-foreign/retry"),
        ("POST", "/api/workflows/instances/instance-foreign/pause"),
    ],
)
def test_workflow_instance_routes_hide_other_actor_instance(
    monkeypatch,
    app_client,
    method,
    path,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    foreign_instance = types.SimpleNamespace(
        instance_id="instance-foreign",
        user_id="#V#other_user",
        org_id="#V#other_org",
        namespace="#V#other_user@other_org",
        status=workflows_routes.WorkflowInstanceStatus.RUNNING,
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("foreign workflow instance must not be mutated")

    manager = types.SimpleNamespace(
        get_instance=lambda _instance_id: foreign_instance,
        mark_cancelled=_unexpected,
        reset_for_retry=_unexpected,
        pause_instance=_unexpected,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#user_1"
        session["organisation_concept_id"] = "#V#org_1"

    response = app_client.open(path, method=method)

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "instance_not_found",
        "instance_id": "instance-foreign",
    }


@pytest.mark.parametrize(
    ("persisted_org_id", "persisted_namespace"),
    [
        ("#V#org_a", "#V#same_user@org_a"),
        (None, None),
    ],
)
def test_direct_instance_id_requires_complete_exact_actor_envelope(
    monkeypatch,
    app_client,
    persisted_org_id,
    persisted_namespace,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    instance = types.SimpleNamespace(
        instance_id="instance-cross-org",
        workflow_id="#V#visible_workflow",
        user_id="#V#same_user",
        org_id=persisted_org_id,
        namespace=persisted_namespace,
    )
    manager = types.SimpleNamespace(
        get_instance=lambda _instance_id: instance,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)

    with app_client.session_transaction() as session:
        session["user_concept_id"] = "#V#same_user"
        session["organisation_concept_id"] = "#V#org_b"

    response = app_client.get("/api/workflows/instances/instance-cross-org")

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "instance_not_found",
        "instance_id": "instance-cross-org",
    }


def test_revoked_workflow_visibility_hides_same_actor_http_instance_history(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    instance = types.SimpleNamespace(
        instance_id="instance-revoked",
        workflow_id="#V#revoked_workflow",
        user_id="#V#actor",
        org_id="#V#org",
        namespace="#V#actor@org",
        status=workflows_routes.WorkflowInstanceStatus.RUNNING,
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("revoked instance must not be mutated")

    manager = types.SimpleNamespace(
        list_instance_status_dicts=lambda **_kwargs: [
            {
                "instance_id": instance.instance_id,
                "workflow_id": instance.workflow_id,
                "status": "running",
            }
        ],
        get_instance=lambda _instance_id: instance,
        mark_cancelled=_unexpected,
        reset_for_retry=_unexpected,
        pause_instance=_unexpected,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda _workflow_ids: [],
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#actor"
        flask_session["organisation_concept_id"] = "#V#org"

    listed = app_client.get("/api/workflows/instances")
    responses = [
        app_client.get("/api/workflows/instances/instance-revoked"),
        app_client.post("/api/workflows/instances/instance-revoked/cancel"),
        app_client.post("/api/workflows/instances/instance-revoked/retry"),
        app_client.post("/api/workflows/instances/instance-revoked/pause"),
    ]

    assert listed.get_json() == {"items": [], "count": 0}
    assert all(response.status_code == 404 for response in responses)
    assert all("revoked_workflow" not in response.get_data(as_text=True) for response in responses)


def test_revoked_workflow_visibility_hides_same_actor_http_schedules(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    schedule = types.SimpleNamespace(
        schedule_id="schedule-revoked",
        workflow_id="#V#revoked_workflow",
        user_id="#V#actor",
        org_id="#V#org",
        namespace="#V#actor@org",
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("revoked schedule must not be mutated or triggered")

    manager = types.SimpleNamespace(
        list_schedules=lambda **_kwargs: [schedule],
        get_schedule=lambda _schedule_id: schedule,
        set_schedule_enabled=_unexpected,
        delete_schedule=_unexpected,
    )
    monkeypatch.setattr(workflows_routes, "_get_instance_manager", lambda: manager)
    monkeypatch.setattr(
        workflows_routes,
        "filter_workflow_ids_for_current_actor",
        lambda _workflow_ids: [],
    )
    monkeypatch.setattr(
        workflows_routes,
        "submit_verified_workflow_instance",
        _unexpected,
    )
    with app_client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#actor"
        flask_session["organisation_concept_id"] = "#V#org"

    listed = app_client.get("/api/workflows/schedules")
    responses = [
        app_client.get("/api/workflows/schedules/schedule-revoked"),
        app_client.put(
            "/api/workflows/schedules/schedule-revoked/enabled",
            json={"enabled": False},
        ),
        app_client.delete("/api/workflows/schedules/schedule-revoked"),
        app_client.post("/api/workflows/schedules/schedule-revoked/trigger"),
    ]

    assert listed.get_json() == {"items": [], "count": 0}
    assert all(response.status_code == 404 for response in responses)
    assert all("revoked_workflow" not in response.get_data(as_text=True) for response in responses)


def test_workflow_definitions_list_uses_short_ttl_cache(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.services.workflow_discovery_service as workflow_discovery_service
    import src.backend.workflows.durable.registry_factory as registry_factory

    _allow_synthetic_workflow_access(monkeypatch)

    class _FakeDefinition:
        initial_state = "start"
        purpose = "purpose"

    class _FakeRegistration:
        def __init__(self):
            self.definition = _FakeDefinition()
            self.purpose = "purpose"
            self.source = "built_in"

    class _FakeRegistry:
        def all_workflow_ids(self):
            return ["#V#cached_workflow"]

        def get(self, workflow_id):
            return _FakeDefinition()

        def get_registration(self, workflow_id):
            return _FakeRegistration()

        def peek_registration(self, workflow_id):
            return self.get_registration(workflow_id)

        def get_registration_source(self, workflow_id, *, resolve_lazy: bool = False):
            return "built_in"

    calls = {"build_registry": 0}

    def _fake_build_registry(**_kwargs):
        calls["build_registry"] += 1
        return _FakeRegistry()

    monkeypatch.setenv("VON_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        _fake_build_registry,
    )
    monkeypatch.setattr(
        registry_factory,
        "get_or_build_workflow_registry_inventory_snapshot",
        lambda *args, **kwargs: {"counts": {"registry": 1, "vontology_discovered": 1}},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_usage_aggregates_for_workflows",
        lambda workflow_ids, **_kwargs: {},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_episode_counts_for_workflows",
        lambda workflow_ids, **_kwargs: {
            "#V#cached_workflow": 0
        },
    )
    monkeypatch.setattr(
        workflow_discovery_service,
        "classify_workflow_concept_executability",
        lambda workflow_id: (True, "executable_now", None),
    )

    with workflows_routes._WORKFLOW_DEFINITIONS_CACHE_LOCK:
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE.clear()

    resp1 = app_client.get("/api/workflows/definitions?limit=10")
    assert resp1.status_code == 200
    resp2 = app_client.get("/api/workflows/definitions?limit=10")
    assert resp2.status_code == 200

    assert calls["build_registry"] == 1
    payload = resp2.get_json()
    assert payload["count"] == 1
    assert payload["items"][0]["workflow_id"] == "#V#cached_workflow"


def test_workflow_definitions_list_nocache_bypasses_cache(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.services.workflow_discovery_service as workflow_discovery_service
    import src.backend.workflows.durable.registry_factory as registry_factory

    class _FakeDefinition:
        initial_state = "start"
        purpose = "purpose"

    class _FakeRegistration:
        def __init__(self):
            self.definition = _FakeDefinition()
            self.purpose = "purpose"
            self.source = "built_in"

    class _FakeRegistry:
        def all_workflow_ids(self):
            return ["#V#cached_workflow"]

        def get(self, workflow_id):
            return _FakeDefinition()

        def get_registration(self, workflow_id):
            return _FakeRegistration()

        def peek_registration(self, workflow_id):
            return self.get_registration(workflow_id)

        def get_registration_source(self, workflow_id, *, resolve_lazy: bool = False):
            return "built_in"

    calls = {"build_registry": 0}

    def _fake_build_registry(**_kwargs):
        calls["build_registry"] += 1
        return _FakeRegistry()

    monkeypatch.setenv("VON_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        _fake_build_registry,
    )
    monkeypatch.setattr(
        registry_factory,
        "get_or_build_workflow_registry_inventory_snapshot",
        lambda *args, **kwargs: {"counts": {"registry": 1, "vontology_discovered": 1}},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_usage_aggregates_for_workflows",
        lambda workflow_ids, **_kwargs: {},
    )
    monkeypatch.setattr(
        workflows_routes,
        "get_workflow_episode_counts_for_workflows",
        lambda workflow_ids, **_kwargs: {
            "#V#cached_workflow": 0
        },
    )
    monkeypatch.setattr(
        workflow_discovery_service,
        "classify_workflow_concept_executability",
        lambda workflow_id: (True, "executable_now", None),
    )

    with workflows_routes._WORKFLOW_DEFINITIONS_CACHE_LOCK:
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE.clear()

    resp1 = app_client.get("/api/workflows/definitions?limit=10")
    assert resp1.status_code == 200
    resp2 = app_client.get("/api/workflows/definitions?limit=10&nocache=true")
    assert resp2.status_code == 200

    assert calls["build_registry"] == 2


def test_workflow_definitions_list_serves_stale_when_refresh_in_progress(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory

    cache_key = (10, None, None, None, "user:anon|org:none")
    stale_payload = {
        "items": [
            {
                "workflow_id": "#V#stale_workflow",
                "description": "stale",
                "description_source": "cache",
                "initial_state": "start",
                "source": "built_in",
                "definition_identity": None,
                "attempts": 0,
                "completions": 0,
                "completion_rate": None,
                "last_episode_at": None,
                "episodes_count": 0,
                "is_executable": True,
                "executability_reason": "executable_now",
                "executability_detail": None,
            }
        ],
        "count": 1,
        "total": 1,
        "episodes_scope": {"namespace": None, "session_id": None, "turn_id": None},
        "parity_inventory": {},
    }

    monkeypatch.setenv("VON_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS", "60")

    with workflows_routes._WORKFLOW_DEFINITIONS_CACHE_LOCK:
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE.clear()
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE[cache_key] = {
            "expires_at_monotonic": time.monotonic() - 1.0,
            "stored_at_monotonic": time.monotonic() - 5.0,
            "payload": stale_payload,
        }

    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda: (_ for _ in ()).throw(AssertionError("Should not rebuild registry")),
    )

    lock = workflows_routes._get_workflow_definitions_refresh_lock(cache_key)
    acquired = lock.acquire(blocking=False)
    assert acquired is True
    try:
        resp = app_client.get("/api/workflows/definitions?limit=10")
    finally:
        lock.release()

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["count"] == 1
    assert payload["items"][0]["workflow_id"] == "#V#stale_workflow"
    assert payload["cache"]["state"] == "stale"
    assert payload["cache"]["refresh_in_progress"] is True
    assert float(payload["cache"]["retry_after_seconds"]) > 0.0


def test_cached_workflow_definitions_are_reprojected_for_ambient_actor(
    monkeypatch,
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    from src.backend.security import access_control

    visible_id = "#V#visible_workflow"
    restricted_id = "#V#restricted_workflow"

    class _Collection:
        def find(self, query, _projection):
            requested_ids = set(query["concept_id"]["$in"])
            documents = {
                visible_id: {"concept_id": visible_id, "relationships": {}},
                restricted_id: {
                    "concept_id": restricted_id,
                    "relationships": {
                        "#V#specific_to_user": ["#V#other_actor"]
                    },
                },
            }
            return [documents[item] for item in requested_ids if item in documents]

    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    cached_payload = {
        "_introspection_workflow_ids": [visible_id, restricted_id],
        "items": [
            {"workflow_id": visible_id, "description": "visible"},
            {"workflow_id": restricted_id, "description": "restricted metadata"},
        ],
        "count": 2,
        "total": 2,
        "parity_inventory": {
            "registry_workflow_ids": [visible_id, restricted_id],
        },
    }

    with access_control.override_current_actor("#V#current_actor", "#V#team_org"):
        projected = workflows_routes._attach_workflow_definitions_cache_metadata(
            cached_payload,
            state="fresh",
            age_seconds=0.0,
            refresh_in_progress=False,
        )

    assert projected["items"] == [
        {"workflow_id": visible_id, "description": "visible"}
    ]
    assert projected["count"] == 1
    assert projected["total"] == 1
    assert restricted_id not in str(projected)
    assert "restricted metadata" not in str(projected)
    assert "_introspection_workflow_ids" not in projected


def test_workflow_definitions_list_serves_expired_cache_and_starts_background_refresh(
    monkeypatch, app_client
):
    import src.backend.server.routes.workflows_routes as workflows_routes
    import src.backend.workflows.durable.registry_factory as registry_factory

    cache_key = (10, None, None, None, "user:anon|org:none")
    stale_payload = {
        "items": [
            {
                "workflow_id": "#V#stale_workflow",
                "description": "stale",
                "description_source": "cache",
                "initial_state": "start",
                "source": "built_in",
                "definition_identity": None,
                "attempts": 0,
                "completions": 0,
                "completion_rate": None,
                "last_episode_at": None,
                "episodes_count": 0,
                "is_executable": True,
                "executability_reason": "executable_now",
                "executability_detail": None,
            }
        ],
        "count": 1,
        "total": 1,
        "episodes_scope": {"namespace": None, "session_id": None, "turn_id": None},
        "parity_inventory": {},
    }

    refresh_calls: list[dict[str, object]] = []

    monkeypatch.setenv("VON_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(
        registry_factory,
        "build_durable_workflow_registry_read_only",
        lambda: (_ for _ in ()).throw(
            AssertionError("Should not rebuild registry in foreground")
        ),
    )
    def _fake_start_background_refresh(**kwargs):
        refresh_calls.append(kwargs)
        refresh_lock = kwargs.get("refresh_lock")
        if refresh_lock is not None:
            refresh_lock.release()
        return True

    monkeypatch.setattr(
        workflows_routes,
        "_start_workflow_definitions_background_refresh",
        _fake_start_background_refresh,
    )

    with workflows_routes._WORKFLOW_DEFINITIONS_CACHE_LOCK:
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE.clear()
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE[cache_key] = {
            "expires_at_monotonic": time.monotonic() - 1.0,
            "stored_at_monotonic": time.monotonic() - 5.0,
            "payload": stale_payload,
        }

    resp = app_client.get("/api/workflows/definitions?limit=10")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["items"][0]["workflow_id"] == "#V#stale_workflow"
    assert payload["cache"]["state"] == "stale"
    assert payload["cache"]["refresh_in_progress"] is True
    assert float(payload["cache"]["retry_after_seconds"]) > 0.0
    assert refresh_calls
    assert refresh_calls[0]["cache_key"] == cache_key


def test_workflow_definitions_list_refresh_in_progress_without_stale_returns_503(
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    cache_key = (10, None, None, None, "user:anon|org:none")
    with workflows_routes._WORKFLOW_DEFINITIONS_CACHE_LOCK:
        workflows_routes._WORKFLOW_DEFINITIONS_CACHE.clear()

    lock = workflows_routes._get_workflow_definitions_refresh_lock(cache_key)
    acquired = lock.acquire(blocking=False)
    assert acquired is True
    try:
        resp = app_client.get("/api/workflows/definitions?limit=10")
    finally:
        lock.release()

    assert resp.status_code == 503
    payload = resp.get_json()
    assert payload["error"] == "workflow_definitions_refresh_in_progress"
    assert payload["retryable"] is True
    assert float(payload["retry_after_seconds"]) > 0.0
    assert resp.headers.get("Retry-After") == "1"


def test_workflow_studio_catalogue_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    seen: dict[str, object] = {}

    def _fake_catalogue(**kwargs):
        seen.update(kwargs)
        return {
            "items": [
                {
                    "workflow_id": "#V#alpha_workflow",
                    "description": "Alpha workflow",
                    "source": "vontology",
                    "is_executable": True,
                }
            ],
            "count": 1,
            "total": 1,
            "episodes_scope": {"namespace": None, "session_id": None, "turn_id": None},
            "parity_inventory": {},
        }

    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_catalogue_payload",
        _fake_catalogue,
    )

    resp = app_client.get("/api/workflow-studio/catalogue?include_designs=false")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["items"][0]["workflow_id"] == "#V#alpha_workflow"
    assert payload["studio"]["independent_surface"] is True
    assert seen["include_designs"] is False


def test_workflow_studio_detail_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)
    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_studio_detail_payload",
        lambda *_args, **_kwargs: {
            "workflow_id": "#V#alpha_workflow",
            "summary": {
                "workflow_id": "#V#alpha_workflow",
                "source": "vontology",
                "description": "Alpha",
                "definition_identity": {"definition_hash": "hash"},
            },
            "authority": {"authoritative_store": "vontology"},
            "views": {
                "topology": {
                    "definition": {
                        "workflow_id": "#V#alpha_workflow",
                        "initial_step": "start",
                        "steps": [{"step_id": "start", "name": "Start"}],
                        "edges": [],
                    }
                }
            },
            "operations": {"instances": {"items": [], "active_count": 0}},
            "authoring": {"available": False},
            "raw": None,
            "raw_source": "none",
            "warnings": [],
        },
    )

    resp = app_client.get("/api/workflow-studio/workflows/%23V%23alpha_workflow")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["workflow_id"] == "#V#alpha_workflow"
    assert payload["authority"]["authoritative_store"] == "vontology"
    assert payload["views"]["topology"]["definition"]["initial_step"] == "start"


def test_workflow_studio_detail_hides_actor_incomplete_definition(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)

    def _raise_actor_authority_error(*_args, **_kwargs):
        raise workflows_routes.WorkflowStudioAuthorityError(
            "workflow_definition_not_loadable_for_actor"
        )

    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_studio_detail_payload",
        _raise_actor_authority_error,
    )

    resp = app_client.get("/api/workflow-studio/workflows/%23V%23alpha_workflow")

    assert resp.status_code == 404
    assert resp.get_json() == {
        "error": "workflow_definition_not_found",
        "workflow_id": "#V#alpha_workflow",
    }


def test_workflow_studio_authoring_preview_conflict_returns_409(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)

    def _raise_conflict(*_args, **_kwargs):
        raise workflows_routes.WorkflowStudioConflictError(
            "workflow_definition_hash_conflict"
        )

    monkeypatch.setattr(
        workflows_routes,
        "preview_workflow_authoring_spec",
        _raise_conflict,
    )

    resp = app_client.post(
        "/api/workflow-studio/workflows/%23V%23alpha_workflow/authoring/preview",
        json={"authoring_spec": {"workflow_id": "#V#alpha_workflow", "steps": []}},
    )

    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload["error"] == "workflow_definition_hash_conflict"


def test_workflow_studio_authoring_apply_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)

    monkeypatch.setattr(
        workflows_routes,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: {
            "workflow_id": "#V#alpha_workflow",
            "publication": {"summary": {"workflows_published": 1}},
            "preview": {"contract_validation": {"valid": True}},
        },
    )
    _authenticate_workflow_studio_client(app_client)

    resp = app_client.post(
        "/api/workflow-studio/workflows/%23V%23alpha_workflow/authoring/apply",
        json={
            "authoring_spec": {
                "workflow_id": "#V#alpha_workflow",
                "steps": [{"state_id": "start"}],
            }
        },
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["publication"]["summary"]["workflows_published"] == 1


def test_workflow_studio_authoring_proposal_submit_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)

    captured: dict[str, object] = {}

    _authenticate_workflow_studio_client(
        app_client,
        user_id="#V#test_user",
        org_id="#V#test_org",
    )
    monkeypatch.setattr(
        workflows_routes,
        "submit_workflow_authoring_proposal",
        lambda workflow_id, **kwargs: (
            captured.update({"workflow_id": workflow_id, **kwargs})
            or {
                "success": True,
                "proposal": {"status": "pending_review"},
            }
        ),
    )

    resp = app_client.post(
        "/api/workflow-studio/workflows/%23V%23alpha_workflow/proposals/authoring",
        json={
            "authoring_spec": {"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "start"}]},
            "base_definition_hash": "base-hash",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "namespace": "#V#test_user@test_org",
        },
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["proposal"]["status"] == "pending_review"
    assert captured["workflow_id"] == "#V#alpha_workflow"
    assert captured["base_definition_hash"] == "base-hash"
    assert captured["proposed_by"] == "#V#test_user"
    assert payload["namespace"] == "#V#test_user@test_org"


def test_workflow_studio_authoring_proposal_review_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)

    captured: dict[str, object] = {}

    _authenticate_workflow_studio_client(
        app_client,
        user_id="#V#reviewer",
        org_id="#V#test_org",
    )
    monkeypatch.setattr(
        workflows_routes,
        "review_workflow_authoring_proposal",
        lambda workflow_id, **kwargs: (
            captured.update({"workflow_id": workflow_id, **kwargs})
            or {"success": True, "review_action": kwargs["action"]}
        ),
    )

    resp = app_client.post(
        "/api/workflow-studio/workflows/%23V%23alpha_workflow/proposals/review",
        json={"action": "approve", "review_reason": "Safe to publish", "namespace": "#V#reviewer@test_org"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["review_action"] == "approve"
    assert captured == {
        "workflow_id": "#V#alpha_workflow",
        "action": "approve",
        "review_reason": "Safe to publish",
        "reviewed_by": "#V#reviewer",
        "user_id": "#V#reviewer",
        "org_id": "#V#test_org",
        "namespace": "#V#reviewer@test_org",
    }


def test_workflow_studio_publication_supersede_endpoint_requires_replacement_id(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)
    _authenticate_workflow_studio_client(app_client)
    resp = app_client.post(
        "/api/workflow-studio/workflows/%23V%23alpha_workflow/publication/supersede",
        json={},
    )

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["error"] == "replacement_workflow_id_required"


def test_workflow_studio_description_proposal_endpoint(monkeypatch, app_client):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)

    captured: dict[str, str | None] = {}

    def _build_proposal(
        workflow_id,
        mode="auto",
        user_concept_id=None,
        org_concept_id=None,
    ):
        captured["workflow_id"] = workflow_id
        captured["mode"] = mode
        captured["user_concept_id"] = user_concept_id
        captured["org_concept_id"] = org_concept_id
        return {
            "workflow_id": workflow_id,
            "proposal": {"text": "Alpha workflow description", "source": mode},
            "guardrails": {"proposal_only": True},
        }

    monkeypatch.setattr(
        workflows_routes,
        "get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        workflows_routes,
        "build_workflow_description_proposal",
        _build_proposal,
    )
    with app_client.session_transaction() as flask_session:
        flask_session["organisation_concept_id"] = "#V#test_org"

    resp = app_client.post(
        "/api/workflow-studio/workflows/%23V%23alpha_workflow/proposals/description",
        json={"mode": "llm"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["proposal"]["text"] == "Alpha workflow description"
    assert payload["proposal"]["source"] == "llm"
    assert captured == {
        "workflow_id": "#V#alpha_workflow",
        "mode": "llm",
        "user_concept_id": "#V#test_user",
        "org_concept_id": "#V#test_org",
    }


def test_workflow_studio_preview_remains_available_to_anonymous_new_target(
    monkeypatch,
    app_client,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(
        workflows_routes,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "workflow_id": workflow_id,
        },
    )

    response = app_client.post(
        "/api/workflow-studio/workflows/%23V%23new_workflow/authoring/preview",
        json={
            "authoring_spec": {
                "workflow_id": "#V#new_workflow",
                "steps": [{"state_id": "start"}],
            }
        },
    )

    assert response.status_code == 200
    assert response.get_json()["workflow_id"] == "#V#new_workflow"


@pytest.mark.parametrize(
    ("path", "payload", "service_name"),
    [
        (
            "authoring/apply",
            {
                "authoring_spec": {
                    "workflow_id": "#V#new_workflow",
                    "steps": [{"state_id": "start"}],
                }
            },
            "apply_workflow_authoring_spec",
        ),
        (
            "proposals/authoring",
            {
                "authoring_spec": {
                    "workflow_id": "#V#new_workflow",
                    "steps": [{"state_id": "start"}],
                }
            },
            "submit_workflow_authoring_proposal",
        ),
    ],
)
def test_workflow_studio_anonymous_actor_cannot_mutate_new_target(
    monkeypatch,
    app_client,
    path,
    payload,
    service_name,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    def _unexpected_mutation(*_args, **_kwargs):
        raise AssertionError("anonymous Studio route must not enter mutation service")

    monkeypatch.setattr(workflows_routes, service_name, _unexpected_mutation)

    response = app_client.post(
        f"/api/workflow-studio/workflows/%23V%23new_workflow/{path}",
        json=payload,
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "workflow_actor_authority_required",
        "error_code": "workflow_actor_authority_required",
        "workflow_id": "#V#new_workflow",
    }


@pytest.mark.parametrize(
    ("path", "payload", "service_name"),
    [
        (
            "authoring/apply",
            {
                "authoring_spec": {
                    "workflow_id": "#V#public_workflow",
                    "steps": [{"state_id": "start"}],
                }
            },
            "apply_workflow_authoring_spec",
        ),
        (
            "proposals/authoring",
            {
                "authoring_spec": {
                    "workflow_id": "#V#public_workflow",
                    "steps": [{"state_id": "start"}],
                }
            },
            "submit_workflow_authoring_proposal",
        ),
        (
            "proposals/review",
            {"action": "approve"},
            "review_workflow_authoring_proposal",
        ),
        (
            "proposals/rollback",
            {},
            "rollback_workflow_authoring_promotion",
        ),
        ("publication/demote", {}, "demote_workflow_routing"),
        (
            "publication/supersede",
            {"replacement_workflow_id": "#V#replacement_workflow"},
            "supersede_workflow_publication",
        ),
    ],
)
def test_workflow_studio_anonymous_actor_cannot_mutate_public_target(
    monkeypatch,
    app_client,
    path,
    payload,
    service_name,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    def _unexpected_mutation(*_args, **_kwargs):
        raise AssertionError("anonymous Studio route must not enter mutation service")

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: True)
    monkeypatch.setattr(workflows_routes, service_name, _unexpected_mutation)

    response = app_client.post(
        f"/api/workflow-studio/workflows/%23V%23public_workflow/{path}",
        json=payload,
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "workflow_actor_authority_required",
        "error_code": "workflow_actor_authority_required",
        "workflow_id": "#V#public_workflow",
    }


@pytest.mark.parametrize(
    ("path", "service_name"),
    [
        ("authoring/preview", "preview_workflow_authoring_spec"),
        ("authoring/apply", "apply_workflow_authoring_spec"),
        ("proposals/authoring", "submit_workflow_authoring_proposal"),
    ],
)
def test_workflow_studio_authoring_routes_allow_provably_new_targets(
    monkeypatch,
    app_client,
    path,
    service_name,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: False)
    _authenticate_workflow_studio_client(
        app_client,
        user_id="#V#author",
        org_id="#V#org",
    )
    monkeypatch.setattr(
        workflows_routes,
        service_name,
        lambda workflow_id, **_kwargs: {
            "success": True,
            "workflow_id": workflow_id,
        },
    )

    response = app_client.post(
        f"/api/workflow-studio/workflows/%23V%23new_workflow/{path}",
        json={
            "authoring_spec": {
                "workflow_id": "#V#new_workflow",
                "steps": [{"state_id": "start"}],
            }
        },
    )

    assert response.status_code == 200
    assert response.get_json()["workflow_id"] == "#V#new_workflow"


@pytest.mark.parametrize(
    ("path", "service_name"),
    [
        ("authoring/preview", "preview_workflow_authoring_spec"),
        ("authoring/apply", "apply_workflow_authoring_spec"),
        ("proposals/authoring", "submit_workflow_authoring_proposal"),
    ],
)
def test_workflow_studio_authoring_routes_conceal_hidden_existing_targets(
    monkeypatch,
    app_client,
    path,
    service_name,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    def _raise_hidden(*_args, **_kwargs):
        raise workflows_routes.WorkflowStudioAuthorityError(
            "workflow_definition_not_loadable_for_actor"
        )

    _authenticate_workflow_studio_client(app_client)
    monkeypatch.setattr(workflows_routes, service_name, _raise_hidden)
    response = app_client.post(
        f"/api/workflow-studio/workflows/%23V%23hidden_workflow/{path}",
        json={
            "authoring_spec": {
                "workflow_id": "#V#hidden_workflow",
                "steps": [{"state_id": "start"}],
            }
        },
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "workflow_definition_not_found",
        "workflow_id": "#V#hidden_workflow",
    }


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("proposals/review", {"action": "approve"}),
        ("proposals/rollback", {}),
        ("publication/demote", {}),
        (
            "publication/supersede",
            {"replacement_workflow_id": "#V#replacement"},
        ),
        ("proposals/description", {}),
    ],
)
def test_workflow_studio_existing_target_mutations_conceal_hidden_workflows(
    monkeypatch,
    app_client,
    path,
    payload,
):
    import src.backend.server.routes.workflows_routes as workflows_routes

    monkeypatch.setattr(workflows_routes, "can_access_concept", lambda _workflow_id: False)

    response = app_client.post(
        f"/api/workflow-studio/workflows/%23V%23hidden_workflow/{path}",
        json=payload,
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "workflow_definition_not_found",
        "workflow_id": "#V#hidden_workflow",
    }



def test_workflow_studio_page_route(app_client):
    resp = app_client.get("/von/workflow-studio")

    assert resp.status_code == 200
    assert b"Workflow Studio" in resp.data
    assert b'href="/von/"' in resp.data
