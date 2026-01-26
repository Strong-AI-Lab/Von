"""Tests for Office document text extraction in read_file_copy."""

from __future__ import annotations

import io
import pytest


def test_docx_extraction(monkeypatch):
    """Test that DOCX files are extracted using python-docx."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo

    # Create a minimal DOCX file in memory
    from docx import Document

    doc = Document()
    doc.add_paragraph("Hello from Word!")
    doc.add_paragraph("Second paragraph.")

    docx_buffer = io.BytesIO()
    doc.save(docx_buffer)
    docx_bytes = docx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#docx_test",
        blob_key="uploads/user/abc/document.docx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/document.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        original_filename="document.docx",
        size_bytes=len(docx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": docx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#docx_test")
    assert result["success"] is True
    assert "Hello from Word!" in result["text"]
    assert "Second paragraph." in result["text"]
    assert result["text_extraction"] == "python_docx"
    assert result["encoding"] == "utf-8"


def test_docx_extraction_by_extension(monkeypatch):
    """Test that DOCX files are detected by extension even without correct MIME type."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from docx import Document

    doc = Document()
    doc.add_paragraph("Content detected by extension")

    docx_buffer = io.BytesIO()
    doc.save(docx_buffer)
    docx_bytes = docx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#docx_test",
        blob_key="uploads/user/abc/document.docx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/document.docx",
        content_type="application/octet-stream",  # Generic type
        original_filename="document.docx",
        size_bytes=len(docx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": docx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#docx_test")
    assert result["success"] is True
    assert "Content detected by extension" in result["text"]
    assert result["text_extraction"] == "python_docx"


def test_docx_with_table_extraction(monkeypatch):
    """Test that tables in DOCX files are extracted."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from docx import Document

    doc = Document()
    doc.add_paragraph("Document with table")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Header 1"
    table.cell(0, 1).text = "Header 2"
    table.cell(1, 0).text = "Cell A"
    table.cell(1, 1).text = "Cell B"

    docx_buffer = io.BytesIO()
    doc.save(docx_buffer)
    docx_bytes = docx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#docx_table_test",
        blob_key="uploads/user/abc/table.docx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/table.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        original_filename="table.docx",
        size_bytes=len(docx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": docx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#docx_table_test")
    assert result["success"] is True
    assert "Header 1" in result["text"]
    assert "Cell A" in result["text"]


def test_pptx_extraction(monkeypatch):
    """Test that PPTX files are extracted using python-pptx."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide_layout = prs.slide_layouts[5]  # Blank layout
    slide = prs.slides.add_slide(slide_layout)

    # Add a text box
    left = Inches(1)
    top = Inches(1)
    width = Inches(5)
    height = Inches(1)
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.text = "Hello from PowerPoint!"

    pptx_buffer = io.BytesIO()
    prs.save(pptx_buffer)
    pptx_bytes = pptx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#pptx_test",
        blob_key="uploads/user/abc/presentation.pptx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/presentation.pptx",
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        original_filename="presentation.pptx",
        size_bytes=len(pptx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": pptx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#pptx_test")
    assert result["success"] is True
    assert "Hello from PowerPoint!" in result["text"]
    assert result["text_extraction"] == "python_pptx"
    assert result["encoding"] == "utf-8"


def test_pptx_extraction_by_extension(monkeypatch):
    """Test that PPTX files are detected by extension."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    txBox = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    txBox.text_frame.text = "Extension detection test"

    pptx_buffer = io.BytesIO()
    prs.save(pptx_buffer)
    pptx_bytes = pptx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#pptx_ext_test",
        blob_key="uploads/user/abc/slides.pptx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/slides.pptx",
        content_type="application/octet-stream",
        original_filename="slides.pptx",
        size_bytes=len(pptx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": pptx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#pptx_ext_test")
    assert result["success"] is True
    assert "Extension detection test" in result["text"]
    assert result["text_extraction"] == "python_pptx"


def test_xlsx_extraction(monkeypatch):
    """Test that XLSX files are extracted using openpyxl."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Data"
    ws["A1"] = "Name"
    ws["B1"] = "Value"
    ws["A2"] = "Item 1"
    ws["B2"] = 42
    ws["A3"] = "Item 2"
    ws["B3"] = 100

    xlsx_buffer = io.BytesIO()
    wb.save(xlsx_buffer)
    xlsx_bytes = xlsx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#xlsx_test",
        blob_key="uploads/user/abc/spreadsheet.xlsx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/spreadsheet.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_filename="spreadsheet.xlsx",
        size_bytes=len(xlsx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": xlsx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#xlsx_test")
    assert result["success"] is True
    assert "Name" in result["text"]
    assert "Item 1" in result["text"]
    assert "42" in result["text"]
    assert result["text_extraction"] == "openpyxl"
    assert result["encoding"] == "utf-8"


def test_xlsx_extraction_by_extension(monkeypatch):
    """Test that XLSX files are detected by extension."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "Extension test"

    xlsx_buffer = io.BytesIO()
    wb.save(xlsx_buffer)
    xlsx_bytes = xlsx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#xlsx_ext_test",
        blob_key="uploads/user/abc/data.xlsx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/data.xlsx",
        content_type="application/octet-stream",
        original_filename="data.xlsx",
        size_bytes=len(xlsx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": xlsx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#xlsx_ext_test")
    assert result["success"] is True
    assert "Extension test" in result["text"]
    assert result["text_extraction"] == "openpyxl"


def test_xlsx_multiple_sheets(monkeypatch):
    """Test that multiple sheets in XLSX files are extracted."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    assert ws1 is not None
    ws1.title = "First Sheet"
    ws1["A1"] = "Data from first"

    ws2 = wb.create_sheet("Second Sheet")
    ws2["A1"] = "Data from second"

    xlsx_buffer = io.BytesIO()
    wb.save(xlsx_buffer)
    xlsx_bytes = xlsx_buffer.getvalue()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#xlsx_multi_test",
        blob_key="uploads/user/abc/multi.xlsx",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/multi.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_filename="multi.xlsx",
        size_bytes=len(xlsx_bytes),
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {"success": True, "info": info, "data": xlsx_bytes},
    )

    result = catalogue._read_file_copy(concept_id="#V#xlsx_multi_test")
    assert result["success"] is True
    assert "First Sheet" in result["text"]
    assert "Second Sheet" in result["text"]
    assert "Data from first" in result["text"]
    assert "Data from second" in result["text"]


def test_plain_text_still_works(monkeypatch):
    """Test that plain text files still work after Office extraction code is added."""
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services.computer_file_copy_service import FileCopyBlobInfo

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    info = FileCopyBlobInfo(
        concept_id="#V#text_test",
        blob_key="uploads/user/abc/readme.txt",
        blob_backend="local",
        blob_uri="local://uploads/user/abc/readme.txt",
        content_type="text/plain",
        original_filename="readme.txt",
        size_bytes=23,
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kwargs: {
            "success": True,
            "info": info,
            "data": b"Plain text still works",
        },
    )

    result = catalogue._read_file_copy(concept_id="#V#text_test")
    assert result["success"] is True
    assert result["text"] == "Plain text still works"
    # Should NOT have text_extraction set for plain text
    assert "text_extraction" not in result or result.get("text_extraction") is None
