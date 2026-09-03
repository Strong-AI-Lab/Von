from __future__ import annotations

from typing import Any

import pytest
from flask import Flask

from src.backend.server.routes import settings_routes


def _make_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "settings-meta-muse-test"
    app.register_blueprint(settings_routes.settings_bp, url_prefix="/api/settings")
    return app


def test_meta_key_presence_supports_canonical_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "meta-secret-file-value"
    secret_file = tmp_path / "meta_api_key"
    secret_file.write_text(secret, encoding="utf-8")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("META_API_KEY_FILE", str(secret_file))
    monkeypatch.setattr(settings_routes, "read_repo_dotenv_values", lambda _keys: {})

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/env_var/check",
            json={"env_var_name": "META_API_KEY"},
        )
    )

    assert response.status_code == 200
    assert response.get_json() == {"exists": True, "source": "secret_file"}
    assert response.headers.get("Cache-Control") == "no-store"
    assert secret not in response.get_data(as_text=True)


def test_meta_key_presence_rejects_generic_model_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "must-not-be-read")

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/env_var/check",
            json={"env_var_name": "MODEL_API_KEY"},
        )
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Unsupported model API key source."}


def test_settings_meta_key_resolver_reads_repo_dotenv_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret_file = tmp_path / "meta_api_key"
    secret_file.write_text("meta-dotenv-file-value", encoding="utf-8")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY_FILE", raising=False)
    monkeypatch.setattr(
        settings_routes,
        "read_repo_dotenv_values",
        lambda keys: (
            {"META_API_KEY_FILE": str(secret_file)}
            if "META_API_KEY_FILE" in keys
            else {}
        ),
    )

    assert (
        settings_routes._resolve_settings_api_key("META_API_KEY")
        == "meta-dotenv-file-value"
    )


def test_meta_model_catalogue_filters_to_the_fixed_supported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = False

    class _FakeMetaMuseClient:
        def __init__(self, *, api_key: str, **_kwargs: Any):
            assert api_key == "configured-meta-key"

        @staticmethod
        def list_models() -> list[str]:
            return ["muse-spark-1.3", "future-meta-model"]

        @staticmethod
        def generate(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            nonlocal generated
            generated = True

    monkeypatch.setattr(settings_routes, "MetaMuseClient", _FakeMetaMuseClient)
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda name, **_kwargs: (
            "configured-meta-key" if name == "META_API_KEY" else None
        ),
    )

    response = _make_app().test_client().get("/api/settings/models/meta")

    assert response.status_code == 200
    assert response.get_json() == ["muse-spark-1.3"]
    assert response.headers.get("Cache-Control") == "no-store"
    assert generated is False


def test_meta_key_verification_uses_authenticated_catalogue_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = False

    class _FakeMetaMuseClient:
        def __init__(self, *, api_key: str, **_kwargs: Any):
            assert api_key == "configured-meta-key"

        @staticmethod
        def list_models() -> list[str]:
            return ["muse-spark-1.3"]

        @staticmethod
        def generate(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            nonlocal generated
            generated = True

    monkeypatch.setattr(settings_routes, "MetaMuseClient", _FakeMetaMuseClient)
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda name, **_kwargs: (
            "configured-meta-key" if name == "META_API_KEY" else None
        ),
    )

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/meta/verify",
            json={"api_key_env_var": "META_API_KEY"},
        )
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "models": ["muse-spark-1.3"],
        "verification_kind": "authenticated_model_catalogue",
    }
    assert generated is False


def test_meta_model_probe_checks_exact_actor_pair_before_key_and_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    captured: dict[str, Any] = {}

    def _allow(**kwargs: Any) -> dict[str, bool]:
        events.append("eligibility")
        captured["eligibility"] = kwargs
        return {"allowed": True}

    def _resolve_key(name: str, **_kwargs: Any) -> str:
        events.append("key")
        assert name == "META_API_KEY"
        return "configured-meta-key"

    class _FakeMetaMuseClient:
        def __init__(self, **kwargs: Any):
            events.append("client")
            captured["client"] = kwargs
            self.last_response_metadata = {"api_surface": "responses"}

        def generate(self, prompt: str, **kwargs: Any) -> str:
            events.append("request")
            captured["prompt"] = prompt
            captured["generate"] = kwargs
            return "OK"

    monkeypatch.setattr(settings_routes, "MetaMuseClient", _FakeMetaMuseClient)
    monkeypatch.setattr(settings_routes, "assert_model_execution_allowed", _allow)
    monkeypatch.setattr(settings_routes, "_resolve_settings_api_key", _resolve_key)
    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#meta_user", "authenticated_session"),
    )
    monkeypatch.setattr(
        settings_routes,
        "get_effective_organisation_concept_id",
        lambda: "#V#meta_org",
    )
    monkeypatch.setattr(
        settings_routes,
        "normalise_model_parameters_for_storage",
        lambda *_args, **_kwargs: {"reasoning_effort": "medium"},
    )
    monkeypatch.setattr(
        settings_routes,
        "build_model_parameter_capabilities",
        lambda **_kwargs: {"parameters": {}},
    )

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/meta/test_model",
            json={
                "api_key_env_var": "META_API_KEY",
                "model": "muse-spark-1.3",
                "model_parameters": {"reasoning_effort": "medium"},
            },
        )
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is True
    assert payload["model"] == "muse-spark-1.3"
    assert payload["api_surface"] == "responses"
    assert events == ["eligibility", "key", "client", "request"]
    assert captured["eligibility"] == {
        "provider": "meta",
        "model": "muse-spark-1.3",
        "user_concept_id": "#V#meta_user",
        "org_concept_id": "#V#meta_org",
        "allow_ambient_actor_scope": False,
    }
    assert captured["client"]["model_execution_actor_scope_bound"] is True
    assert captured["generate"] == {
        "model": "muse-spark-1.3",
        "llm_params": {"reasoning_effort": "medium"},
    }


