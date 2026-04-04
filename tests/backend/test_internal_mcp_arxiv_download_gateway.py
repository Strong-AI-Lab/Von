"""Gateway-path regression tests for arXiv download cache diagnostics."""

from __future__ import annotations

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _assert_schema_conformance(
    gateway: InternalMCPGateway, method: str, payload: dict
) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def test_download_paper_gateway_reports_partial_cache_diagnostics(
    monkeypatch, tmp_path
) -> None:
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = cache_dir / "2603.14482.md"
    markdown_path.write_text("# Partial cache only\n", encoding="utf-8")

    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    from src.backend.integrations.internal_mcp.arxiv_proxy_mcp import ArxivProxyError

    class _Proxy:
        async def download_paper(self, *, arxiv_id: str, filename=None):
            raise ArxivProxyError(f"download failed for {arxiv_id}")

    async def _fake_get_proxy():
        return _Proxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _fake_get_proxy,
    )

    gateway = _build_gateway()
    payload = gateway.invoke("download_paper", {"arxiv_id": "2603.14482"}).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "arxiv_proxy_error"
    details = payload.get("error_details") or {}
    assert details.get("cache_state") == "markdown_only_partial_cache"
    assert details.get("recommended_recovery_action") == "reacquire_pdf_from_source"
    cache_diagnostics = details.get("cache_diagnostics") or {}
    assert cache_diagnostics.get("cached_markdown_path") == str(markdown_path)
    assert cache_diagnostics.get("partial_cache_without_pdf") is True
    _assert_schema_conformance(gateway, "download_paper", payload)
