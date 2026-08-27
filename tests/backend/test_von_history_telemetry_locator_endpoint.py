from __future__ import annotations

from copy import deepcopy

from flask import Flask

from src.backend.services.conversation_scope_binding_service import (
    verify_history_location_binding,
    verify_turn_telemetry_binding,
)


def test_history_telemetry_locator_uses_org_hint_to_upgrade_bare_namespace(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#test_user", None),
    )
    monkeypatch.setattr(
        "src.backend.services.conversation_telemetry_locator_service.build_conversation_llm_telemetry_locator",
        lambda **kwargs: (
            captured.update(kwargs)
            or {
                "schema_version": "conversation_llm_telemetry_locator.v1",
                "generated_at_utc": "2026-04-06T03:00:00Z",
                "session_id": kwargs["session_id"],
                "session_name": "Session 1741",
                "namespace_context": {
                    "namespace": kwargs.get("namespace"),
                    "user_id": kwargs.get("user_id"),
                    "org_id": kwargs.get("organisation_concept_id"),
                },
                "metadata": {"total_turns": 0},
                "mcp_access": {},
                "turns": [],
            }
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/telemetry_locator",
        query_string={
            "session_id": "session-1741",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#test_user",
            "user_concept_id": "#V#test_user",
        },
        headers={"X-Von-Window-Session": "ws-1741"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["history_owner_user_id"] == "#V#test_user"
    assert body["requested_user_id"] == "#V#test_user"
    assert captured["namespace"] == "#V#test_user@org"
    assert captured["requested_namespace"] == "#V#test_user@org"
    assert captured["requested_user_id"] == "#V#test_user"
    assert captured["organisation_concept_id"] == "#V#org"


def test_history_turn_telemetry_access_issues_exact_actor_bound_descriptors(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#actor_a",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#actor_a@org"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: ("#V#actor_a@org", "#V#org"),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#actor_a", None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_debug_entry",
        lambda **_kwargs: {"request_id": "req-route-2609"},
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact history locator must avoid full diagnostics hydration")
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_telemetry_access",
        query_string={
            "request_id": "req-route-2609",
            "session_id": "session-route-2609",
            "history_index": 3,
            "include_live_progress": "false",
        },
        headers={"X-Von-Window-Session": "ws-route-2609"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == "turn_telemetry_mcp_access.v1"
    assert set(body["mcp_access"]) == {
        "chat_history_get_debug_entry",
        "conversation_telemetry_get_locator",
        "turn_execution_get_diagnostics",
    }

    diagnostics_arguments = body["mcp_access"][
        "turn_execution_get_diagnostics"
    ]["arguments"]
    with app.app_context():
        verified_turn = verify_turn_telemetry_binding(
            diagnostics_arguments["turn_telemetry_ref"],
            expected_tool="turn_execution_get_diagnostics",
            expected_actor_user_id="#V#actor_a",
        )
    assert verified_turn["success"] is True
    assert verified_turn["request_id"] == "req-route-2609"

    debug_arguments = body["mcp_access"]["chat_history_get_debug_entry"][
        "arguments"
    ]
    with app.app_context():
        verified_history = verify_history_location_binding(
            debug_arguments["history_location_ref"],
            require_read_delegation=True,
            expected_tool="chat_history_get_debug_entry",
            expected_actor_user_id="#V#actor_a",
        )
    assert verified_history["success"] is True
    assert verified_history["history_index"] == 3


def test_history_turn_telemetry_access_requires_authenticated_actor(monkeypatch):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_telemetry_access",
        query_string={"request_id": "req-route-2609"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Not authenticated"


def test_history_turn_telemetry_access_rejects_unknown_explicit_window_before_lookup(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#actor_a",
    )

    def fail_closed_context(**kwargs):
        captured.update(kwargs)
        raise von_routes.WindowSessionContextUnavailable(
            "window_session_context_unavailable"
        )

    monkeypatch.setattr(
        von_routes,
        "_get_effective_context_with_owned_conversation_recovery",
        fail_closed_context,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("scope denial must happen before telemetry lookup")
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_telemetry_access",
        query_string={
            "request_id": "req-unknown-window",
            "session_id": "session-owned-by-actor-a",
        },
        headers={"X-Von-Window-Session": "unknown-window"},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "error": "window_context_unavailable",
        "error_code": "window_context_unavailable",
        "retryable": True,
    }
    assert captured["window_session_id"] == "unknown-window"
    assert captured["user_concept_id"] == "#V#actor_a"
    assert captured["conversation_session_id"] == "session-owned-by-actor-a"


def test_history_turn_telemetry_access_reports_transient_scope_recovery_failure(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#actor_a",
    )
    monkeypatch.setattr(
        von_routes,
        "_get_effective_context_with_owned_conversation_recovery",
        lambda **_kwargs: (_ for _ in ()).throw(
            von_routes.WindowSessionContextRecoveryUnavailable(
                "window_session_context_recovery_unavailable"
            )
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_telemetry_access",
        query_string={"request_id": "req-store-outage"},
        headers={"X-Von-Window-Session": "known-window"},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "window_context_recovery_unavailable",
        "error_code": "window_context_recovery_unavailable",
        "retryable": True,
    }


def test_history_turn_telemetry_access_reads_exact_actor_scope_live_progress(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    actor_user_id = "#V#actor_a"
    actor_org_id = "#V#org_a"
    actor_namespace = "#V#actor_a@org_a"
    request_id = "req-live-progress-actor-org-a"
    actor_scope_key = von_routes._build_authenticated_tool_progress_scope_key(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_org_id,
        namespace=actor_namespace,
    )

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: actor_user_id,
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {
            "namespace": actor_namespace,
            "organisation_id": actor_org_id,
        },
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: (actor_namespace, actor_org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        von_routes,
        "fetch_tool_progress_state",
        lambda **_kwargs: None,
    )
    monkeypatch.setitem(
        von_routes._TOOL_PROGRESS,
        (actor_scope_key, request_id),
        {
            "request_id": request_id,
            "session_id": "session-live-progress-org-a",
            "status": "thinking",
            "stage": "tool_execution",
            "phase": "tool_execution",
            "updated_at_epoch": 9_999_999_999.0,
            "request_started_epoch": 9_999_999_998.0,
            "elapsed_ms": 1_000,
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_telemetry_access",
        query_string={"request_id": request_id},
        headers={"X-Von-Window-Session": "window-org-a"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == "turn_telemetry_mcp_access.v1"
    assert "turn_execution_get_live_progress" in body["mcp_access"]


def test_history_turn_telemetry_access_does_not_cross_actor_org_scope(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    actor_user_id = "#V#actor_a"
    org_a_scope_key = von_routes._build_authenticated_tool_progress_scope_key(
        user_concept_id=actor_user_id,
        organisation_concept_id="#V#org_a",
        namespace="#V#actor_a@org_a",
    )
    org_b_scope_key = von_routes._build_authenticated_tool_progress_scope_key(
        user_concept_id=actor_user_id,
        organisation_concept_id="#V#org_b",
        namespace="#V#actor_a@org_b",
    )
    request_id = "req-live-progress-private-to-org-a"
    assert org_a_scope_key != org_b_scope_key

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: actor_user_id,
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {
            "namespace": "#V#actor_a@org_b",
            "organisation_id": "#V#org_b",
        },
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: ("#V#actor_a@org_b", "#V#org_b"),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        von_routes,
        "fetch_tool_progress_state",
        lambda **_kwargs: None,
    )
    monkeypatch.setitem(
        von_routes._TOOL_PROGRESS,
        (org_a_scope_key, request_id),
        {
            "request_id": request_id,
            "status": "thinking",
            "stage": "tool_execution",
            "phase": "tool_execution",
            "updated_at_epoch": 9_999_999_999.0,
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_telemetry_access",
        query_string={"request_id": request_id},
        headers={"X-Von-Window-Session": "window-org-b"},
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "Turn telemetry not found"}


def _stored_failure_capsule(request_id: str) -> dict:
    return {
        "schema_version": "turn_failure_capsule.v1",
        "generated_at_utc": "2026-08-18T00:00:00Z",
        "request_id": request_id,
        "terminal_status": "effect_partially_completed",
        "response_authority": "canonical_outcome",
        "producer": {
            "code_version": "v20260818_backend+gabc123",
            "git_commit": "abc123abc123abc123abc123abc123abc123abcd",
        },
        "effects": [
            {
                "effect_id": "effect-download",
                "name": "download_paper",
                "initial_status": "failed",
                "status": "failed",
                "changed": False,
                "outcome_resolved": False,
                "error": {
                    "code": "arxiv_acquisition_unavailable",
                    "preview": {
                        "text": (
                            "RuntimeError: asyncio lock is bound to a different "
                            "event loop"
                        ),
                        "char_count": 66,
                        "sha256": "a" * 64,
                        "truncated": False,
                    },
                },
            }
        ],
        "effect_summary": {
            "total_count": 1,
            "included_count": 1,
            "omitted_count": 0,
        },
        "canonical_scope_modes": ["user"],
        "redaction": {
            "applied": True,
            "redacted_count": 1,
            "truncated": False,
        },
    }


def test_history_turn_failure_capsule_returns_only_stored_shared_grant_projection(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    request_id = "req-capsule-shared"
    capsule = _stored_failure_capsule(request_id)
    stored_capsule = deepcopy(capsule)
    stored_capsule["mcp_access"] = {"signed_ref": "must-not-escape"}
    stored_capsule["actor_concept_id"] = "#V#private_actor"
    stored_capsule["prompt"] = "private prompt"
    stored_capsule["producer"]["namespace"] = "#V#private_actor@org"
    stored_capsule["effects"][0]["arguments"] = {"raw": "private argument"}
    stored_capsule["effects"][0]["error"]["receipt"] = {
        "access_token": "private token"
    }
    stored_capsule["effects"][0]["error"]["preview"]["messages"] = [
        "private message"
    ]
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#invitee",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#invitee@org"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: ("#V#invitee@org", "#V#org"),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (
            "#V#owner",
            {"organisation_concept_id": "#V#org"},
        ),
    )
    monkeypatch.setattr(
        von_routes,
        "_derive_namespace_for_user_org",
        lambda *_args, **_kwargs: "#V#owner@org",
    )

    def get_debug(**kwargs):
        captured.update(kwargs)
        return {
            "request_id": request_id,
            "turn_failure_capsule": stored_capsule,
            "aux_llm_calls": {"offloaded": True},
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_debug_entry",
        get_debug,
    )
    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: (_ for _ in ()).throw(
            AssertionError("capsule readback must not substitute current runtime")
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_failure_capsule",
        query_string={
            "request_id": request_id,
            "session_id": "shared-session",
            "history_index": 5,
        },
        headers={"X-Von-Window-Session": "shared-window"},
    )

    assert response.status_code == 200
    assert response.get_json() == capsule
    assert captured == {
        "user_id": "#V#owner",
        "session_id": "shared-session",
        "history_index": 5,
        "namespace": "#V#owner@org",
        "include_legacy": True,
        "hydrate_blob_refs": False,
    }
    assert "mcp_access" not in response.get_json()
    assert "history_owner_user_id" not in response.get_json()
    assert "private" not in str(response.get_json())


def test_history_turn_failure_capsule_denies_cross_actor_conversation(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#wrong_actor",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#wrong_actor@org"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: ("#V#wrong_actor@org", "#V#org"),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_debug_entry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("cross-actor denial must happen before debug lookup")
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_failure_capsule",
        query_string={
            "request_id": "req-private",
            "session_id": "private-session",
            "history_index": 2,
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not authorised for conversation"}


def test_history_turn_failure_capsule_denies_wrong_request_for_history_entry(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#owner",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#owner@org"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: ("#V#owner@org", "#V#org"),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#owner", None),
    )
    monkeypatch.setattr(
        von_routes,
        "_derive_namespace_for_user_org",
        lambda *_args, **_kwargs: "#V#owner@org",
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_debug_entry",
        lambda **_kwargs: {
            "request_id": "different-request",
            "turn_failure_capsule": _stored_failure_capsule("different-request"),
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_failure_capsule",
        query_string={
            "request_id": "requested-turn",
            "session_id": "owner-session",
            "history_index": 4,
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "request_id does not belong to history_index"
    }


def test_history_turn_failure_capsule_legacy_absence_is_typed_404(monkeypatch):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#owner",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#owner@org"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_history_request_scope_hints",
        lambda **_kwargs: ("#V#owner@org", "#V#org"),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#owner", None),
    )
    monkeypatch.setattr(
        von_routes,
        "_derive_namespace_for_user_org",
        lambda *_args, **_kwargs: "#V#owner@org",
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_debug_entry",
        lambda **_kwargs: {
            "request_id": "legacy-request",
            "aux_llm_calls": [{"type": "adaptive_turn_effect_outcome_report"}],
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_failure_capsule",
        query_string={
            "request_id": "legacy-request",
            "session_id": "legacy-session",
            "history_index": 1,
        },
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "failure_capsule_not_available"}


def test_history_turn_failure_capsule_requires_authenticated_actor(monkeypatch):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history/turn_failure_capsule",
        query_string={
            "request_id": "unauthenticated-request",
            "session_id": "private-session",
            "history_index": 1,
        },
    )

    assert response.status_code == 401
    assert response.get_json() == {"error": "Not authenticated"}
