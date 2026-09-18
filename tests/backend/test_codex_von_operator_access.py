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
    assert "mcp_servers.von.enabled=true" in args
    assert 'mcp_servers.von.command="/operator/python"' in args
    assert not any("enabled=false" in value for value in args)


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
