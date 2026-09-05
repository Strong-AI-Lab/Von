"""Exercise account ownership at discovery and at the actual Jira RPC boundary."""

import asyncio

import pytest

from src.backend.integrations.internal_mcp import jira_proxy_mcp as jira
from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import von_user_authentication_service as identity
from src.backend.services.adaptive_turn_service import (
    _model_visible_input_schema,
    ordinary_turn_capability_delegation,
)

OWNER = "#V#jira_owner"
OTHER = "#V#other_actor"
AUTH = {
    "base_url": "https://example.atlassian.net",
    "email": "owner@example.com",
    "token_present": True,
}
READS = (
    ("jira_search", {"jql": "project = PROJ ORDER BY created DESC"}),
    ("jira_get_issue", {"issue_key": "PROJ-1"}),
    ("jira_get_comments", {"issue_key": "PROJ-1"}),
)


@pytest.fixture
def account(monkeypatch):
    state = {"owner": OWNER, "calls": []}
    monkeypatch.setattr(jira, "inspect_jira_auth_config", lambda: dict(AUTH))

    def lookup(email):
        assert email == AUTH["email"]
        return {"concept_id": state["owner"]} if state["owner"] else None

    monkeypatch.setattr(identity, "find_user_concept_by_login_email", lookup)
    proxy = jira.JiraMCPProxy(
        jira.JiraProxyConfig(
            command="unused",
            args=[],
            env={
                "ATLASSIAN_BASE_URL": AUTH["base_url"],
                "ATLASSIAN_EMAIL": AUTH["email"],
                "ATLASSIAN_API_TOKEN": "test-token",
            },
        )
    )

    async def call(tool, arguments):
        state["calls"].append((tool, arguments))
        return {"issues": [], "key": "PROJ-1", "comments": []}

    monkeypatch.setattr(proxy._client, "call_tool", call)

    async def get_proxy():
        return proxy

    monkeypatch.setattr(jira, "get_jira_proxy", get_proxy)
    state["proxy"] = proxy
    return state


def gateway(*, operator=False):
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=operator,
    )


def test_governed_owner_binding_projects_only_the_three_read_capabilities(account):
    resource = jira.jira_resource_binding_for_user(OWNER)
    assert resource
    assert jira.jira_resource_binding_for_user(OTHER) is None
    assert jira.jira_resource_binding_for_user(None) is None
    g = gateway()
    names = ordinary_turn_capability_delegation(
        g, user_concept_id=OWNER, trusted_argument_values={"jira_resource_id": resource}
    )
    assert all(name in names for name, _ in READS)
    assert "jira_create_issue" not in names
    assert "jira_get_auth_config" not in names
    without = ordinary_turn_capability_delegation(g, user_concept_id=OTHER)
    assert all(name not in without for name, _ in READS)
    for name, _ in READS:
        schema = _model_visible_input_schema(
            g.get_method_definition(name), {"jira_resource_id": resource}
        )
        assert "resource_id" not in schema["properties"]


@pytest.mark.parametrize("name,arguments", READS)
@pytest.mark.parametrize("with_selector", [True, False])
def test_owner_read_through_gateway_or_workflow_keeps_exact_query(
    account, name, arguments, with_selector
):
    payload = dict(arguments)
    if with_selector:
        payload["resource_id"] = jira.jira_resource_binding_for_user(OWNER)
    with override_current_actor(OWNER, None):
        result = gateway().invoke(name, payload).payload
    assert result["authority"]["actor_concept_id"] == OWNER
    assert account["calls"] == [(name, arguments)]


@pytest.mark.parametrize("name,arguments", READS)
@pytest.mark.parametrize("actor", [OTHER, None])
def test_other_actor_and_payload_impersonation_denied_before_rpc(
    account, name, arguments, actor
):
    with override_current_actor(actor, None):
        result = (
            gateway()
            .invoke(
                name,
                {
                    **arguments,
                    "user_concept_id": OWNER,
                    "namespace": "jira_owner",
                    "resource_id": jira.jira_resource_binding_for_user(OWNER),
                },
            )
            .payload
        )
    assert result["error_code"] == (
        "jira_resource_not_authorised"
        if actor
        else "authenticated_actor_context_required"
    )
    assert account["calls"] == []


