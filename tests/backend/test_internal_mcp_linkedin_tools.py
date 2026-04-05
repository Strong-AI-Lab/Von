"""Smoke tests for LinkedIn MCP integration in the internal MCP catalogue."""

from src.backend.integrations.internal_mcp.catalogue import (
    _linkedin_get_company_stats,
    _linkedin_get_csv_data,
    _linkedin_get_messages,
    _linkedin_get_profile,
    _linkedin_list_files,
    build_default_catalogue,
)


def test_linkedin_methods_registered_in_catalogue():
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())

    assert "linkedin_list_exports" in names
    assert "linkedin_list_files" in names
    assert "linkedin_get_profile" in names
    assert "linkedin_get_csv_data" in names
    assert "linkedin_get_company_stats" in names
    assert "linkedin_get_messages" in names


def test_linkedin_handlers_require_minimum_fields():
    err_files = _linkedin_list_files(export_name=None)
    assert err_files.get("success") is False
    assert "export_name" in err_files.get("error", "")

    err_profile = _linkedin_get_profile(export_name=None)
    assert err_profile.get("success") is False
    assert "export_name" in err_profile.get("error", "")

    err_csv = _linkedin_get_csv_data(export_name=None, file_name=None)
    assert err_csv.get("success") is False
    assert "export_name" in err_csv.get("error", "")
    assert "file_name" in err_csv.get("error", "")

    err_company = _linkedin_get_company_stats(export_name=None)
    assert err_company.get("success") is False
    assert "export_name" in err_company.get("error", "")

    err_messages = _linkedin_get_messages(export_name=None)
    assert err_messages.get("success") is False
    assert "export_name" in err_messages.get("error", "")


def test_linkedin_get_csv_data_rejects_invalid_limit():
    result = _linkedin_get_csv_data(
        export_name="sample-export",
        file_name="Profile.csv",
        limit="ten",
    )
    assert result.get("success") is False
    assert result.get("error_code") == "invalid_parameter"


def test_linkedin_tools_success_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def list_exports(self, *, refresh: bool = False):
            assert refresh is False
            return {
                "success": True,
                "exports": ["export_a.zip"],
                "total_exports": 1,
                "data_root": "data/linkedin",
                "data_root_exists": True,
            }

        async def list_files(self, *, export_name: str):
            assert export_name == "export_a.zip"
            return {
                "success": True,
                "export_name": export_name,
                "files": ["Profile.csv", "Connections.csv"],
            }

    async def _fake_get_linkedin_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.linkedin_proxy_mcp.get_linkedin_proxy",
        _fake_get_linkedin_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    exports_result = gateway.invoke("linkedin_list_exports", {})
    exports_payload = exports_result.payload
    assert exports_payload.get("success") is True
    assert exports_payload.get("total_exports") == 1

    files_result = gateway.invoke(
        "linkedin_list_files",
        {"export_name": "export_a.zip"},
    )
    files_payload = files_result.payload
    assert files_payload.get("success") is True
    assert files_payload.get("export_name") == "export_a.zip"
    assert files_payload.get("files") == ["Profile.csv", "Connections.csv"]


def test_linkedin_list_files_proxy_error_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.integrations.internal_mcp.linkedin_proxy_mcp import (
        LinkedInProxyError,
    )

    class _FailingProxy:
        async def list_files(self, *, export_name: str):  # noqa: ARG002
            raise LinkedInProxyError("proxy unavailable")

    async def _fake_get_linkedin_proxy():
        return _FailingProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.linkedin_proxy_mcp.get_linkedin_proxy",
        _fake_get_linkedin_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke("linkedin_list_files", {"export_name": "export_a.zip"})
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "linkedin_proxy_error"
