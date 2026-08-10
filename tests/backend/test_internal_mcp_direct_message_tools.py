from __future__ import annotations

from typing import Any


def _message_doc(
    *,
    message_id: str = "#V#message_test",
    delivery_fingerprint: str | None = None,
) -> dict[str, Any]:
    metadata = {"attribution": "Sent by Von on behalf of #V#user_alice"}
    if delivery_fingerprint:
        metadata["delivery_fingerprint"] = delivery_fingerprint
    return {
        "concept_id": message_id,
        "relationships": {
            "is_an_instance_of": ["#V#direct_message"],
            "#V#has_sender": ["#V#user_alice"],
            "#V#has_recipient": ["#V#user_alice"],
        },
        "concept_data": {
            "message_status": "sent",
            "sent_at": "2026-08-06T10:00:00+00:00",
            "read_by": [],
            "subject": "Messaging test",
            "content_fallback": "Please confirm that this arrived.",
            "organisation_concept_id": "#V#org_test",
            "attribution": "Sent by Von on behalf of #V#user_alice",
            "metadata": metadata,
        },
    }


def test_message_send_direct_uses_trusted_actor_and_reconciles_duplicate(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import episode_logging_service, message_service

    persisted: dict[str, Any] = {}
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        message_service,
        "authorise_direct_message_participants",
        lambda **_kwargs: (True, []),
    )

    def fake_find_prior(*, sender_id: str, delivery_fingerprint: str):
        assert sender_id == "#V#user_alice"
        if persisted.get("fingerprint") == delivery_fingerprint:
            return persisted["message"]
        return None

    def fake_create_message(**kwargs):
        create_calls.append(dict(kwargs))
        fingerprint = kwargs["metadata"]["delivery_fingerprint"]
        message = _message_doc(delivery_fingerprint=fingerprint)
        persisted.update({"fingerprint": fingerprint, "message": message})
        return message

    monkeypatch.setattr(
        message_service,
        "get_message_for_delivery_fingerprint",
        fake_find_prior,
    )
    monkeypatch.setattr(message_service, "create_message", fake_create_message)
    monkeypatch.setattr(
        message_service,
        "get_message_for_user",
        lambda message_id, user_id: (
            persisted.get("message")
            if message_id == "#V#message_test" and user_id == "#V#user_alice"
            else None
        ),
    )
    monkeypatch.setattr(
        episode_logging_service,
        "log_episode",
        lambda **_kwargs: "episode-1",
    )

    arguments = {
        "recipient_ids": ["#V#user_alice"],
        "content": "Please confirm that this arrived.",
        "subject": "Messaging test",
        "acting_user_concept_id": "#V#user_alice",
        "organisation_concept_id": "#V#org_test",
        "request_id": "turn-1",
        "namespace": "#V#user_alice@org_test",
    }
    with override_current_actor("#V#user_alice", "#V#org_test"):
        first = catalogue_module._message_send_direct(**arguments)
        second = catalogue_module._message_send_direct(**arguments)

    assert first["success"] is True
    assert first["changed"] is True
    assert first["canonical_read_back"] == {
        "message_id": "#V#message_test",
        "sender_id": "#V#user_alice",
        "recipient_ids": ["#V#user_alice"],
        "subject": "Messaging test",
        "content": "Please confirm that this arrived.",
        "sent_at": "2026-08-06T10:00:00+00:00",
        "status": "sent",
        "read_by": [],
        "thread_id": None,
        "reply_to_id": None,
        "organisation_concept_id": "#V#org_test",
        "attribution": "Sent by Von on behalf of #V#user_alice",
    }
    assert second["success"] is True
    assert second["changed"] is False
    assert second["idempotent_replay"] is True
    assert len(create_calls) == 1
    assert create_calls[0]["sender_id"] == "#V#user_alice"
    assert create_calls[0]["org_id"] == "#V#org_test"


def test_message_send_direct_denies_participant_outside_actor_org(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import message_service

    monkeypatch.setattr(
        message_service,
        "authorise_direct_message_participants",
        lambda **_kwargs: (False, ["#V#user_outsider"]),
    )
    monkeypatch.setattr(
        message_service,
        "create_message",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("message must not be created")
        ),
    )

    with override_current_actor("#V#user_alice", "#V#org_test"):
        result = catalogue_module._message_send_direct(
            recipient_ids=["#V#user_outsider"],
            content="Private",
            acting_user_concept_id="#V#user_alice",
            organisation_concept_id="#V#org_test",
            request_id="turn-2",
            namespace="#V#user_alice@org_test",
        )

    assert result["success"] is False
    assert result["error_code"] == "message_participant_outside_organisation"
    assert result["error_details"]["invalid_concept_ids"] == ["#V#user_outsider"]


