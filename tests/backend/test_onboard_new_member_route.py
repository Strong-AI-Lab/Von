from __future__ import annotations

from typing import Any

import pytest
from flask import Flask


class _FakeSubmissionResult:
    def __init__(self, *, success: bool, payload: dict[str, Any]):
        self.success = success
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    return flask_app


def test_onboard_new_member_validates_member_name(app: Flask) -> None:
    client = app.test_client()

    response = client.post("/von/onboard_new_member", json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "No member name provided."


def test_onboard_new_member_rejects_invalid_max_retries(app: Flask) -> None:
    client = app.test_client()

    response = client.post(
        "/von/onboard_new_member",
        json={"member_name": "Alice", "max_retries": "many"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_retries must be an integer between 0 and 10."


def test_onboard_new_member_submits_first_runnable_candidate(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#tester"
        sess["organisation_concept_id"] = "#V#sail_lab"

    monkeypatch.setattr(
        von_routes,
        "_resolve_onboarding_workflow_candidates",
        lambda payload: ["#V#candidate_one", "#V#candidate_two"],
    )
    sentinel_manager = object()
    monkeypatch.setattr(von_routes, "get_instance_manager", lambda: sentinel_manager)

    call_workflow_ids: list[str] = []

    def _fake_submit(*, workflow_id: str, **kwargs):
        call_workflow_ids.append(workflow_id)
        if workflow_id == "#V#candidate_one":
            return _FakeSubmissionResult(
                success=False,
                payload={
                    "success": False,
                    "workflow_id": workflow_id,
                    "status": "rejected_preflight",
                    "verification": {"runnable_verification_success": False},
                },
            )
        assert kwargs["manager"] is sentinel_manager
        assert kwargs["user_id"] == "#V#tester"
        assert kwargs["org_id"] == "#V#sail_lab"
        assert kwargs["namespace"] == "#V#tester@sail_lab"
        assert kwargs["inputs"]["member_name"] == "Alice Example"
        return _FakeSubmissionResult(
            success=True,
            payload={
                "success": True,
                "workflow_id": workflow_id,
                "status": "pending",
                "instance_id": "instance-123",
                "verification": {"runnable_verification_success": True},
            },
        )

    monkeypatch.setattr(von_routes, "submit_verified_workflow_instance", _fake_submit)

    response = client.post(
        "/von/onboard_new_member",
        json={"member_name": " Alice Example "},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["selected_workflow_id"] == "#V#candidate_two"
    assert payload["attempt_count"] == 2
    assert payload["instance_id"] == "instance-123"
    assert call_workflow_ids == ["#V#candidate_one", "#V#candidate_two"]


def test_onboard_new_member_returns_not_runnable_when_all_candidates_fail(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#tester"
        sess["organisation_concept_id"] = "#V#sail_lab"

    monkeypatch.setattr(
        von_routes,
        "_resolve_onboarding_workflow_candidates",
        lambda payload: ["#V#candidate_one"],
    )
    monkeypatch.setattr(von_routes, "get_instance_manager", lambda: object())
    monkeypatch.setattr(
        von_routes,
        "submit_verified_workflow_instance",
        lambda **kwargs: _FakeSubmissionResult(
            success=False,
            payload={
                "success": False,
                "workflow_id": kwargs["workflow_id"],
                "status": "rejected_preflight",
                "verification": {"runnable_verification_success": False},
                "error_code": "workflow_not_runnable",
            },
        ),
    )

    response = client.post(
        "/von/onboard_new_member",
        json={"member_name": "Bob"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error_code"] == "onboarding_workflow_not_runnable"
    assert payload["candidate_workflow_ids"] == ["#V#candidate_one"]
    assert len(payload["attempts"]) == 1


def test_onboard_new_member_returns_not_configured_when_no_candidates(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#tester"
        sess["organisation_concept_id"] = "#V#sail_lab"
    monkeypatch.setattr(von_routes, "_resolve_onboarding_workflow_candidates", lambda payload: [])

    response = client.post(
        "/von/onboard_new_member",
        json={"member_name": "Carol"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error_code"] == "onboarding_workflow_not_configured"


def test_onboard_new_member_rejects_unauthenticated_actor_claims(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "submit_verified_workflow_instance",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthenticated actor claims must not reach submission")
        ),
    )
    client = app.test_client()

    response = client.post(
        "/von/onboard_new_member",
        json={
            "member_name": "Mallory",
            "workflow_id": "#V#restricted_onboarding_workflow",
            "user_id": "#V#trusted_member",
            "org_id": "#V#trusted_org",
            "namespace": "#V#trusted_member@trusted_org",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == (
        "workflow_actor_authority_required"
    )


def test_onboard_new_member_rejects_authenticated_body_impersonation(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "submit_verified_workflow_instance",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("forged actor must not reach submission")
        ),
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#outsider"
        sess["organisation_concept_id"] = "#V#outsider_org"

    response = client.post(
        "/von/onboard_new_member",
        json={
            "member_name": "Mallory",
            "workflow_id": "#V#restricted_onboarding_workflow",
            "user_id": "#V#trusted_member",
            "org_id": "#V#trusted_org",
            "namespace": "#V#trusted_member@trusted_org",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == "workflow_actor_scope_mismatch"


def test_onboard_new_member_conceals_explicit_hidden_workflow_from_outsider(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    hidden_workflow_id = "#V#restricted_member_onboarding_workflow"

    class _FakeRegistry:
        def all_workflow_ids(self):
            return [hidden_workflow_id]

    raw_candidate_sets: list[list[str]] = []

    def _project_visible(workflow_ids):
        raw_candidate_sets.append(list(workflow_ids))
        return []

    monkeypatch.delenv("VON_NEW_MEMBER_ONBOARDING_WORKFLOW_IDS", raising=False)
    monkeypatch.setattr(
        von_routes,
        "build_workflow_registry_read_only",
        lambda: _FakeRegistry(),
    )
    monkeypatch.setattr(
        von_routes,
        "filter_workflow_ids_for_current_actor",
        _project_visible,
    )
    monkeypatch.setattr(
        von_routes,
        "submit_verified_workflow_instance",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("hidden workflow must not reach submission")
        ),
    )

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#outsider"
        sess["organisation_concept_id"] = "#V#outsider_org"

    response = client.post(
        "/von/onboard_new_member",
        json={
            "member_name": "New Colleague",
            "workflow_id": hidden_workflow_id,
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "No onboarding workflow is currently available.",
        "error_code": "onboarding_workflow_not_configured",
    }
    assert raw_candidate_sets == [[hidden_workflow_id]]
    assert hidden_workflow_id not in response.get_data(as_text=True)


def test_onboard_new_member_filters_global_candidates_before_attempts_and_response(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    hidden_workflow_id = "#V#restricted_member_onboarding_workflow"
    visible_workflow_id = "#V#public_member_onboarding_workflow"

    class _FakeRegistry:
        def all_workflow_ids(self):
            return [hidden_workflow_id, visible_workflow_id]

    submitted_workflow_ids: list[str] = []

    monkeypatch.delenv("VON_NEW_MEMBER_ONBOARDING_WORKFLOW_IDS", raising=False)
    monkeypatch.setattr(
        von_routes,
        "build_workflow_registry_read_only",
        lambda: _FakeRegistry(),
    )
    monkeypatch.setattr(
        von_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: [
            workflow_id
            for workflow_id in workflow_ids
            if workflow_id == visible_workflow_id
        ],
    )

    def _submit_visible(*, workflow_id: str, **_kwargs):
        submitted_workflow_ids.append(workflow_id)
        return _FakeSubmissionResult(
            success=False,
            payload={
                "success": False,
                "workflow_id": workflow_id,
                "status": "rejected_preflight",
                "error_code": "workflow_not_runnable",
            },
        )

    monkeypatch.setattr(von_routes, "get_instance_manager", lambda: object())
    monkeypatch.setattr(
        von_routes,
        "submit_verified_workflow_instance",
        _submit_visible,
    )

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#outsider"
        sess["organisation_concept_id"] = "#V#outsider_org"

    response = client.post(
        "/von/onboard_new_member",
        json={"member_name": "New Colleague"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["candidate_workflow_ids"] == [visible_workflow_id]
    assert payload["attempts"] == [
        {
            "success": False,
            "workflow_id": visible_workflow_id,
            "status": "rejected_preflight",
            "error_code": "workflow_not_runnable",
        }
    ]
    assert submitted_workflow_ids == [visible_workflow_id]
    assert hidden_workflow_id not in response.get_data(as_text=True)


def test_resolve_onboarding_workflow_candidates_uses_payload_env_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    class _FakeRegistry:
        def all_workflow_ids(self):
            return [
                "#V#chat_narration_workflow",
                "#V#sail_phd_student_onboarding_workflow",
                "#V#von_user_onboarding_workflow",
            ]

    monkeypatch.setenv(
        "VON_NEW_MEMBER_ONBOARDING_WORKFLOW_IDS",
        " #V#env_onboarding_workflow, custom_onboard_flow ",
    )
    monkeypatch.setattr(
        von_routes,
        "build_workflow_registry_read_only",
        lambda: _FakeRegistry(),
    )
    monkeypatch.setattr(
        von_routes,
        "filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )

    candidates = von_routes._resolve_onboarding_workflow_candidates(
        {
            "workflow_id": "payload_workflow",
            "workflow_ids": ["#V#payload_fallback", "payload_workflow"],
        }
    )

    assert candidates == [
        "#V#payload_workflow",
        "#V#payload_fallback",
        "#V#env_onboarding_workflow",
        "#V#custom_onboard_flow",
        "#V#sail_phd_student_onboarding_workflow",
        "#V#von_user_onboarding_workflow",
    ]
