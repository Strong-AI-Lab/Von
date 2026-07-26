from __future__ import annotations

from src.backend.services.coding_agent_mcp_access_profile_service import (
    build_coding_agent_mcp_access_profile,
)


def _patch_db_environment(
    monkeypatch,
    *,
    db_name: str,
    running_under_pytest: bool,
) -> None:
    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_configured_database_name",
        lambda: db_name,
    )
    monkeypatch.setattr(
        "src.backend.db.mongo_client._is_running_under_pytest",
        lambda: running_under_pytest,
    )


def test_access_profile_enables_writes_for_noncanonical_local_db(monkeypatch) -> None:
    _patch_db_environment(
        monkeypatch,
        db_name="dev_von_db",
        running_under_pytest=False,
    )
    monkeypatch.delenv("VON_MCP_ALLOW_WRITES", raising=False)

    payload = build_coding_agent_mcp_access_profile()

    assert payload["success"] is True
    assert payload["environment"]["authority_state"] == "local_noncanonical"
    assert payload["environment"]["authority_kind"] == "dev_like"
    assert payload["shared_authority_write_policy"]["mode"] == (
        "enabled_noncanonical_local"
    )
    assert (
        payload["shared_authority_write_policy"]["write_category_tools_allowed"] is True
    )
    assert payload["von_chat_run_policy"]["default_allow_writes"] is False
    assert payload["von_chat_run_policy"]["default_dry_run"] is True


def test_access_profile_blocks_canonical_primary_without_explicit_approval(
    monkeypatch,
) -> None:
    _patch_db_environment(
        monkeypatch,
        db_name="von_db",
        running_under_pytest=False,
    )
    monkeypatch.delenv("VON_MCP_ALLOW_WRITES", raising=False)

    payload = build_coding_agent_mcp_access_profile()

    assert payload["environment"]["authority_state"] == "canonical_primary"
    assert payload["environment"]["authority_kind"] == "prod_like"
    assert payload["shared_authority_write_policy"]["mode"] == (
        "blocked_canonical_primary_requires_explicit_approval"
    )
    assert (
        payload["shared_authority_write_policy"]["write_category_tools_allowed"]
        is False
    )
    assert payload["von_chat_run_policy"]["default_allow_writes"] is False
    assert payload["von_chat_run_policy"]["default_dry_run"] is True


def test_access_profile_enables_test_isolated_writes_without_targeting_primary_db(
    monkeypatch,
) -> None:
    _patch_db_environment(
        monkeypatch,
        db_name="test_von_db",
        running_under_pytest=True,
    )
    monkeypatch.delenv("VON_MCP_ALLOW_WRITES", raising=False)

    payload = build_coding_agent_mcp_access_profile()

    assert payload["environment"]["authority_state"] == "test_isolated"
    assert payload["environment"]["authority_kind"] == "test_like"
    assert payload["shared_authority_write_policy"]["mode"] == "enabled_test_isolated"
    assert (
        payload["shared_authority_write_policy"]["write_category_tools_allowed"] is True
    )
    assert payload["environment"]["configured_database_name"] == "test_von_db"
    assert payload["von_chat_run_policy"]["default_allow_writes"] is False
    assert payload["von_chat_run_policy"]["default_dry_run"] is True


def test_access_profile_allows_canonical_primary_writes_after_explicit_approval(
    monkeypatch,
) -> None:
    _patch_db_environment(
        monkeypatch,
        db_name="von_db",
        running_under_pytest=False,
    )
    monkeypatch.setenv("VON_MCP_ALLOW_WRITES", "1")

    payload = build_coding_agent_mcp_access_profile()

    assert payload["environment"]["authority_state"] == "canonical_primary"
    assert payload["shared_authority_write_policy"]["mode"] == (
        "enabled_canonical_primary_with_explicit_approval"
    )
    assert (
        payload["shared_authority_write_policy"]["write_category_tools_allowed"] is True
    )
