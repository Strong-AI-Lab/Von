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

    monkeypatch.setenv("ARXIV_CACHE_PATH", str(cache_dir))

    blob_root = tmp_path / "blob_store"
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_BLOB_STORE_LOCAL_ROOT", str(blob_root))

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
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

    result = catalogue._finalise_cached_paper(arxiv_id="2506.16596v2")

    assert result["success"] is True
    assert result["file_path"] == str(pdf_path)
    assert result["size_bytes"] == len(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["storage"]["backend"] == "local"
    assert result["storage"]["key"].endswith("arxiv/papers/2506.16596v2.pdf")
    assert result["computer_file_copy_concept_id"] == "#V#computer_file_copy_test"
    assert result["local_cache_deleted"] is True
    assert result.get("local_cache_delete_error") in (None, "")

    # Default behaviour should clean up the cached PDF after successful durable upload.
    assert pdf_path.exists() is False

    assert recorded["user_concept_id"] == "#V#user_test"
    assert recorded["sha256"] == result["sha256"]
    assert recorded["size_bytes"] == len(data)
    assert recorded["blob_key"].endswith("arxiv/papers/2506.16596v2.pdf")


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
