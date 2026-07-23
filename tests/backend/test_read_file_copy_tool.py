from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook


def test_read_file_copy_requires_user_context(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    result = catalogue._read_file_copy(concept_id="#V#file_copy_test")
    assert result["success"] is False
    assert result["error_code"] == "authentication_required"


def test_read_file_copy_rejects_caller_supplied_cross_namespace(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#trusted_user",
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_organisation_concept_id",
        lambda: "#V#trusted_org",
    )
    fetch_called = False

    def _unexpected_fetch(**_kwargs):
        nonlocal fetch_called
        fetch_called = True
        raise AssertionError("cross-namespace read must fail before blob access")

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        _unexpected_fetch,
    )

    result = catalogue._read_file_copy(
        concept_id="#V#file_copy_test",
        namespace="#V#other_user@trusted_org",
    )

    assert result["success"] is False
    assert result["error_code"] == "namespace_mismatch"
    assert fetch_called is False


def test_read_file_copy_accepts_matching_authenticated_namespace(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#trusted_user",
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_organisation_concept_id",
        lambda: "#V#trusted_org",
    )
    captured_kwargs: dict[str, object] = {}
    info = FileCopyBlobInfo(
        concept_id="#V#file_copy_test",
        blob_key="uploads/user/abc/notes.txt",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/notes.txt",
        content_type="text/plain",
        original_filename="notes.txt",
        size_bytes=2,
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: captured_kwargs.update(kwargs)
        or {"success": True, "info": info, "data": b"ok"},
    )

    result = catalogue._read_file_copy(
        concept_id="#V#file_copy_test",
        namespace="#V#trusted_user@trusted_org",
    )

    assert result["success"] is True
    assert captured_kwargs["user_concept_id"] == "#V#trusted_user"
    assert captured_kwargs["organisation_concept_id"] == "#V#trusted_org"
    assert captured_kwargs["namespace"] == "#V#trusted_user@trusted_org"


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


def test_read_file_copy_returns_structured_spreadsheet_evidence(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo

    book = Workbook()
    sheet = book.active
    sheet.title = "Records"
    sheet.append(["Key", "Value"])
    sheet.append(["one", 1])
    stream = BytesIO()
    book.save(stream)
    book.close()
    workbook_bytes = stream.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )
    info = FileCopyBlobInfo(
        concept_id="#V#file_copy_sheet",
        blob_key="uploads/user/abc/records.xlsx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/records.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_filename="records.xlsx",
        size_bytes=len(workbook_bytes),
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "info": info, "data": workbook_bytes},
    )

    result = catalogue._read_file_copy(
        concept_id="#V#file_copy_sheet",
        as_text=False,
        structured_spreadsheet=True,
    )

    assert result["success"] is True
    assert result["spreadsheet_extraction"] == "openpyxl_structured_v1"
    assert result["spreadsheet"]["sheet_names"] == ["Records"]
    assert result["spreadsheet"]["sheets"][0]["headers"] == ["Key", "Value"]
    assert len(result["sha256"]) == 64
    assert "bytes_base64" not in result
    assert "text" not in result


def test_read_file_copy_preserves_typed_spreadsheet_safety_failure(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from src.backend.services.spreadsheet_record_ingestion_service import (
        SpreadsheetPlanError,
    )

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )
    info = FileCopyBlobInfo(
        concept_id="#V#file_copy_sheet",
        blob_key="uploads/user/abc/records.xlsx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/records.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_filename="records.xlsx",
        size_bytes=1024,
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **_kwargs: {
            "success": True,
            "info": info,
            "data": b"bounded-test-placeholder",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.spreadsheet_record_ingestion_service.extract_spreadsheet_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SpreadsheetPlanError(
                "spreadsheet_archive_uncompressed_too_large",
                details={"member_count": 9},
            )
        ),
    )

    result = catalogue._read_file_copy(
        concept_id="#V#file_copy_sheet",
        as_text=False,
        structured_spreadsheet=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "spreadsheet_archive_uncompressed_too_large"
    assert result["error_details"] == {"member_count": 9}
