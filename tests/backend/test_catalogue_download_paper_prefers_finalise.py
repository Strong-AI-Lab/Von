from typing import Any, cast

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

    linked = {}

    def _fake_link_file_copy_to_arxiv_paper(**kwargs):
        linked.update(kwargs)
        return {"paper_concept_id": "#V#paper_on_arxiv_2505_12477_deadbeef"}

    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        _fake_link_file_copy_to_arxiv_paper,
    )

    # Act
    result = catalogue._download_paper(arxiv_id="2505.12477")

    # Assert: ontology registration fields are present
    assert result["success"] is True
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test"
    assert result["uploaded_at"] == "2026-01-01T00:00:00+00:00"
    assert result["paper_concept_id"] == "#V#paper_on_arxiv_2505_12477_deadbeef"
    registration = cast(dict[str, Any], result["computer_file_copy_registration"])
    assert registration["attempted"] is True
    assert registration["succeeded"] is True
    assert registration["status"] == "registered"
    assert recorded["user_concept_id"] == "#V#user_test"
    assert recorded["type_concept_id"] == "#V#arxiv_pdf_file"
    assert recorded["blob_key"].endswith("arxiv/papers/2505.12477.pdf")

    assert linked["user_concept_id"] == "#V#user_test"
    assert linked["arxiv_id"] == "2505.12477"
    assert linked["file_copy_concept_id"] == "#V#computer_file_copy_test"

    # And local cache is deleted by default when authenticated.
    assert result.get("local_cache_deleted") is True
    assert pdf_path.exists() is False


def test_download_paper_materialises_scholarly_representation_when_metadata_available(
    monkeypatch, tmp_path
):
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    from src.backend.integrations.internal_mcp import catalogue

    pdf_path = cache_dir / "2502.14996.pdf"
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

    class _FakeRecord:
        concept_id = "#V#computer_file_copy_test"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: _FakeRecord(),
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        lambda **_kwargs: {"paper_concept_id": "#V#paper_on_arxiv_2502_14996_deadbeef"},
    )
    monkeypatch.setattr(
        catalogue,
        "_get_paper_metadata",
        lambda **_kwargs: {
            "success": True,
            "paper": {
                "id": "2502.14996",
                "title": "Paper Title",
                "summary": "Paper summary",
                "authors": ["A. Author"],
                "categories": ["cs.AI"],
            },
        },
    )

    materialise_calls: list[dict] = []

    def _fake_materialise_arxiv_file_copy(**kwargs):
        materialise_calls.append(dict(kwargs))
        return {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_on_arxiv_2502_14996_deadbeef",
            "file_copy_concept_id": kwargs.get("file_copy_concept_id"),
        }

    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_arxiv_file_copy",
        _fake_materialise_arxiv_file_copy,
    )

    result = catalogue._download_paper(arxiv_id="2502.14996")

    assert result["success"] is True
    scholarly = result.get("scholarly_representation") or {}
    assert scholarly.get("verified") is True
    assert scholarly.get("metadata_source") == "get_paper_metadata"
    assert scholarly.get("metadata_available") is True
    assert result.get("paper_concept_id") == "#V#paper_on_arxiv_2502_14996_deadbeef"

    assert len(materialise_calls) == 1
    assert materialise_calls[0]["user_concept_id"] == "#V#user_test"
    assert materialise_calls[0]["arxiv_id"] == "2502.14996"
    assert (
        materialise_calls[0]["file_copy_concept_id"]
        == "#V#computer_file_copy_test"
    )


def test_download_paper_can_skip_eager_scholarly_materialisation(
    monkeypatch, tmp_path
):
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    from src.backend.integrations.internal_mcp import catalogue

    pdf_path = cache_dir / "2603.21702.pdf"
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

    class _FakeRecord:
        concept_id = "#V#computer_file_copy_test"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: _FakeRecord(),
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        lambda **_kwargs: {"paper_concept_id": "#V#paper_on_arxiv_2603_21702_deadbeef"},
    )

    def _boom(**_kwargs):  # pragma: no cover - defensive
        raise AssertionError(
            "eager scholarly materialisation should be skipped when disabled"
        )

    monkeypatch.setattr(catalogue, "_materialise_arxiv_file_copy_representation", _boom)

    result = catalogue._download_paper(
        arxiv_id="2603.21702",
        materialise_scholarly_representation=False,
    )

    assert result["success"] is True
    scholarly = result.get("scholarly_representation") or {}
    assert scholarly.get("attempted") is False
    assert scholarly.get("status") == "skipped_by_request"
    assert scholarly.get("reason") == "materialise_scholarly_representation_disabled"


def test_download_paper_uses_namespace_override_for_authenticated_registration(
    monkeypatch, tmp_path
):
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    from src.backend.integrations.internal_mcp import catalogue

    pdf_path = cache_dir / "2603.21702.pdf"
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

    class _FakeRecord:
        concept_id = "#V#computer_file_copy_test"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    created = {}

    def _fake_create_instance(**kwargs):
        created.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        _fake_create_instance,
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        lambda **_kwargs: {"paper_concept_id": "#V#paper_on_arxiv_2603_21702_deadbeef"},
    )

    result = catalogue._download_paper(
        arxiv_id="2603.21702",
        namespace="#V#workflow_user@default",
    )

    assert result["success"] is True
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test"
    assert created["user_concept_id"] == "#V#workflow_user"


