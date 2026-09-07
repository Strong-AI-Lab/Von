"""An ordinary task can finish a message without inheriting workflow disposition."""

from unittest.mock import MagicMock

import pytest

from src.backend.integrations.google import gmail_service as gmail
from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp import catalogue as handlers
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import mail_profile_resource_vontology_service as profiles
from src.backend.services.adaptive_turn_service import (
    _model_visible_input_schema,
    _trusted_tool_payload,
    ordinary_turn_capability_delegation,
)


def _gateway():
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _payload():
    return _trusted_tool_payload(
        gateway=_gateway(),
        tool_name="gmail_modify_labels",
        model_payload={
            "profile": "foreign-mail",
            "message_id": "message-1",
            "add_labels": ["Label_done"],
            "remove_labels": [],
            "allow_mutation": False,
            "verify_after": False,
        },
        trusted_argument_values={"gmail_profile": "#V#gmail_profile_actor"},
    )


def test_ordinary_label_capability_binds_profile_and_verification():
    gateway = _gateway()
    assert "gmail_modify_labels" in ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#actor",
        trusted_argument_values={"gmail_profile": "#V#gmail_profile_actor"},
    )
    assert "gmail_modify_labels" not in ordinary_turn_capability_delegation(
        gateway, user_concept_id="#V#actor"
    )
    assert "gmail_modify_labels" not in ordinary_turn_capability_delegation(
        gateway, user_concept_id=None
    )
    schema = _model_visible_input_schema(
        gateway.get_method_definition("gmail_modify_labels")
    )
    assert not {"allow_mutation", "verify_after"}.intersection(schema["properties"])
    assert _payload() == {
        "profile": "#V#gmail_profile_actor",
        "message_id": "message-1",
        "add_labels": ["Label_done"],
        "remove_labels": [],
        "allow_mutation": True,
        "verify_after": True,
    }


@pytest.mark.parametrize("lost_response", [False, True])
def test_actor_bound_completion_preserves_inbox_and_reconciles_reply(
    monkeypatch, lost_response
):
    profile = gmail.GmailProfile(
        profile_id="actor-mail",
        token_path="/nonexistent/token.json",
        scopes=[gmail.MUTATION_SCOPE],
    )
    monkeypatch.setattr(
        gmail, "load_profiles_from_env", lambda: {"actor-mail": profile}
    )
    monkeypatch.setattr(gmail, "resolve_effective_profile_scopes", lambda p: p.scopes)

    def resolve(**kwargs):
        assert kwargs == {
            "user_concept_id": "#V#actor",
            "requested_profile_id": "#V#gmail_profile_actor",
        }
        return {
            "success": True,
            "profile_id": "actor-mail",
            "profile_resource_concept_id": "#V#gmail_profile_actor",
        }

    monkeypatch.setattr(profiles, "resolve_authorised_gmail_profile_for_user", resolve)
    api = MagicMock()
    messages = api.users.return_value.messages.return_value
    messages.modify.return_value.execute.return_value = {"id": "message-1"}
    if lost_response:
        messages.modify.return_value.execute.side_effect = TimeoutError("reply lost")
    messages.get.return_value.execute.return_value = {
        "id": "message-1",
        "labelIds": ["INBOX", "UNREAD", "Label_done"],
    }
    monkeypatch.setattr(gmail, "get_service", lambda *_: api)
    with override_current_actor("#V#actor", "#V#org"):
        result = handlers._gmail_modify_labels(**_payload())
    assert result["success"] is True
    assert result["gmail_label_state_verified"] is True
    assert result["modify_reconciled_after_error"] is lost_response
    assert result["readback_label_ids"] == ["INBOX", "UNREAD", "Label_done"]
    messages.modify.assert_called_once_with(
        userId="me",
        id="message-1",
        body={"addLabelIds": ["Label_done"], "removeLabelIds": []},
    )
    messages.get.assert_called_once_with(userId="me", id="message-1", format="minimal")


def test_label_handler_rechecks_profile_authority_before_effect(monkeypatch):
    monkeypatch.setattr(
        profiles,
        "resolve_authorised_gmail_profile_for_user",
        lambda **_: {
            "success": False,
            "reason_code": "mail_profile_not_authorised_for_actor",
        },
    )
    monkeypatch.setattr(
        gmail, "modify_labels", lambda **_: pytest.fail("denied effect")
    )
    with override_current_actor("#V#actor", "#V#org"):
        result = handlers._gmail_modify_labels(
            profile="foreign-mail",
            message_id="message-1",
            allow_mutation=True,
            add_labels=["Label_done"],
            verify_after=True,
        )
    assert result["error_code"] == "gmail_profile_not_authorised"
