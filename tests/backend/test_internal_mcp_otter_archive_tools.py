"""Owner-scope and protocol-adapter tests for private OtterArchive retrieval."""

from pathlib import Path

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.otter_archive_proxy_mcp import (
    OTTER_ARCHIVE_DATABASE_ENV,
    OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV,
    OTTER_ARCHIVE_PROJECT_DIR_ENV,
    _build_otter_archive_config,
    otter_archive_resource_binding_for_user,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services.adaptive_turn_service import (
    _model_visible_input_schema,
    ordinary_turn_capability_delegation,
)

OWNER_ID = "#V#otter_archive_owner"
OTHER_ID = "#V#other_user"
RESOURCE_ID = "personal_otter_archive"


def _configure_owner(monkeypatch) -> None:
    monkeypatch.setenv(OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV, OWNER_ID)


def test_archive_tools_are_registered_without_removing_linkedin():
    from src.backend.integrations.internal_mcp.otter_archive_tools import TOOL_SPECS

    names = set(build_default_catalogue().list_methods())
    assert {"otter_archive_" + name for name in TOOL_SPECS} <= names
    assert "linkedin_index_status" in names
    assert {"otter_collect_now", "otter_collection_status"} <= names


def test_resource_binding_is_returned_only_for_configured_owner():
    env = {OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV: OWNER_ID}

    assert (
        otter_archive_resource_binding_for_user(OWNER_ID, source_env=env) == RESOURCE_ID
    )
    assert otter_archive_resource_binding_for_user(OTHER_ID, source_env=env) is None
    assert otter_archive_resource_binding_for_user(None, source_env=env) is None


def test_otter_archive_tools_are_projected_only_with_server_bound_resource():
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    without_binding = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id=OWNER_ID,
        trusted_argument_values={},
    )
    with_binding = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id=OWNER_ID,
        trusted_argument_values={"otter_archive_resource_id": RESOURCE_ID},
    )

    assert "otter_archive_search_archive" not in without_binding
    assert "otter_archive_search_archive" in with_binding
    assert "otter_collect_now" in with_binding
    assert "otter_collection_status" in with_binding
    assert "otter_collect_now" not in without_binding

    definition = gateway.get_method_definition("otter_archive_search_archive")
    assert definition is not None
    visible_schema = _model_visible_input_schema(
        definition,
        {"otter_archive_resource_id": RESOURCE_ID},
    )
    assert "resource_id" not in visible_schema["properties"]


def test_proxy_config_uses_one_database_and_minimal_environment(tmp_path: Path):
    project = tmp_path / "OtterArchiveMCP"
    executable = project / ".venv" / "bin" / "otter-archive-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    database = tmp_path / "otter_archive.sqlite3"
    database.write_bytes(b"sqlite-placeholder")
    env = {
        OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV: OWNER_ID,
        OTTER_ARCHIVE_PROJECT_DIR_ENV: str(project),
        OTTER_ARCHIVE_DATABASE_ENV: str(database),
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "must-not-cross-boundary",
    }

    config = _build_otter_archive_config(resource_id=RESOURCE_ID, source_env=env)

    assert config.database == database.resolve()
    assert config.command == str(executable.resolve())
    assert config.args == []
    assert config.env[OTTER_ARCHIVE_DATABASE_ENV] == str(database.resolve())
    assert "OPENAI_API_KEY" not in config.env


def test_authenticated_owner_can_call_and_receives_authority_receipt(monkeypatch):
    _configure_owner(monkeypatch)

    class FakeProxy:
        async def call(self, tool_name, arguments):
            assert tool_name == "search_archive"
            assert arguments == {"query": "DeepMind", "limit": 5}
            return {"count": 1, "results": [{"person": "Ada"}]}

    async def fake_get_otter_archive_proxy(*, resource_id):
        assert resource_id == RESOURCE_ID
        return FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.otter_archive_proxy_mcp.get_otter_archive_proxy",
        fake_get_otter_archive_proxy,
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    with override_current_actor(OWNER_ID, None):
        result = gateway.invoke(
            "otter_archive_search_archive",
            {
                "resource_id": RESOURCE_ID,
                "query": "DeepMind",
                "limit": 5,
            },
        ).payload

    assert result["success"] is True
    assert result["count"] == 1
    assert result["authority"] == {
        "schema_version": "otter_archive_resource_authority.v1",
        "authorised": True,
        "principal_kind": "authenticated_actor",
        "actor_user_concept_id": OWNER_ID,
        "resource_id": RESOURCE_ID,
        "grant_source": "deployment_owner_binding",
        "access": "read_only",
    }


def test_other_actor_is_denied_before_proxy_launch(monkeypatch):
    _configure_owner(monkeypatch)
    proxy_called = False

    async def fake_get_otter_archive_proxy(*, resource_id):
        nonlocal proxy_called
        proxy_called = True
        raise AssertionError("private subprocess must not start")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.otter_archive_proxy_mcp.get_otter_archive_proxy",
        fake_get_otter_archive_proxy,
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    with override_current_actor(OTHER_ID, None):
        result = gateway.invoke(
            "otter_archive_index_status",
            {"resource_id": RESOURCE_ID},
        ).payload

    assert result["success"] is False
    assert result["error_code"] == "otter_archive_resource_not_authorised"
    assert proxy_called is False


def test_payload_identity_is_not_authority(monkeypatch):
    _configure_owner(monkeypatch)
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "otter_archive_index_status",
        {"resource_id": RESOURCE_ID},
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "authenticated_actor_context_required"


def test_trusted_local_operator_is_distinguished_from_google_actor(monkeypatch):
    _configure_owner(monkeypatch)

    class FakeProxy:
        async def call(self, tool_name, arguments):
            assert tool_name == "index_status"
            assert arguments == {}
            return {"ready": True, "records": 7}

    async def fake_get_otter_archive_proxy(*, resource_id):
        assert resource_id == RESOURCE_ID
        return FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.otter_archive_proxy_mcp.get_otter_archive_proxy",
        fake_get_otter_archive_proxy,
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )

    result = gateway.invoke(
        "otter_archive_index_status",
        {"resource_id": RESOURCE_ID},
    ).payload

    assert result["success"] is True
    assert result["authority"]["principal_kind"] == "trusted_local_operator"
    assert result["authority"]["actor_user_concept_id"] is None
