import asyncio
import hashlib


def test_arxiv_list_papers_lists_cached_pdfs_without_calling_upstream(
    monkeypatch, tmp_path
):
    from src.backend.integrations.internal_mcp.arxiv_proxy_mcp import (
        ArxivMCPProxy,
        ArxivProxyConfig,
    )

    # Ensure the test is isolated from any developer/CI environment configuration.
    monkeypatch.setenv("VON_ARXIV_INCLUDE_DURABLE_LISTING", "0")
    monkeypatch.delenv("VON_BLOB_STORE_BACKEND", raising=False)
    monkeypatch.delenv("VON_BLOB_STORE_LOCAL_ROOT", raising=False)

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
    assert result["total_papers"] == 1, result

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


def test_arxiv_store_downloaded_pdf_falls_back_to_cached_pdf_when_missing_path(
    monkeypatch, tmp_path
):
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
    pdf_path = cache_dir / "2512.23959.pdf"
    pdf_path.write_bytes(data)

    proxy = ArxivMCPProxy(ArxivProxyConfig(storage_path=cache_dir))

    # Simulate arxiv-mcp-server reporting success without returning a file path.
    stored = proxy._store_downloaded_pdf(
        result={"success": True}, arxiv_id="2512.23959"
    )

    assert stored["file_path"] == str(pdf_path)
    assert stored["size_bytes"] == len(data)
    assert stored["sha256"] == hashlib.sha256(data).hexdigest()
    assert stored["storage"]["backend"] == "local"
    assert stored["storage"]["key"].endswith("arxiv/papers/2512.23959.pdf")
    assert stored["storage"]["uri"]


def test_arxiv_store_downloaded_pdf_waits_for_async_cache_settlement(
    monkeypatch, tmp_path
):
    from src.backend.integrations.internal_mcp import arxiv_proxy_mcp as mod

    blob_root = tmp_path / "blob_store"
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(blob_root))

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / "2603.21702.pdf"
    data = b"%PDF-1.4\n%async\n"
    attempts = {"count": 0}

    def _fake_find_cached_pdf_for_arxiv_id(storage_path, arxiv_id):
        attempts["count"] += 1
        if attempts["count"] >= 3 and not pdf_path.exists():
            pdf_path.write_bytes(data)
        return pdf_path if pdf_path.exists() else None

    monkeypatch.setattr(mod, "_find_cached_pdf_for_arxiv_id", _fake_find_cached_pdf_for_arxiv_id)
    monkeypatch.setattr(mod, "_find_recent_pdf_in_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    proxy = mod.ArxivMCPProxy(mod.ArxivProxyConfig(storage_path=cache_dir, timeout_sec=1.0))

    stored = proxy._store_downloaded_pdf(
        result={"status": "converting", "message": "Paper downloaded, conversion started"},
        arxiv_id="2603.21702",
        download_started_at=mod.time.time(),
    )

    assert stored["success"] is True
    assert stored["file_path"] == str(pdf_path)
    assert stored["size_bytes"] == len(data)
    assert stored["sha256"] == hashlib.sha256(data).hexdigest()
    assert attempts["count"] >= 3


def test_arxiv_store_downloaded_pdf_directly_reacquires_when_server_omits_path(
    monkeypatch, tmp_path
):
    from src.backend.integrations.internal_mcp import arxiv_proxy_mcp as mod

    blob_root = tmp_path / "blob_store"
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(blob_root))

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = b"%PDF-1.4\n%direct-fallback\n"
    requests: list[tuple[str, float]] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return data

    def _fake_urlopen(request, timeout=0):
        requests.append((request.full_url, timeout))
        return _Response()

    monkeypatch.setattr(mod, "urlopen", _fake_urlopen, raising=False)

    proxy = mod.ArxivMCPProxy(mod.ArxivProxyConfig(storage_path=cache_dir))

    stored = proxy._store_downloaded_pdf(
        result={"success": True, "message": "Paper downloaded"},
        arxiv_id="2603.26499",
        download_started_at=mod.time.time(),
    )

    expected_path = cache_dir / "2603.26499.pdf"
    assert stored["success"] is True
    assert stored["file_path"] == str(expected_path)
    assert expected_path.read_bytes() == data
    assert stored["direct_pdf_fallback"] is True
    assert stored["direct_pdf_source_url"] == "https://arxiv.org/pdf/2603.26499.pdf"
    assert stored["size_bytes"] == len(data)
    assert stored["sha256"] == hashlib.sha256(data).hexdigest()
    assert stored["storage"]["key"].endswith("arxiv/papers/2603.26499.pdf")
    assert requests == [("https://arxiv.org/pdf/2603.26499.pdf", 30.0)]


