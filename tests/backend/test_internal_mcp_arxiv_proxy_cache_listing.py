import asyncio
import hashlib


def test_arxiv_list_papers_lists_cached_pdfs_without_calling_upstream(tmp_path):
    from src.backend.integrations.internal_mcp.arxiv_proxy_mcp import (
        ArxivMCPProxy,
        ArxivProxyConfig,
    )

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Filename pattern mimics arXiv IDs commonly used in tooling.
    (cache_dir / "2506.16596v2.pdf").write_bytes(b"%PDF-1.4\n%fake\n")

    proxy = ArxivMCPProxy(ArxivProxyConfig(storage_path=cache_dir))

    async def _should_not_be_called(*args, **kwargs):
        raise AssertionError("Unexpected upstream tool call")

    proxy._call_tool = _should_not_be_called  # type: ignore[assignment]

    result = asyncio.run(proxy.list_papers())

    assert result["success"] is True
    assert result["total_papers"] == 1

    paper = result["papers"][0]
    assert paper["filename"] == "2506.16596v2.pdf"
    assert paper["arxiv_id"] == "2506.16596v2"
    assert paper["version"] == 2
    assert paper["size_bytes"] > 0


def test_arxiv_store_downloaded_pdf_includes_sha256_and_size(monkeypatch, tmp_path):
    from src.backend.integrations.internal_mcp.arxiv_proxy_mcp import (
        ArxivMCPProxy,
        ArxivProxyConfig,
    )

    blob_root = tmp_path / "blob_store"
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(blob_root))

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    data = b"%PDF-1.4\n%fake\ncontent\n"
    pdf_path = cache_dir / "2506.16596v2.pdf"
    pdf_path.write_bytes(data)

    expected_sha256 = hashlib.sha256(data).hexdigest()

    proxy = ArxivMCPProxy(ArxivProxyConfig(storage_path=cache_dir))

    stored = proxy._store_downloaded_pdf(
        result={"file_path": str(pdf_path)},
        arxiv_id="2506.16596v2",
    )

    assert stored["file_path"] == str(pdf_path)
    assert stored["size_bytes"] == len(data)
    assert stored["sha256"] == expected_sha256
    assert stored["version"] == 2

    storage = stored["storage"]
    assert storage["backend"] == "local"
    assert storage["key"].endswith("arxiv/papers/2506.16596v2.pdf")
    assert storage["uri"]