def test_direct_durable_proxy_call_rechecks_actor_without_gateway(account):
    with (
        override_current_actor(OTHER, None),
        pytest.raises(jira.JiraInvocationAuthorityError),
    ):
        asyncio.run(account["proxy"].search(jql="project = PROJ"))
    assert account["calls"] == []
    with override_current_actor(OWNER, None):
        result = asyncio.run(account["proxy"].search(jql="project = PROJ"))
    assert result["authority"]["actor_concept_id"] == OWNER


@pytest.mark.parametrize("new_owner", [None, OTHER])
def test_revoked_or_reassigned_binding_cannot_use_previous_selector(account, new_owner):
    resource = jira.jira_resource_binding_for_user(OWNER)
    account["owner"] = new_owner
    with override_current_actor(OWNER, None):
        result = (
            gateway()
            .invoke("jira_get_issue", {"issue_key": "PROJ-1", "resource_id": resource})
            .payload
        )
    assert result["error_code"] == "jira_resource_not_authorised"
    assert account["calls"] == []


def test_ambiguous_or_unavailable_identity_fails_closed(account, monkeypatch):
    def unavailable(_email):
        raise RuntimeError("private identity detail must not be returned")

    monkeypatch.setattr(identity, "find_user_concept_by_login_email", unavailable)
    assert jira.jira_resource_binding_for_user(OWNER) is None
    with override_current_actor(OWNER, None):
        result = gateway().invoke("jira_get_issue", {"issue_key": "PROJ-1"}).payload
    assert result["error_code"] == "jira_account_binding_unavailable"
    assert "private identity detail" not in str(result)
    assert account["calls"] == []


def test_stale_account_selector_denied(account):
    with override_current_actor(OWNER, None):
        result = (
            gateway()
            .invoke(
                "jira_get_issue", {"issue_key": "PROJ-1", "resource_id": "old-account"}
            )
            .payload
        )
    assert result["error_code"] == "jira_resource_not_authorised"
    assert account["calls"] == []


def test_explicit_local_operator_remains_available_without_login_binding(account):
    account["owner"] = None
    with override_current_actor(None, None):
        result = (
            gateway(operator=True)
            .invoke("jira_get_issue", {"issue_key": "PROJ-1"})
            .payload
        )
    assert result["authority"]["principal_kind"] == "trusted_local_operator"


def test_jira_authentication_failure_is_not_relabelled_as_actor_denial(
    account, monkeypatch
):
    async def denied(*_args):
        return {"success": False, "status_code": 401, "error": "Unauthorised"}

    monkeypatch.setattr(account["proxy"]._client, "call_tool", denied)
    with override_current_actor(OWNER, None):
        result = gateway().invoke("jira_get_issue", {"issue_key": "PROJ-1"}).payload
    assert result["status_code"] == 401
    assert result["success"] is False


def test_proxy_cache_tracks_actual_account_and_credential_configuration(monkeypatch):
    env = {
        "ATLASSIAN_BASE_URL": AUTH["base_url"],
        "ATLASSIAN_EMAIL": AUTH["email"],
        "ATLASSIAN_API_TOKEN": "first",
    }
    monkeypatch.setattr(jira, "_proxy_instance", None)
    monkeypatch.setattr(
        jira,
        "_build_jira_config",
        lambda: jira.JiraProxyConfig(command="unused", args=[], env=dict(env)),
    )
    first = asyncio.run(jira.get_jira_proxy())
    assert asyncio.run(jira.get_jira_proxy()) is first
    env["ATLASSIAN_EMAIL"] = "changed@example.com"
    changed = asyncio.run(jira.get_jira_proxy())
    assert changed is not first
    env["ATLASSIAN_API_TOKEN"] = "rotated"
    assert asyncio.run(jira.get_jira_proxy()) is not changed


def test_operator_migration_cli_binds_authority_only_around_its_runner(
    monkeypatch, capsys
):
    from pathlib import Path
    from types import SimpleNamespace
    from scripts import run_jira_task_migration as cli
    from src.backend.integrations.internal_mcp.gateway import (
        internal_mcp_actor_context_is_trusted_local_operator,
    )

    monkeypatch.setattr(
        cli, "_parse_args", lambda: SimpleNamespace(report_path=Path("unused.json"))
    )
    calls = []

    def run(_options):
        assert internal_mcp_actor_context_is_trusted_local_operator()
        calls.append(True)
        return {"dry_run": True}

    monkeypatch.setattr(cli, "run_jira_task_migration_sync", run)
    cli.main()
    assert calls == [True]
    assert not internal_mcp_actor_context_is_trusted_local_operator()
    assert '"dry_run": true' in capsys.readouterr().out