def test_message_get_direct_does_not_accept_same_org_nonparticipant(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import message_service

    monkeypatch.setattr(message_service, "get_message_for_user", lambda *_args: None)

    with override_current_actor("#V#user_charlie", "#V#org_test"):
        result = catalogue_module._message_get_direct(
            message_id="#V#message_test",
            acting_user_concept_id="#V#user_charlie",
            organisation_concept_id="#V#org_test",
            namespace="#V#user_charlie@org_test",
        )

    assert result["success"] is False
    assert result["error_code"] == "message_not_found"


def test_message_and_task_identity_arguments_are_server_bound():
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )
    from src.backend.services.adaptive_turn_service import _trusted_tool_payload

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    trusted = {
        "actor_user_concept_id": "#V#user_alice",
        "actor_organisation_concept_id": "#V#org_test",
        "turn_namespace": "#V#user_alice@org_test",
        "turn_id": "turn-trusted",
        "conversation_id": "conversation-trusted",
    }

    message_payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="message_send_direct",
        model_payload={
            "recipient_ids": ["#V#user_alice"],
            "content": "Test",
            "acting_user_concept_id": "#V#spoofed",
            "organisation_concept_id": "#V#spoofed_org",
            "request_id": "spoofed-turn",
            "namespace": "#V#spoofed@spoofed_org",
        },
        trusted_argument_values=trusted,
    )
    assert message_payload["acting_user_concept_id"] == "#V#user_alice"
    assert message_payload["organisation_concept_id"] == "#V#org_test"
    assert message_payload["request_id"] == "turn-trusted"
    assert message_payload["namespace"] == "#V#user_alice@org_test"

    task_payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="task_create",
        model_payload={
            "title": "Test task",
            "description": "Test",
            "assignee_id": "#V#spoofed",
            "created_by_concept_id": "#V#spoofed",
            "organisation_concept_id": "#V#spoofed_org",
            "originating_session_id": "spoofed-conversation",
            "report_to_concept_id": "#V#outsider",
        },
        trusted_argument_values=trusted,
    )
    assert task_payload["assignee_id"] == "#V#user_alice"
    assert task_payload["assignee_concept_id"] == "#V#user_alice"
    assert task_payload["created_by_concept_id"] == "#V#user_alice"
    assert task_payload["organisation_concept_id"] == "#V#org_test"
    assert task_payload["originating_session_id"] == "conversation-trusted"
    assert task_payload["acting_user_concept_id"] == "#V#user_alice"
    assert task_payload["request_id"] == "turn-trusted"
    assert task_payload["report_to_concept_id"] is None


def test_agent_visible_message_and_task_schemas_hide_trusted_identity_fields():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.services.adaptive_turn_service import _model_visible_input_schema

    catalogue = build_default_catalogue()
    expected_required = {
        "message_send_direct": {"recipient_ids", "content"},
        "task_create": {"title", "description"},
        "task_update_status": {"task_concept_id", "status"},
        "task_add_comment": {"task_concept_id", "body"},
    }
    hidden_fields = {
        "acting_user_concept_id",
        "organisation_concept_id",
        "request_id",
        "namespace",
        "author_concept_id",
        "assignee_id",
        "assignee_concept_id",
        "created_by_concept_id",
    }
    for tool_name, required_fields in expected_required.items():
        definition = catalogue.get(tool_name)
        assert definition is not None
        schema = _model_visible_input_schema(definition)
        assert set(schema.get("required") or []) == required_fields
        assert hidden_fields.isdisjoint(schema.get("properties") or {})


def _owned_task(*, status: str = "pending") -> dict[str, Any]:
    return {
        "task_concept_id": "#V#task_1",
        "title": "Agent task",
        "description": "Test",
        "status": status,
        "priority": "medium",
        "assignee_concept_id": "#V#user_alice",
        "created_by_concept_id": "#V#user_alice",
        "organisation_concept_id": "#V#org_test",
    }


def _task_actor_arguments() -> dict[str, str]:
    return {
        "acting_user_concept_id": "#V#user_alice",
        "organisation_concept_id": "#V#org_test",
        "namespace": "#V#user_alice@org_test",
    }


def test_task_status_denies_same_org_task_not_owned_by_actor(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    monkeypatch.setattr(
        task_management_service,
        "get_task",
        lambda _task_id: {
            **_owned_task(),
            "assignee_concept_id": "#V#user_bob",
            "created_by_concept_id": "#V#user_bob",
        },
    )
    monkeypatch.setattr(
        task_management_service,
        "update_task_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthorised task must not be updated")
        ),
    )

    with override_current_actor("#V#user_alice", "#V#org_test"):
        result = catalogue_module._task_update_status(
            task_concept_id="#V#task_1",
            status="completed",
            **_task_actor_arguments(),
        )

    assert result["success"] is False
    assert result["error_code"] == "task_actor_scope_denied"


