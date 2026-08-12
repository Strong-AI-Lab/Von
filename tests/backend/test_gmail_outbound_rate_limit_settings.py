from __future__ import annotations

from typing import Any

import pytest
from flask import Flask

from src.backend.server.routes import settings_routes
from src.backend.server.routes.settings_routes import settings_bp
from src.backend.services import settings_service


def _make_settings_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def _policy(**overrides) -> settings_service.GmailOutboundRateLimitSettings:
    values: dict[str, Any] = {
        "enabled": False,
        "max_messages_per_10_minutes": 5,
        "max_messages_per_day": 25,
        "max_recipients_per_message": 10,
        "max_recipient_deliveries_per_day": 50,
    }
    values.update(overrides)
    return settings_service.GmailOutboundRateLimitSettings(**values)


def _patch_unrelated_settings_reads(monkeypatch) -> None:
    monkeypatch.setattr(settings_routes, "resolve_rag_embedder_setting", lambda: None)
    monkeypatch.setattr(settings_routes, "resolve_rag_llm_setting", lambda: None)
    monkeypatch.setattr(settings_routes, "get_server_default_llm_setting", lambda: None)


def test_policy_defaults_disabled_with_conservative_limits(monkeypatch):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: None)

    policy = settings_service.get_gmail_outbound_rate_limit_settings()

    assert policy.as_dict() == {
        "schema_version": "gmail_outbound_rate_limits.v1",
        "enabled": False,
        "max_messages_per_10_minutes": 5,
        "max_messages_per_day": 25,
        "max_recipients_per_message": 10,
        "max_recipient_deliveries_per_day": 50,
    }


def test_stored_policy_is_typed_clamped_and_internally_consistent():
    policy = settings_service.normalise_gmail_outbound_rate_limit_settings(
        {
            "enabled": "yes",
            "max_messages_per_10_minutes": 999,
            "max_messages_per_day": -2,
            "max_recipients_per_message": 100,
            "max_recipient_deliveries_per_day": 4,
        }
    )

    assert policy.enabled is True
    assert policy.max_messages_per_10_minutes == 100
    assert policy.max_messages_per_day == 1
    assert policy.max_recipients_per_message == 4
    assert policy.max_recipient_deliveries_per_day == 4


@pytest.mark.parametrize(
    "payload, error_fragment",
    [
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"max_messages_per_day": True}, "must be an integer"),
        ({"max_messages_per_day": 0}, "must be between 1 and 1000"),
        ({"typo": 3}, "unknown field"),
        (
            {"schema_version": "gmail_outbound_rate_limits.v999"},
            "schema_version",
        ),
        (
            {
                "max_recipients_per_message": 11,
                "max_recipient_deliveries_per_day": 10,
            },
            "cannot exceed",
        ),
    ],
)
def test_strict_admin_policy_parser_rejects_ambiguous_values(payload, error_fragment):
    with pytest.raises(ValueError, match=error_fragment):
        settings_service.parse_gmail_outbound_rate_limit_settings(payload)


def test_partial_admin_policy_inherits_current_values():
    parsed = settings_service.parse_gmail_outbound_rate_limit_settings(
        {"enabled": True},
        current=_policy(max_messages_per_day=40),
    )

    assert parsed.enabled is True
    assert parsed.max_messages_per_day == 40
    assert parsed.max_messages_per_10_minutes == 5


def test_batch_settings_exposes_canonical_policy(monkeypatch):
    monkeypatch.setattr(
        settings_service,
        "get_settings_batch",
        lambda _names: {
            settings_service.GMAIL_OUTBOUND_RATE_LIMITS_SETTING_NAME: {
                "enabled": True,
                "max_messages_per_day": 30,
            }
        },
    )

    result = settings_service.get_all_settings_batch()

    assert result["gmail_outbound_rate_limits"]["enabled"] is True
    assert result["gmail_outbound_rate_limits"]["max_messages_per_day"] == 30


def test_non_admin_cannot_update_outbound_email_policy(monkeypatch):
    app = _make_settings_app()
    called = {"value": False}
    monkeypatch.setattr(
        settings_routes,
        "set_gmail_outbound_rate_limit_settings",
        lambda _value: called.update(value=True) or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["role_in_org"] = "member"
        response = client.post(
            "/api/settings/",
            json={"gmail_outbound_rate_limits": {"enabled": True}},
        )

    assert response.status_code == 403
    assert called["value"] is False


def test_admin_can_update_valid_outbound_email_policy(monkeypatch):
    app = _make_settings_app()
    _patch_unrelated_settings_reads(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        settings_routes,
        "get_gmail_outbound_rate_limit_settings",
        lambda: _policy(),
    )

    def persist(value):
        captured["policy"] = value
        return True

    monkeypatch.setattr(
        settings_routes,
        "set_gmail_outbound_rate_limit_settings",
        persist,
    )

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["role_in_org"] = "admin"
        response = client.post(
            "/api/settings/",
            json={
                "gmail_outbound_rate_limits": {
                    "enabled": True,
                    "max_messages_per_10_minutes": 4,
                    "max_messages_per_day": 20,
                    "max_recipients_per_message": 8,
                    "max_recipient_deliveries_per_day": 40,
                }
            },
        )

    assert response.status_code == 200
    assert captured["policy"].enabled is True
    assert captured["policy"].max_messages_per_10_minutes == 4
    assert response.get_json()["gmail_outbound_rate_limits"]["enabled"] is True


def test_admin_endpoint_rejects_invalid_policy_without_persisting(monkeypatch):
    app = _make_settings_app()
    _patch_unrelated_settings_reads(monkeypatch)
    called = {"value": False}
    monkeypatch.setattr(
        settings_routes,
        "get_gmail_outbound_rate_limit_settings",
        lambda: _policy(),
    )
    monkeypatch.setattr(
        settings_routes,
        "set_gmail_outbound_rate_limit_settings",
        lambda _value: called.update(value=True) or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["role_in_org"] = "owner"
        response = client.post(
            "/api/settings/",
            json={"gmail_outbound_rate_limits": {"max_messages_per_10_minutes": 0}},
        )

    assert response.status_code == 400
    assert called["value"] is False


def test_policy_is_only_returned_to_admin_or_owner(monkeypatch):
    app = _make_settings_app()
    _patch_unrelated_settings_reads(monkeypatch)
    monkeypatch.setattr(
        settings_routes,
        "get_all_settings_data",
        lambda: {"gmail_outbound_rate_limits": _policy(enabled=True).as_dict()},
    )
    monkeypatch.setattr(
        settings_routes,
        "get_gmail_outbound_rate_limit_settings",
        lambda: _policy(enabled=True),
    )

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["role_in_org"] = "member"
        member_response = client.get("/api/settings/")

        with client.session_transaction() as session:
            session["role_in_org"] = "owner"
        owner_response = client.get("/api/settings/")

    assert member_response.status_code == 200
    assert "gmail_outbound_rate_limits" not in member_response.get_json()
    assert owner_response.status_code == 200
    assert owner_response.get_json()["gmail_outbound_rate_limits"]["enabled"] is True
