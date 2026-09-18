"""Operator opt-in and bound-principal behaviour, independent of live credentials."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts.codex_von_access import launch_access_args, operator_access
from scripts.codex_von_mcp import BoundTools, load_binding


def test_ordinary_workers_keep_their_existing_boundaries():
    assert launch_access_args({}, inbox=True) == [
        "--sandbox",
        "read-only",
        "-c",
        "mcp_servers.von.enabled=false",
    ]
    assert launch_access_args({})[:2] == ["--sandbox", "workspace-write"]
    assert 'default_permissions="von-coding"' in launch_access_args(
        {"permission_profile": "von-coding"}
    )
    assert operator_access({}) is None


@pytest.mark.parametrize("inbox", [False, True])
def test_operator_has_the_same_explicit_access_in_both_routes(inbox):
    args = launch_access_args(
        {
            "operator_access": {
                "enabled": True,
                "mcp_command": [
                    "/operator/python",
                    "/operator/mcp.py",
                    "--binding",
                    "/private/binding.json",
                ],
            }
        },
        inbox=inbox,
    )
    assert args[:2] == ["--sandbox", "danger-full-access"]
    assert "mcp_servers.von_operator.enabled=true" in args
    assert 'mcp_servers.von_operator.command="/operator/python"' in args
    assert "mcp_servers.von.enabled=false" in args
    assert not any("mcp_servers.von.enabled=true" in value for value in args)


@pytest.mark.parametrize(
    "profile",
    [True, {}, {"enabled": False}, {"enabled": True, "mcp_command": ["relative"]}],
)
def test_malformed_operator_profile_fails_closed(profile):
    with pytest.raises(ValueError):
        launch_access_args({"operator_access": profile})


def test_binding_must_be_operator_owned_and_not_shared_writable(tmp_path):
    path = tmp_path / "binding.json"
    path.write_text(
        json.dumps(
            {
                "actor_id": "#V#manager",
                "organisation_id": "#V#org",
                "methods": ["task_get"],
            }
        )
    )
    path.chmod(0o600)
    assert load_binding(path)["actor_id"] == "#V#manager"
    path.chmod(0o666)
    with pytest.raises(PermissionError):
        load_binding(path)


def test_only_installed_methods_reach_canonical_gateway():
    calls = []

    class Catalogue:
        def get(self, name):
            assert name == "task_get"

    gateway = SimpleNamespace(
        invoke=lambda name, args: calls.append((name, args))
        or SimpleNamespace(payload={"success": True})
    )
    bound = BoundTools(
        {
            "actor_id": "#V#manager",
            "organisation_id": "#V#org",
            "methods": ["task_get"],
        },
        Catalogue(),
        gateway,
    )

    @contextmanager
    def actor():
        yield

    bound.actor = actor
    with pytest.raises(PermissionError):
        bound.call("delete_database", {})
    assert calls == []
    assert bound.call("task_get", {"task_concept_id": "#V#task"}) == {"success": True}
    assert calls == [("task_get", {"task_concept_id": "#V#task"})]
    assert (
        bound.call("von_context", {"actor_id": "#V#michael"})["actor_id"]
        == "#V#manager"
    )


def test_conflicting_actor_is_rejected_before_canonical_call():
    catalogue = SimpleNamespace(get=lambda name: None)
    gateway = SimpleNamespace(
        invoke=lambda *a: pytest.fail("conflicting actor reached handler")
    )
    bound = BoundTools(
        {
            "actor_id": "#V#manager",
            "organisation_id": "#V#org",
            "methods": ["task_get"],
        },
        catalogue,
        gateway,
    )
    assert bound.call("task_get", {"acting_user_concept_id": "#V#owner"}) == {
        "success": False,
        "error_code": "operator_actor_mismatch",
    }


@pytest.mark.parametrize("operator", [True, False])
def test_real_gateway_preserves_installed_operator_task_authority(
    monkeypatch, operator
):
    from contextlib import nullcontext
    from src.backend.integrations.internal_mcp.catalogue import (
        _authorise_task_actor_mutation,
    )
    from src.backend.integrations.internal_mcp.gateway import (
        InternalMCPGateway,
        MethodCatalogue,
        MethodDefinition,
        INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.integrations.internal_mcp.schemas import Schema
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service

    task = {
        "task_concept_id": "#V#task",
        "organisation_concept_id": "#V#org",
        "assignee_concept_id": "#V#other_agent",
        "created_by_concept_id": "#V#owner",
    }
    monkeypatch.setattr(task_management_service, "get_task", lambda _: task)

    def handler(**kwargs):
        actor, found, error = _authorise_task_actor_mutation(
            kwargs, surface="task_add_comment"
        )
        return error or {
            "success": True,
            "actor": actor.user_concept_id,
            "task": found["task_concept_id"],
        }

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="task_authority_probe",
            handler=handler,
            input_schema=Schema(required={}, optional={}, allow_unknown=True),
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue, transport=InternalMCPTransport(), enabled=True
    )
    provenance = (
        bind_internal_mcp_actor_context_source(
            INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
            preexisting_actor_context=("#V#manager", "#V#org"),
        )
        if operator
        else nullcontext()
    )
    with override_current_actor("#V#manager", "#V#org"), provenance:
        result = gateway.invoke(
            "task_authority_probe",
            {
                "task_concept_id": "#V#task",
                "actor_context_source": INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
            },
        ).payload
    if operator:
        assert result == {"success": True, "actor": "#V#manager", "task": "#V#task"}
    else:
        assert result["success"] is False
        assert result["error_code"] == "task_actor_scope_denied"
