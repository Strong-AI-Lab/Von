from __future__ import annotations

import hashlib


def test_finalise_cached_paper_uploads_cached_pdf_and_registers_file_copy(
    monkeypatch, tmp_path
):
    from src.backend.integrations.internal_mcp import catalogue

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    data = b"%PDF-1.4\n%fake\ncontent\n"
    pdf_path = cache_dir / "2506.16596v2.pdf"
    pdf_path.write_bytes(data)
    markdown_data = b"# Test\n\nSome content.\n"
    markdown_path = cache_dir / "2506.16596v2.md"
    markdown_path.write_bytes(markdown_data)

    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    blob_root = tmp_path / "blob_store"
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(blob_root))

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    recorded_calls = []

    class _FakeRecord:
        def __init__(self, suffix: str):
            self.concept_id = f"#V#computer_file_copy_test_{suffix}"
            self.uploaded_at = "2026-01-01T00:00:00+00:00"

    def _fake_create_instance(**kwargs):
        record = _FakeRecord(str(len(recorded_calls)))
        recorded_calls.append((record, kwargs))
        return record

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        _fake_create_instance,
    )

    linked_calls = []

    def _fake_link_file_copy_to_arxiv_paper(**kwargs):
        linked_calls.append(kwargs)
        return {"paper_concept_id": "#V#paper_on_arxiv_2506_16596v2_deadbeef"}

    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        _fake_link_file_copy_to_arxiv_paper,
    )

    result = catalogue._finalise_cached_paper(arxiv_id="2506.16596v2")

    assert result["success"] is True
    assert result["file_path"] == str(pdf_path)
    assert result["size_bytes"] == len(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["storage"]["backend"] == "local"
    assert result["storage"]["key"].endswith("arxiv/papers/2506.16596v2.pdf")
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test_0"
    assert result["paper_concept_id"] == "#V#paper_on_arxiv_2506_16596v2_deadbeef"
    assert result["local_cache_deleted"] is True
    assert result.get("local_cache_delete_error") in (None, "")
    assert result["markdown"]["status"] == "uploaded"
    assert result["markdown"]["file_path"] == str(markdown_path)
    assert result["markdown"]["size_bytes"] == len(markdown_data)
    assert result["markdown"]["sha256"] == hashlib.sha256(markdown_data).hexdigest()
    assert result["markdown"]["storage"]["backend"] == "local"
    assert result["markdown"]["storage"]["key"].endswith("arxiv/papers/2506.16596v2.md")
    assert result["markdown"]["computer_file_copy_concept_id"] == "#V#computer_file_copy_test_1"
    assert result["markdown"]["local_cache_deleted"] is True
    assert result["markdown"].get("local_cache_delete_error") in (None, "")

    # Default behaviour should clean up the cached PDF after successful durable upload.
    assert pdf_path.exists() is False
    assert markdown_path.exists() is False

    assert len(recorded_calls) == 2
    pdf_record, pdf_kwargs = recorded_calls[0]
    md_record, md_kwargs = recorded_calls[1]

    assert pdf_kwargs["user_concept_id"] == "#V#user_test"
    assert pdf_kwargs["type_concept_id"] == "#V#arxiv_pdf_file"
    assert pdf_kwargs["sha256"] == result["sha256"]
    assert pdf_kwargs["size_bytes"] == len(data)
    assert pdf_kwargs["blob_key"].endswith("arxiv/papers/2506.16596v2.pdf")

    assert md_kwargs["user_concept_id"] == "#V#user_test"
    assert md_kwargs["type_concept_id"] == "#V#arxiv_markdown_file"
    assert md_kwargs["sha256"] == result["markdown"]["sha256"]
    assert md_kwargs["size_bytes"] == len(markdown_data)
    assert md_kwargs["blob_key"].endswith("arxiv/papers/2506.16596v2.md")

    assert len(linked_calls) >= 2
    linked_file_copy_ids = {
        call["file_copy_concept_id"]
        for call in linked_calls
        if call.get("user_concept_id") == "#V#user_test"
        and call.get("arxiv_id") == "2506.16596v2"
    }
    assert pdf_record.concept_id in linked_file_copy_ids
    assert md_record.concept_id in linked_file_copy_ids


def test_finalise_cached_paper_errors_when_cache_missing(monkeypatch, tmp_path):
    from src.backend.integrations.internal_mcp import catalogue

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    result = catalogue._finalise_cached_paper(arxiv_id="2506.16596")

    assert result["success"] is False
    assert result["error"] == "cached_pdf_not_found"


def test_finalise_cached_paper_materialises_scholarly_representation(
    monkeypatch, tmp_path
):
    from src.backend.integrations.internal_mcp import catalogue

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / "2502.14996.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(tmp_path / "blob_store"))

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
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
        }

    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_arxiv_file_copy",
        _fake_materialise_arxiv_file_copy,
    )

    result = catalogue._finalise_cached_paper(
        arxiv_id="2502.14996",
        include_markdown=False,
    )

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


def test_finalise_cached_paper_can_skip_eager_scholarly_materialisation(
    monkeypatch, tmp_path
):
    from src.backend.integrations.internal_mcp import catalogue

    cache_dir = tmp_path / "arxiv_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / "2603.21702.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(tmp_path / "blob_store"))

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
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

    result = catalogue._finalise_cached_paper(
        arxiv_id="2603.21702",
        include_markdown=False,
        materialise_scholarly_representation=False,
    )

    assert result["success"] is True
    scholarly = result.get("scholarly_representation") or {}
    assert scholarly.get("attempted") is False
    assert scholarly.get("status") == "skipped_by_request"
    assert scholarly.get("reason") == "materialise_scholarly_representation_disabled"
