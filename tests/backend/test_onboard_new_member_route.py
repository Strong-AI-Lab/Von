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
    monkeypatch.setattr(von_routes, "_resolve_onboarding_workflow_candidates", lambda payload: [])

    response = client.post(
        "/von/onboard_new_member",
        json={"member_name": "Carol"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error_code"] == "onboarding_workflow_not_configured"


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

