from __future__ import annotations

import os
import sys

import pytest


def _project_pdf(data: bytes) -> dict:
    from src.backend.services.file_bytes_text_projection_service import (
        extract_file_bytes_text_projection,
    )

    return extract_file_bytes_text_projection(
        data=data,
        content_type="application/pdf",
        original_filename="source.pdf",
    )


def test_unverified_platform_fails_closed_without_starting_parser(monkeypatch):
    from src.backend.services import file_bytes_text_projection_service as service

    monkeypatch.setattr(service, "_pdf_parser_isolation_supported", lambda: False)
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported hosts must not start the PDF parser")
        ),
    )

    result = _project_pdf(b"%PDF-1.7")

    assert result == {
        "text_length": 0,
        "text_truncated": False,
        "text_extraction": "pdf_text_projection_unsupported",
        "text_extraction_error": "pdf_parser_isolation_unavailable",
    }


def test_worker_memory_limit_is_a_truncated_resource_result(monkeypatch):
    from src.backend.services import file_bytes_text_projection_service as service

    monkeypatch.setattr(
        service,
        "_run_isolated_pdf_worker",
        lambda *_args, **_kwargs: {
            "status": "resource_limit",
            "error": "pdf_parser_memory_limit_exceeded",
        },
    )

    result = _project_pdf(b"%PDF-1.7")

    assert result == {
        "text_length": 0,
        "text_truncated": True,
        "text_extraction": "pdf_resource_limit_exceeded",
        "text_extraction_error": "pdf_parser_memory_limit_exceeded",
    }


@pytest.mark.skipif(os.name != "posix", reason="process-group kill is POSIX-only")
def test_wall_limit_kills_worker_and_returns_named_resource_result(monkeypatch):
    from src.backend.services import file_bytes_text_projection_service as service

    processes = []
    real_popen = service.subprocess.Popen

    def _capture_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service, "_pdf_parser_isolation_supported", lambda: True)
    monkeypatch.setattr(
        service,
        "_pdf_worker_command",
        lambda **_kwargs: [sys.executable, "-c", "import time; time.sleep(5)"],
    )
    monkeypatch.setattr(service, "PDF_PARSER_WALL_LIMIT_SECONDS", 0.05)
    monkeypatch.setattr(service.subprocess, "Popen", _capture_process)

    result = service._run_isolated_pdf_worker(
        b"%PDF-1.7",
        stop_after_chars=20_001,
    )

    assert result == {
        "status": "resource_limit",
        "error": "pdf_parser_wall_time_limit_exceeded",
    }
    assert len(processes) == 1
    assert processes[0].poll() is not None