def test_arxiv_list_papers_includes_durable_blob_store_objects(monkeypatch, tmp_path):
    import asyncio

    from src.backend.integrations.internal_mcp.arxiv_proxy_mcp import (
        ArxivMCPProxy,
        ArxivProxyConfig,
    )

    blob_root = tmp_path / "blob_store"
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(blob_root))
    monkeypatch.setenv("VON_ARXIV_INCLUDE_DURABLE_LISTING", "1")

    # Create a durable blob-store object (local backend stores this as a file).
    durable_key = blob_root / "arxiv" / "papers"
    durable_key.mkdir(parents=True, exist_ok=True)
    (durable_key / "2412.02730.pdf").write_bytes(b"%PDF-1.4\n%durable\n")

    # Also create a cache file for a different paper.
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "2506.16596v2.pdf").write_bytes(b"%PDF-1.4\n%cache\n")

    proxy = ArxivMCPProxy(ArxivProxyConfig(storage_path=cache_dir))

    async def _should_not_be_called(*args, **kwargs):
        raise AssertionError("Unexpected upstream tool call")

    proxy._call_tool = _should_not_be_called  # type: ignore[assignment]

    result = asyncio.run(proxy.list_papers())
    assert result["success"] is True
    assert result["total_papers"] == 2

    papers = {p.get("arxiv_id"): p for p in result["papers"]}
    assert "2412.02730" in papers
    assert "2506.16596v2" in papers

    durable = papers["2412.02730"]
    assert durable.get("storage")
    assert durable["storage"]["backend"] == "local"
    assert durable["storage"]["key"] == "arxiv/papers/2412.02730.pdf"
    assert "2412.02730.pdf" in durable["storage"]["uri"]

    cached = papers["2506.16596v2"]
    assert cached.get("file_path")
    assert cached.get("size_bytes")


def test_arxiv_list_papers_builds_s3_uris_for_durable_objects(monkeypatch, tmp_path):
    from src.backend.integrations.internal_mcp.arxiv_proxy_mcp import (
        ArxivMCPProxy,
        ArxivProxyConfig,
    )

    monkeypatch.setenv("VON_ARXIV_INCLUDE_DURABLE_LISTING", "1")
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "swift")
    monkeypatch.setenv("VON_SWIFT_CONTAINER", "von-artifacts")
    monkeypatch.setenv(
        "VON_S3_ENDPOINT_URL", "https://object-storage.nz-por-1.catalystcloud.io"
    )
    monkeypatch.setenv("VON_S3_BUCKET", "von-artifacts")
    monkeypatch.setenv("VON_S3_PREFIX", "von")

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    class S3BlobStore:
        def list(self, prefix=""):
            assert prefix == "arxiv/papers"
            return ["arxiv/papers/2412.02730.pdf"]

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_blob_store_from_env",
        lambda: S3BlobStore(),
    )

    proxy = ArxivMCPProxy(ArxivProxyConfig(storage_path=cache_dir))
    result = asyncio.run(proxy.list_papers())

    assert result["success"] is True
    assert result["total_papers"] == 1
    paper = result["papers"][0]
    assert paper["storage"]["backend"] == "s3"
    assert paper["storage"]["key"] == "arxiv/papers/2412.02730.pdf"
    assert paper["storage"]["uri"] == (
        "https://object-storage.nz-por-1.catalystcloud.io/"
        "von-artifacts/von/arxiv/papers/2412.02730.pdf"
    )
