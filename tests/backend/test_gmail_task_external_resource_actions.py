from types import SimpleNamespace

import pytest

from src.backend.integrations.google import (
    gmail_task_external_resource_actions as provider,
)
from src.backend.services.task_external_resource_action_service import (
    TaskExternalResourceActionAccessError,
)


def _gmail_task() -> dict[str, str]:
    return {
        "evidence": (
            "source_system=gmail; source_profile=research-gmail; "
            "source_item_id=19f257e5c6caaba"
        )
    }


def test_list_actions_projects_only_exact_gmail_source_evidence() -> None:
    assert provider.list_actions(_gmail_task()) == [
        {
            "action_id": "gmail.open_source",
            "kind": "open_resource",
            "label": "Open source in Gmail",
            "source_system": "gmail",
            "resource_type": "gmail_thread",
        }
    ]

    assert (
        provider.list_actions(
            {"evidence": "source_system=gmail; source_profile=research-gmail"}
        )
        == []
    )
    assert (
        provider.list_actions(
            {
                "evidence": (
                    "source_system=mail; source_profile=research-gmail; "
                    "source_item_id=19f257e5c6caaba"
                )
            }
        )
        == []
    )
    assert (
        provider.list_actions(
            {
                "evidence": (
                    "source_system=gmail; source_profile=research-gmail; "
                    "source_item_id=one; source_item_id=two"
                )
            }
        )
        == []
    )


def test_resolve_action_uses_authorised_profile_and_encodes_thread_target(
    monkeypatch,
) -> None:
    from src.backend.integrations.google import gmail_service
    from src.backend.services import (
        gmail_profile_invocation_authority_service as authority_service,
    )

    observed: dict[str, object] = {}
    monkeypatch.setattr(
        authority_service,
        "authorise_gmail_profile_for_invocation",
        lambda profile: SimpleNamespace(
            profile_id=f"canonical-{profile}",
            audit_namespace="actor:namespace",
        ),
    )

    def fake_target(**kwargs):
        observed.update(kwargs)
        return {
            "authorised_email": "Research+Mail@example.test",
            "thread_id": "thread/one",
        }

    monkeypatch.setattr(
        gmail_service,
        "resolve_message_thread_navigation_target",
        fake_target,
    )

    assert provider.resolve_action(_gmail_task(), "gmail.open_source") == (
        "https://mail.google.com/mail/?authuser="
        "Research%2BMail%40example.test#all/thread%2Fone"
    )
    assert observed == {
        "profile_id": "canonical-research-gmail",
        "message_id": "19f257e5c6caaba",
        "audit_context": {
            "namespace": "actor:namespace",
            "source": "task_external_resource_action",
            "action": "gmail.open_source",
        },
    }
    assert provider.resolve_action(_gmail_task(), "gmail.delete_source") is None


def test_resolve_action_converts_gmail_authority_denial_to_generic_access_error(
    monkeypatch,
) -> None:
    from src.backend.services import (
        gmail_profile_invocation_authority_service as authority_service,
    )

    def deny(_profile):
        raise authority_service.GmailInvocationAuthorityError(
            reason_code="gmail_profile_not_authorised",
            safe_message="The requested Gmail profile is not authorised.",
        )

    monkeypatch.setattr(
        authority_service,
        "authorise_gmail_profile_for_invocation",
        deny,
    )

    with pytest.raises(TaskExternalResourceActionAccessError) as error:
        provider.resolve_action(_gmail_task(), "gmail.open_source")

    assert error.value.reason_code == "gmail_profile_not_authorised"
    assert str(error.value) == "The requested Gmail profile is not authorised."
