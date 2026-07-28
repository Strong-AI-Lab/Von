from __future__ import annotations

from src.backend.services import settings_service


def test_buttonify_post_answer_model_call_is_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: None)
    monkeypatch.delenv("VON_BUTTONIFY_MODEL_ENABLE", raising=False)

    assert settings_service.get_buttonify_model_enabled() is False

    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")

    assert settings_service.get_buttonify_model_enabled() is True


def test_invalid_represented_buttonify_setting_fails_to_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        settings_service,
        "get_setting",
        lambda _name: "not-a-boolean",
    )

    assert settings_service.get_buttonify_model_enabled() is False