def test_download_paper_registers_when_proxy_payload_omits_success_flag(
    monkeypatch, tmp_path
):
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    from src.backend.integrations.internal_mcp import catalogue

    pdf_path = cache_dir / "2603.21702.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

    class _Proxy:
        async def download_paper(self, *, arxiv_id: str, filename=None):
            return {
                "arxiv_id": arxiv_id,
                "file_path": str(pdf_path),
                "size_bytes": pdf_path.stat().st_size,
                "sha256": "deadbeef",
                "status": "converting",
                "storage": {
                    "backend": "swift",
                    "key": f"arxiv/papers/{arxiv_id}.pdf",
                    "uri": f"swift://von-artifacts/von/arxiv/papers/{arxiv_id}.pdf",
                },
            }

    async def _fake_get_proxy():
        return _Proxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _fake_get_proxy,
    )

    class _FakeRecord:
        concept_id = "#V#computer_file_copy_test"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: _FakeRecord(),
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        lambda **_kwargs: {"paper_concept_id": "#V#paper_on_arxiv_2603_21702_deadbeef"},
    )

    result = catalogue._download_paper(
        arxiv_id="2603.21702",
        namespace="#V#workflow_user@default",
    )

    assert result["success"] is True
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test"
    registration = cast(dict[str, Any], result["computer_file_copy_registration"])
    assert registration["attempted"] is True
    assert registration["succeeded"] is True
    assert registration["status"] == "registered"


def test_finalise_cached_paper_uses_namespace_override_for_authenticated_registration(
    monkeypatch, tmp_path
):
    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_pdf = cache_dir / "2603.21702.pdf"
    cached_pdf.write_bytes(b"%PDF-1.4\n%fake\n")
    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    from src.backend.integrations.internal_mcp import catalogue

    put_calls: list[dict[str, Any]] = []

    class _Ref:
        backend = "local"
        key = "arxiv/papers/2603.21702.pdf"
        uri = "local://arxiv/papers/2603.21702.pdf"

    class _Stored:
        ref = _Ref()

    def _fake_put_bytes_durable(**kwargs):
        put_calls.append(dict(kwargs))
        return _Stored()

    monkeypatch.setattr(
        "src.backend.services.blob_uploads.put_bytes_durable",
        _fake_put_bytes_durable,
    )

    class _FakeRecord:
        concept_id = "#V#computer_file_copy_test"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    created = {}

    def _fake_create_instance(**kwargs):
        created.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        _fake_create_instance,
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        lambda **_kwargs: {"paper_concept_id": "#V#paper_on_arxiv_2603_21702_deadbeef"},
    )
    monkeypatch.setattr(
        catalogue,
        "_materialise_arxiv_file_copy_representation",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_on_arxiv_2603_21702_deadbeef",
            "file_copy_concept_id": "#V#computer_file_copy_test",
        },
    )

    result = catalogue._finalise_cached_paper(
        arxiv_id="2603.21702",
        namespace="#V#workflow_user@default",
        delete_local_cache=False,
        include_markdown=False,
    )

    assert result["success"] is True
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test"
    assert created["user_concept_id"] == "#V#workflow_user"
    assert len(put_calls) == 1


def test_get_paper_metadata_falls_back_to_builtin_service_when_proxy_lacks_method(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    class _Proxy:
        pass

    async def _fake_get_proxy():
        return _Proxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _fake_get_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_metadata_service.fetch_arxiv_metadata",
        lambda **_kwargs: {
            "schema_version": "arxiv_metadata_record.v1",
            "id": "2603.21702",
            "title": "The Geometry of Next-Token Prediction",
            "authors": ["Amit Kanujia", "Bonan Min", "Nikhil Vyas"],
            "summary": "Abstract text.",
            "abstract": "Abstract text.",
            "publication_date": "2026-03-23",
            "metadata_source": "arxiv_atom_api",
        },
    )

    result = cast(dict[str, Any], catalogue._get_paper_metadata(arxiv_id="2603.21702"))

    assert result["success"] is True
    assert result["id"] == "2603.21702"
    assert result["publication_date"] == "2026-03-23"
    assert result["metadata_source"] == "arxiv_atom_api"
    assert result["metadata_fallback_reason"] == "arxiv_proxy_metadata_unsupported"


def test_get_paper_metadata_falls_back_when_proxy_returns_unknown_tool_payload(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    class _Proxy:
        async def get_paper_metadata(self, *, arxiv_id: str):
            return {"text": f"Error: Unknown tool get_paper_metadata for {arxiv_id}"}

    async def _fake_get_proxy():
        return _Proxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _fake_get_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_metadata_service.fetch_arxiv_metadata",
        lambda **_kwargs: {
            "schema_version": "arxiv_metadata_record.v1",
            "id": "2603.21702",
            "title": "Neutral representations of finite diagonalizable group schemes and fields of moduli",
            "authors": ["Giulio Bresciani", "Angelo Vistoli", "Tianzhi Yang"],
            "summary": "Abstract text.",
            "abstract": "Abstract text.",
            "publication_date": "2026-03-23",
            "metadata_source": "arxiv_atom_api",
        },
    )

    result = cast(dict[str, Any], catalogue._get_paper_metadata(arxiv_id="2603.21702"))

    assert result["success"] is True
    assert result["id"] == "2603.21702"
    assert result["metadata_source"] == "arxiv_atom_api"
    assert (
        result["metadata_fallback_reason"] == "metadata_payload_unusable"
    )