def test_meta_model_probe_rejects_legacy_identity_header_before_paid_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security import access_control

    paid_work_started = False

    def _unexpected(*_args: Any, **_kwargs: Any) -> None:
        nonlocal paid_work_started
        paid_work_started = True
        pytest.fail("legacy identity must be rejected before paid work")

    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda _concept_id: "#V#claimed_user",
    )
    monkeypatch.setattr(settings_routes, "assert_model_execution_allowed", _unexpected)
    monkeypatch.setattr(settings_routes, "_resolve_settings_api_key", _unexpected)
    monkeypatch.setattr(settings_routes, "MetaMuseClient", _unexpected)

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/meta/test_model",
            headers={"X-User-Concept-ID": "#V#claimed_user"},
            json={"model": "muse-spark-1.3"},
        )
    )

    assert response.status_code == 401
    assert response.get_json()["failure_kind"] == (
        "authenticated_actor_context_required"
    )
    assert paid_work_started is False


def test_meta_model_probe_denial_precedes_key_resolution_and_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_resolved = False
    client_constructed = False

    def _deny(**_kwargs: Any) -> None:
        raise settings_routes.ModelExecutionEligibilityError(
            "Meta model 'muse-spark-1.3' is not enabled for this actor.",
            provider="meta",
            model="muse-spark-1.3",
        )

    def _unexpected_key(*_args: Any, **_kwargs: Any) -> str:
        nonlocal key_resolved
        key_resolved = True
        return "unexpected"

    class _UnexpectedMetaMuseClient:
        def __init__(self, **_kwargs: Any):
            nonlocal client_constructed
            client_constructed = True

    monkeypatch.setattr(settings_routes, "assert_model_execution_allowed", _deny)
    monkeypatch.setattr(settings_routes, "_resolve_settings_api_key", _unexpected_key)
    monkeypatch.setattr(settings_routes, "MetaMuseClient", _UnexpectedMetaMuseClient)
    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#meta_user", "authenticated_session"),
    )
    monkeypatch.setattr(
        settings_routes, "get_effective_organisation_concept_id", lambda: None
    )

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/meta/test_model",
            json={"model": "muse-spark-1.3"},
        )
    )

    assert response.status_code == 403
    assert response.get_json()["failure_kind"] == "model_not_enabled"
    assert key_resolved is False
    assert client_constructed is False


def test_meta_model_probe_rejects_noncanonical_key_source_before_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligibility_checked = False
    key_resolved = False

    def _unexpected_eligibility(**_kwargs: Any) -> dict[str, bool]:
        nonlocal eligibility_checked
        eligibility_checked = True
        return {"allowed": True}

    def _unexpected_key(*_args: Any, **_kwargs: Any) -> str:
        nonlocal key_resolved
        key_resolved = True
        return "unexpected"

    monkeypatch.setattr(
        settings_routes, "assert_model_execution_allowed", _unexpected_eligibility
    )
    monkeypatch.setattr(settings_routes, "_resolve_settings_api_key", _unexpected_key)

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/meta/test_model",
            json={
                "api_key_env_var": "MODEL_API_KEY",
                "model": "muse-spark-1.3",
            },
        )
    )

    assert response.status_code == 400
    assert response.get_json()["failure_kind"] == "invalid_key_env_var"
    assert eligibility_checked is False
    assert key_resolved is False


def test_meta_model_probe_reports_billing_without_exposing_provider_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_detail = "billing_not_configured meta-secret-sentinel"

    class _BillingError(RuntimeError):
        failure_kind = "billing_not_configured"
        provider_error_code = "billing_not_configured"
        status_code = 402

    class _FakeMetaMuseClient:
        def __init__(self, **_kwargs: Any):
            self.last_response_metadata: dict[str, Any] = {}

        @staticmethod
        def generate(*_args: Any, **_kwargs: Any) -> str:
            raise _BillingError(sensitive_detail)

    monkeypatch.setattr(settings_routes, "MetaMuseClient", _FakeMetaMuseClient)
    monkeypatch.setattr(
        settings_routes,
        "assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda *_args, **_kwargs: "configured-meta-key",
    )
    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#u", "authenticated_session"),
    )
    monkeypatch.setattr(
        settings_routes, "get_effective_organisation_concept_id", lambda: None
    )
    monkeypatch.setattr(
        settings_routes,
        "normalise_model_parameters_for_storage",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        settings_routes,
        "build_model_parameter_capabilities",
        lambda **_kwargs: {"parameters": {}},
    )
    caplog.set_level("WARNING")

    response = (
        _make_app()
        .test_client()
        .post(
            "/api/settings/meta/test_model",
            json={"model": "muse-spark-1.3"},
        )
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is False
    assert payload["failure_kind"] == "billing_not_configured"
    assert payload["reason"] == "Meta API billing is not configured for this key."
    assert sensitive_detail not in response.get_data(as_text=True)
    assert sensitive_detail not in caplog.text


def test_settings_data_exposes_only_the_canonical_meta_key_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_routes, "get_all_settings_batch", dict)
    monkeypatch.setattr(settings_routes, "list_profile_ids_from_env", list)
    monkeypatch.setattr(settings_routes, "get_expert_tabs_enabled", lambda: False)
    monkeypatch.setattr(settings_routes, "get_expert_footer_enabled", lambda: False)

    with _make_app().app_context():
        settings = settings_routes.get_all_settings_data()

    assert settings["meta_api_key_env_var"] == "META_API_KEY"
    assert "model_api_key_env_var" not in settings
