from __future__ import annotations


def test_read_file_copy_requires_user_context(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    result = catalogue._read_file_copy(concept_id="#V#file_copy_test")
    assert result["success"] is False
    assert result["error_code"] == "authentication_required"


def test_read_file_copy_returns_text(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo

    captured_kwargs: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#file_copy_test",
        blob_key="uploads/user/abc/notes.txt",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/notes.txt",
        content_type="text/plain",
        original_filename="notes.txt",
        size_bytes=11,
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: captured_kwargs.update(kwargs)
        or {"success": True, "info": info, "data": b"hello world"},
    )

    result = catalogue._read_file_copy(concept_id="#V#file_copy_test", max_bytes=20)
    assert result["success"] is True
    assert result["text"] == "hello world"
    assert result["original_filename"] == "notes.txt"
    assert captured_kwargs["user_concept_id"] == "#V#user_test"
    assert captured_kwargs["namespace"] is None
