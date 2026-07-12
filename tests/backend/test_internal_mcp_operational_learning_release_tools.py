from __future__ import annotations

from flask import Flask, session
import pytest

from src.backend.integrations.internal_mcp import catalogue as catalogue_module
from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
)
from src.backend.integrations.internal_mcp.schemas import SchemaValidationError
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services import (
    operational_learning_release_vontology_service as service,
)
from src.backend.security.access_control import override_current_actor


NAMESPACE = "#V#user@org"
USER_ID = "#V#user"
ORG_ID = "#V#org"


def _gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_learning_release_catalogue_methods_are_strict_and_correctly_categorised() -> (
    None
):
    snapshot = build_default_catalogue().snapshot()
    expected_categories = {
        "operational_learning_build_failure_evidence_packets": "read",
        "operational_learning_build_experiment_evidence": "read",
        "operational_learning_build_certification_evidence": "read",
        "operational_learning_release_get_state": "read",
        "operational_learning_release_register_candidate": "write",
        "operational_learning_release_register_candidate_evaluation": "write",
        "operational_learning_release_record_human_approval": "write",
        "operational_learning_release_promote_candidate": "write",
        "operational_learning_release_reject_candidate": "write",
        "operational_learning_release_rollback": "write",
    }

    for method_name, category in expected_categories.items():
        assert snapshot[method_name]["category"] == category
        assert snapshot[method_name]["input_schema"]["allow_unknown"] is False

    approval_schema = snapshot["operational_learning_release_record_human_approval"][
        "input_schema"
    ]
    assert "namespace" not in approval_schema["required"]
    assert "user_id" not in approval_schema["required"]
    assert "org_id" not in approval_schema["required"]
    assert approval_schema["enum_values"]["action"] == [
        "promote",
        "reject",
        "rollback",
    ]
    assert (
        "human_approval"
        in snapshot["operational_learning_release_promote_candidate"]["input_schema"][
            "required"
        ]
    )
    assert (
        "human_approval"
        in snapshot["operational_learning_release_rollback"]["input_schema"]["required"]
    )


def test_get_state_tool_forwards_exact_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_load(**kwargs):
        captured.update(kwargs)
        return {"version": 0, "state_sha256": "a" * 64}

    monkeypatch.setattr(service, "load_operational_learning_release_state", fake_load)
    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context("/"):
        session["user_concept_id"] = USER_ID
        session["organisation_concept_id"] = ORG_ID
        result = (
            _gateway()
            .invoke(
                "operational_learning_release_get_state",
                {"namespace": NAMESPACE, "user_id": USER_ID, "org_id": ORG_ID},
            )
            .payload
        )

    assert result["success"] is True
    assert captured == {
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }


def test_get_state_accepts_durable_workflow_actor_context_without_flask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "load_operational_learning_release_state",
        lambda **kwargs: {"scope": kwargs},
    )
    with override_current_actor(USER_ID, ORG_ID):
        result = (
            _gateway()
            .invoke(
                "operational_learning_release_get_state",
                {"namespace": NAMESPACE, "user_id": USER_ID, "org_id": ORG_ID},
            )
            .payload
        )

    assert result["success"] is True
    assert result["state_record"]["scope"] == {
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }


def test_optimistic_conflict_is_projected_with_read_and_retry_affordances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def conflict(**_kwargs):
        raise service.LearningReleaseStateConflictError(
            expected_version=1,
            expected_state_sha256="a" * 64,
            current_version=2,
            current_state_sha256="b" * 64,
            state_concept_id="#V#operational_learning_release_state_test",
        )

    monkeypatch.setattr(
        service,
        "register_operational_learning_release_candidate_in_vontology",
        conflict,
    )
    definition = build_default_catalogue().get(
        "operational_learning_release_register_candidate"
    )
    monkeypatch.setattr(
        catalogue_module,
        "_operational_learning_authorised_scope",
        lambda _kwargs: {
            "namespace": NAMESPACE,
            "user_id": USER_ID,
            "org_id": ORG_ID,
        },
    )
    result = definition.handler(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        expected_version=1,
        expected_state_sha256="a" * 64,
        candidate_id="candidate-1",
        release_id="release-1",
        affected_artifact="#V#generic_artifact",
        release_payload={},
        failure_evidence_packets=[],
        proposal_authority={},
        expires_at="2027-01-01T00:00:00+00:00",
        retest_after="2026-12-01T00:00:00+00:00",
        retest_requirements={},
    )

    assert result["success"] is False
    assert result["error_code"] == "operational_learning_release_state_conflict"
    assert result["error_details"]["details"]["current_version"] == 2
    assert set(result["suggestions"]) == {
        "read_latest_state",
        "retry_with_latest_version",
    }


def test_human_approval_tool_derives_actor_only_from_authenticated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)
        return {"success": True, "operation": "record_authenticated_human_approval"}

    monkeypatch.setattr(
        service,
        "record_authenticated_human_learning_release_approval",
        fake_record,
    )
    app = Flask(__name__)
    app.secret_key = "test-only"
    gateway = _gateway()
    with app.test_request_context("/"):
        session["user_concept_id"] = USER_ID
        session["organisation_concept_id"] = ORG_ID
        result = gateway.invoke(
            "operational_learning_release_record_human_approval",
            {
                "expected_version": 1,
                "expected_state_sha256": "a" * 64,
                "approval_id": "approval-1",
                "action": "promote",
                "candidate_id": "candidate-1",
            },
        ).payload

    assert result["success"] is True
    assert captured["authenticated_namespace"] == NAMESPACE
    assert captured["authenticated_user_id"] == USER_ID
    assert captured["authenticated_org_id"] == ORG_ID


def test_human_approval_tool_rejects_caller_supplied_actor_scope() -> None:
    with pytest.raises(SchemaValidationError, match="Unexpected field 'namespace'"):
        _gateway().invoke(
            "operational_learning_release_record_human_approval",
            {
                "expected_version": 1,
                "expected_state_sha256": "a" * 64,
                "approval_id": "approval-1",
                "action": "promote",
                "candidate_id": "candidate-1",
                "namespace": "#V#forged@org",
            },
        )


def test_state_tool_rejects_scope_different_from_authenticated_session() -> None:
    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context("/"):
        session["user_concept_id"] = USER_ID
        session["organisation_concept_id"] = ORG_ID
        result = (
            _gateway()
            .invoke(
                "operational_learning_release_get_state",
                {
                    "namespace": "#V#other@org",
                    "user_id": "#V#other",
                    "org_id": ORG_ID,
                },
            )
            .payload
        )

    assert result["success"] is False
    assert result["error_code"] == (
        "operational_learning_release_authenticated_scope_mismatch"
    )


def test_human_approval_tool_fails_closed_without_authenticated_request() -> None:
    definition = build_default_catalogue().get(
        "operational_learning_release_record_human_approval"
    )
    result = definition.handler(
        expected_version=1,
        expected_state_sha256="a" * 64,
        approval_id="approval-1",
        action="promote",
        candidate_id="candidate-1",
    )

    assert result["success"] is False
    assert result["error_code"] == "authenticated_human_approval_context_required"