def test_task_status_retry_reports_no_change(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    state = {
        **_owned_task(),
        "created_by_concept_id": "#V#user_bob",
    }
    update_calls = {"count": 0}
    observed_actor: dict[str, Any] = {}
    monkeypatch.setattr(task_management_service, "get_task", lambda _task_id: dict(state))

    def fake_update(_task_id: str, status: str, *, actor_concept_id=None):
        update_calls["count"] += 1
        observed_actor["actor_concept_id"] = actor_concept_id
        state["status"] = status
        return dict(state)

    monkeypatch.setattr(task_management_service, "update_task_status", fake_update)
    arguments = {
        "task_concept_id": "#V#task_1",
        "status": "completed",
        **_task_actor_arguments(),
    }
    with override_current_actor("#V#user_alice", "#V#org_test"):
        first = catalogue_module._task_update_status(**arguments)
        second = catalogue_module._task_update_status(**arguments)

    assert first["success"] is True
    assert first["changed"] is True
    assert first["canonical_read_back"]["status"] == "completed"
    assert second["success"] is True
    assert second["changed"] is False
    assert second["idempotent_replay"] is True
    assert update_calls["count"] == 1
    assert observed_actor["actor_concept_id"] == "#V#user_alice"


def test_task_comment_retry_reuses_canonical_comment(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    persisted: dict[str, Any] = {}
    add_calls = {"count": 0}
    monkeypatch.setattr(
        task_management_service,
        "get_task",
        lambda _task_id: _owned_task(),
    )
    monkeypatch.setattr(
        task_management_service,
        "find_task_comment_by_effect_fingerprint",
        lambda _task_id, fingerprint: (
            persisted.get("comment") if persisted.get("fingerprint") == fingerprint else None
        ),
    )

    def fake_add(_task_id: str, *, body: str, author_concept_id=None, source=None):
        add_calls["count"] += 1
        comment = {
            "comment_id": "comment_1",
            "body": body,
            "author_concept_id": author_concept_id,
            "source": source,
        }
        persisted.update(
            {
                "comment": comment,
                "fingerprint": source["effect_fingerprint"],
            }
        )
        return comment

    monkeypatch.setattr(task_management_service, "add_task_comment", fake_add)
    monkeypatch.setattr(
        task_management_service,
        "get_task_comment",
        lambda _task_id, _comment_id: persisted.get("comment"),
    )
    arguments = {
        "task_concept_id": "#V#task_1",
        "body": "Canonical task comment read-back confirmed.",
        "request_id": "turn-1",
        **_task_actor_arguments(),
    }
    with override_current_actor("#V#user_alice", "#V#org_test"):
        first = catalogue_module._task_add_comment(**arguments)
        second = catalogue_module._task_add_comment(**arguments)

    assert first["success"] is True
    assert first["changed"] is True
    assert second["success"] is True
    assert second["changed"] is False
    assert second["idempotent_replay"] is True
    assert add_calls["count"] == 1


def test_task_create_retry_reuses_canonical_task(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    persisted: dict[str, Any] = {}
    create_calls = {"count": 0}

    def fake_find(**kwargs):
        if persisted.get("fingerprint") == kwargs["creation_fingerprint"]:
            return persisted.get("task")
        return None

    def fake_create(**kwargs):
        create_calls["count"] += 1
        task = _owned_task()
        persisted.update(
            {
                "task": task,
                "fingerprint": kwargs["agent_creation_fingerprint"],
            }
        )
        return task

    monkeypatch.setattr(
        task_management_service,
        "find_task_by_agent_creation_fingerprint",
        fake_find,
    )
    monkeypatch.setattr(task_management_service, "create_task", fake_create)
    monkeypatch.setattr(
        task_management_service,
        "get_task",
        lambda _task_id: persisted.get("task"),
    )
    arguments = {
        "title": "Agent task",
        "description": "Test",
        "assignee_id": "#V#user_alice",
        "created_by_concept_id": "#V#user_alice",
        "request_id": "turn-1",
        "originating_session_id": "conversation-1",
        **_task_actor_arguments(),
    }
    with override_current_actor("#V#user_alice", "#V#org_test"):
        first = catalogue_module._task_create(**arguments)
        second = catalogue_module._task_create(**arguments)

    assert first["success"] is True
    assert first["changed"] is True
    assert second["success"] is True
    assert second["changed"] is False
    assert second["idempotent_replay"] is True
    assert create_calls["count"] == 1


def test_task_create_actor_scoped_idempotency_key_reuses_task_without_turn(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    persisted: dict[str, Any] = {}
    create_calls = {"count": 0}

    def fake_find(**kwargs):
        if persisted.get("fingerprint") == kwargs["creation_fingerprint"]:
            return persisted.get("task")
        return None

    def fake_create(**kwargs):
        create_calls["count"] += 1
        task = {
            **_owned_task(),
            "title": kwargs["title"],
            "description": kwargs["description"],
        }
        persisted.update(
            {
                "task": task,
                "fingerprint": kwargs["agent_creation_fingerprint"],
                "request_id": kwargs["agent_creation_request_id"],
            }
        )
        return task

    monkeypatch.setattr(
        task_management_service,
        "find_task_by_agent_creation_fingerprint",
        fake_find,
    )
    monkeypatch.setattr(task_management_service, "create_task", fake_create)
    monkeypatch.setattr(
        task_management_service,
        "get_task",
        lambda _task_id: persisted.get("task"),
    )
    base_arguments = {
        "title": "Review source item",
        "description": "Initial task text",
        "assignee_id": "#V#user_alice",
        "created_by_concept_id": "#V#user_alice",
        "idempotency_key": "paper-review:gmail:profile-a:message-1",
        **_task_actor_arguments(),
    }
    with override_current_actor("#V#user_alice", "#V#org_test"):
        first = catalogue_module._task_create(**base_arguments)
        second = catalogue_module._task_create(
            **{
                **base_arguments,
                "title": "Changed presentation text",
                "description": "A later workflow release may phrase this differently.",
            }
        )

    assert first["success"] is True
    assert first["changed"] is True
    assert second["success"] is True
    assert second["changed"] is False
    assert second["idempotent_replay"] is True
    assert create_calls["count"] == 1
    assert str(persisted["request_id"]).startswith("idempotency:")


def test_task_create_actor_scoped_idempotency_key_does_not_cross_actor(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    fingerprints: list[str] = []
    tasks: dict[str, dict[str, Any]] = {}

    monkeypatch.setattr(
        task_management_service,
        "find_task_by_agent_creation_fingerprint",
        lambda **_kwargs: None,
    )

    def fake_create(**kwargs):
        fingerprints.append(kwargs["agent_creation_fingerprint"])
        task = {
            **_owned_task(),
            "task_concept_id": f"#V#task_{len(fingerprints)}",
            "title": kwargs["title"],
            "description": kwargs["description"],
            "assignee_concept_id": kwargs["assignee_concept_id"],
            "created_by_concept_id": kwargs["created_by_concept_id"],
        }
        tasks[task["task_concept_id"]] = task
        return task

    monkeypatch.setattr(task_management_service, "create_task", fake_create)
    monkeypatch.setattr(
        task_management_service,
        "get_task",
        lambda task_id: tasks[task_id],
    )

    for actor in ("#V#user_alice", "#V#user_bob"):
        with override_current_actor(actor, "#V#org_test"):
            result = catalogue_module._task_create(
                title="Review source item",
                description="Review it",
                assignee_id=actor,
                created_by_concept_id=actor,
                acting_user_concept_id=actor,
                organisation_concept_id="#V#org_test",
                idempotency_key="paper-review:gmail:profile-a:message-1",
            )
        assert result["success"] is True

    assert len(fingerprints) == 2
    assert fingerprints[0] != fingerprints[1]


def test_task_create_reconciles_late_first_write_after_create_failure(monkeypatch):
    """A retry must read back a task committed after its first lookup."""
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    canonical_task = _owned_task()
    find_calls = {"count": 0}

    def fake_find(**_kwargs):
        find_calls["count"] += 1
        return None if find_calls["count"] == 1 else canonical_task

    def fake_create(**_kwargs):
        raise task_management_service.TaskManagementError("duplicate concept_id")

    monkeypatch.setattr(
        task_management_service,
        "find_task_by_agent_creation_fingerprint",
        fake_find,
    )
    monkeypatch.setattr(task_management_service, "create_task", fake_create)
    arguments = {
        "title": "Agent task",
        "description": "Test",
        "assignee_id": "#V#user_alice",
        "created_by_concept_id": "#V#user_alice",
        "request_id": "turn-1",
        "originating_session_id": "conversation-1",
        **_task_actor_arguments(),
    }

    with override_current_actor("#V#user_alice", "#V#org_test"):
        result = catalogue_module._task_create(**arguments)

    assert find_calls["count"] == 2
    assert result["success"] is True
    assert result["changed"] is False
    assert result["idempotent_replay"] is True
    assert result["canonical_read_back"] == canonical_task
