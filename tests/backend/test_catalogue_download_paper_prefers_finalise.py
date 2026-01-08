import pytest


def test_download_paper_prefers_finalise_when_cached_and_authenticated(
    monkeypatch, tmp_path
):
    # Arrange: cached PDF exists
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "2506.16596v2.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    # And we're authenticated
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    # Stub finalise_cached_paper so we can assert it was chosen.
    from src.backend.integrations.internal_mcp import catalogue

    called = {"count": 0, "args": None}

    def _fake_finalise_cached_paper(**kwargs):
        called["count"] += 1
        called["args"] = dict(kwargs)
        return {
            "success": True,
            "arxiv_id": "2506.16596v2",
            "file_path": str(cache_dir / "2506.16596v2.pdf"),
            "size_bytes": 16,
            "sha256": "deadbeef",
            "storage": {
                "backend": "local",
                "key": "arxiv/papers/2506.16596v2.pdf",
                "uri": "local://x",
            },
            "computer_file_copy_concept_id": "#V#computer_file_copy_test",
            "uploaded_at": "2026-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(
        catalogue, "_finalise_cached_paper", _fake_finalise_cached_paper
    )

    # If we accidentally fall through to the proxy path, fail loudly.
    async def _boom():  # pragma: no cover
        raise AssertionError("get_arxiv_proxy should not be called when cached")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _boom,
    )

    # Act
    result = catalogue._download_paper(arxiv_id="2506.16596v2")

    # Assert
    assert result["success"] is True
    assert called["count"] == 1
    assert called["args"]["arxiv_id"] == "2506.16596v2"


def test_download_paper_falls_back_when_not_authenticated(monkeypatch, tmp_path):
    # Arrange: cached PDF exists
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "2506.16596.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    # But no auth context, so we should not finalise/register.
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    from src.backend.integrations.internal_mcp import catalogue

    # Provide a minimal proxy implementation.
    class _Proxy:
        async def download_paper(self, *, arxiv_id: str, filename=None):
            return {"success": True, "source": "proxy", "arxiv_id": arxiv_id}

    async def _fake_get_proxy():
        return _Proxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _fake_get_proxy,
    )

    # Act
    result = catalogue._download_paper(arxiv_id="2506.16596")

    # Assert
    assert result["success"] is True
    assert result.get("source") == "proxy"


def test_download_paper_registers_file_copy_when_authenticated(monkeypatch, tmp_path):
    # Arrange: no cached PDF initially (force proxy path)
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    # Authenticated
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    from src.backend.integrations.internal_mcp import catalogue

    # Proxy returns a downloaded/stored record (already durable-stored) including file_path.
    pdf_path = cache_dir / "2505.12477.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

    class _Proxy:
        async def download_paper(self, *, arxiv_id: str, filename=None):
            return {
                "success": True,
                "arxiv_id": arxiv_id,
                "file_path": str(pdf_path),
                "size_bytes": pdf_path.stat().st_size,
                "sha256": "deadbeef",
                "storage": {
                    "backend": "local",
                    "key": f"arxiv/papers/{arxiv_id}.pdf",
                    "uri": f"local://arxiv/papers/{arxiv_id}.pdf",
                },
            }

    async def _fake_get_proxy():
        return _Proxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _fake_get_proxy,
    )

    recorded = {}

    class _FakeRecord:
        concept_id = "#V#computer_file_copy_test"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    def _fake_create_instance(**kwargs):
        recorded.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        _fake_create_instance,
    )

    # Act
    result = catalogue._download_paper(arxiv_id="2505.12477")

    # Assert: ontology registration fields are present
    assert result["success"] is True
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test"
    assert result["uploaded_at"] == "2026-01-01T00:00:00+00:00"
    assert recorded["user_concept_id"] == "#V#user_test"
    assert recorded["blob_key"].endswith("arxiv/papers/2505.12477.pdf")

    # And local cache is deleted by default when authenticated.
    assert result.get("local_cache_deleted") is True
    assert pdf_path.exists() is False
