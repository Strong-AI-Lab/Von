from __future__ import annotations

import threading
import time


class _StubResponse:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str] | None = None,
        body_chunks: list[bytes] | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._body_chunks = list(body_chunks or [])
        self.url = url or "https://example.com/file.bin"
        self.closed = False

    def iter_content(self, chunk_size: int = 0):
        del chunk_size
        yield from self._body_chunks

    def close(self) -> None:
        self.closed = True


class _StubSession:
    def __init__(self, responses: list[_StubResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, "kwargs": dict(kwargs)})
        if not self._responses:
            raise AssertionError("No stubbed responses remaining")
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_download_remote_file_copy_bytes_follows_redirect_and_inferrs_mime(
    monkeypatch,
):
    from src.backend.services import remote_file_copy_ingestion_service as svc

    monkeypatch.setattr(
        svc,
        "_validate_remote_target",
        lambda url: {
            "success": True,
            "url": url,
            "host": "example.com",
            "resolved_addresses": ["198.51.100.20"],
        },
    )

    redirect_response = _StubResponse(
        status_code=302,
        headers={"Location": "https://cdn.example.com/files/deck"},
        url="https://example.com/download",
    )
    final_response = _StubResponse(
        status_code=200,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="deck.pptx"',
            "Content-Length": "11",
            "ETag": "abc123",
        },
        body_chunks=[b"hello ", b"world"],
        url="https://cdn.example.com/files/deck",
    )
    session = _StubSession([redirect_response, final_response])
    monkeypatch.setattr(svc, "_create_requests_session", lambda: session)

    result = svc.download_remote_file_copy_bytes(url="https://example.com/download")

    assert result["success"] is True
    assert result["redirect_count"] == 1
    assert result["redirects"] == [
        {
            "status_code": 302,
            "from_url": "https://example.com/download",
            "to_url": "https://cdn.example.com/files/deck",
        }
    ]
    assert result["final_url"] == "https://cdn.example.com/files/deck"
    assert result["original_filename"] == "deck.pptx"
    assert result["filename_source"] == "response.content_disposition"
    assert (
        result["content_type"]
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert result["content_type_source"] == "filename.extension"
    assert result["response_headers"]["etag"] == "abc123"
    assert redirect_response.closed is True
    assert final_response.closed is True
    assert session.closed is True


def test_download_remote_file_copy_bytes_blocks_private_host(monkeypatch):
    from src.backend.services import remote_file_copy_ingestion_service as svc

    session = _StubSession([])
    monkeypatch.setattr(svc, "_create_requests_session", lambda: session)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (0, 0, 0, "", ("10.0.0.5", 443)),
        ],
    )

    result = svc.download_remote_file_copy_bytes(
        url="https://files.example.com/data.pdf"
    )

    assert result["success"] is False
    assert result["error"] == "blocked_private_host"
    assert result["host"] == "files.example.com"
    assert session.calls == []
    assert session.closed is True


def test_download_remote_file_copy_bytes_rejects_oversized_response(monkeypatch):
    from src.backend.services import remote_file_copy_ingestion_service as svc

    monkeypatch.setattr(
        svc,
        "_validate_remote_target",
        lambda url: {
            "success": True,
            "url": url,
            "host": "example.com",
            "resolved_addresses": ["198.51.100.20"],
        },
    )
    large_response = _StubResponse(
        status_code=200,
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": "99",
        },
        body_chunks=[b"unused"],
        url="https://example.com/big.pdf",
    )
    session = _StubSession([large_response])
    monkeypatch.setattr(svc, "_create_requests_session", lambda: session)

    result = svc.download_remote_file_copy_bytes(
        url="https://example.com/big.pdf",
        max_bytes=10,
    )

    assert result["success"] is False
    assert result["error"] == "remote_file_too_large"
    assert result["content_length"] == 99
    assert result["max_bytes"] == 10
    assert large_response.closed is True
    assert session.closed is True


def test_download_remote_file_copy_bytes_enforces_total_timeout(monkeypatch):
    from src.backend.services import remote_file_copy_ingestion_service as svc

    monkeypatch.setattr(
        svc,
        "_validate_remote_target",
        lambda url: {
            "success": True,
            "url": url,
            "host": "example.com",
            "resolved_addresses": ["198.51.100.20"],
        },
    )
    response = _StubResponse(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        body_chunks=[b"late bytes"],
        url="https://example.com/slow.pdf",
    )
    session = _StubSession([response])
    monkeypatch.setattr(svc, "_create_requests_session", lambda: session)

    ticks = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(svc.time, "monotonic", lambda: next(ticks, 2.0))

    result = svc.download_remote_file_copy_bytes(
        url="https://example.com/slow.pdf",
        timeout_seconds=1,
    )

    assert result["success"] is False
    assert result["error"] == "remote_file_copy_timeout"
    assert result["timeout_seconds"] == 1.0
    assert result["elapsed_seconds"] == 2.0
    assert result["final_url"] == "https://example.com/slow.pdf"
    assert result["status_code"] == 200
    assert session.calls[0]["kwargs"]["timeout"] == (1.0, 1.0)
    assert response.closed is True
    assert session.closed is True


def test_import_remote_url_file_copy_persists_download_provenance(monkeypatch):
    from src.backend.services import remote_file_copy_ingestion_service as svc

    captured_download: dict[str, object] = {}

    def _fake_download_remote_file_copy_bytes(**kwargs):
        captured_download.update(kwargs)
        return {
            "success": True,
            "requested_url": "https://example.com/paper",
            "final_url": "https://cdn.example.com/paper.pdf",
            "redirects": [
                {
                    "status_code": 302,
                    "from_url": "https://example.com/paper",
                    "to_url": "https://cdn.example.com/paper.pdf",
                }
            ],
            "hops": [{"url": "https://example.com/paper", "host": "example.com"}],
            "status_code": 200,
            "size_bytes": 7,
            "data": b"pdfdata",
            "original_filename": "paper.pdf",
            "filename_source": "response.content_disposition",
            "response_content_type": "application/pdf",
            "content_type": "application/pdf",
            "content_type_source": "response.content_type",
            "response_headers": {
                "content_type": "application/pdf",
                "content_disposition": 'attachment; filename="paper.pdf"',
            },
        }

    monkeypatch.setattr(
        svc,
        "download_remote_file_copy_bytes",
        _fake_download_remote_file_copy_bytes,
    )

    captured: dict[str, object] = {}

    def _fake_import_bytes_file_copy(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "concept_id": "#V#imported_remote_file",
            "type_concept_id": "#V#computer_file_copy",
            "uploaded_at": "2026-03-08T00:00:00+00:00",
            "storage": {
                "backend": "swift",
                "key": "imports/user/hash/paper.pdf",
                "uri": "swift://bucket/imports/user/hash/paper.pdf",
                "size_bytes": 7,
            },
            "artifact_record": {"artifact_id": "#V#imported_remote_file"},
            "typing": {"typing_persisted": True},
            "typing_result": {
                "detected_type_concept_ids": ["#V#pdf_computer_file_copy"]
            },
        }

    monkeypatch.setattr(svc, "import_bytes_file_copy", _fake_import_bytes_file_copy)

    result = svc.import_remote_url_file_copy(
        url="https://example.com/paper",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        namespace_source="request.namespace",
        timeout_seconds=12.5,
        registration_timeout_seconds=7.5,
    )

    assert result["success"] is True
    assert captured_download["timeout_seconds"] == 12.5
    assert result["download_timeout_seconds"] == 12.5
    assert result["registration_timeout_seconds"] == 7.5
    assert result["timeout_policy"] == {
        "download_timeout_seconds": 12.5,
        "registration_timeout_seconds": 7.5,
        "registration_advisory_seconds": 7.5,
        "download_phase": "remote_download",
        "registration_phase": "file_copy_registration",
        "download_elapsed_time_enforcement": "hard_external_resource",
        "registration_elapsed_time_enforcement": "advisory",
    }
    assert captured["original_filename"] == "paper.pdf"
    assert captured["organisation_concept_id"] == "#V#org"
    assert captured["namespace"] == "#V#user@org"
    assert captured["namespace_source"] == "request.namespace"
    assert captured["source_identifier"] == "https://example.com/paper"
    assert captured["source_uri"] == "https://cdn.example.com/paper.pdf"
    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["response_status_code"] == "200"
    assert metadata["redirect_count"] == 1
    assert result["registration_lookup"] == {
        "sha256": (
            "d23c47e2668cdbc7f204ad3988579fb541ac7ca8abf6038d07236b8a2ba02c1f"
        ),
        "size_bytes": 7,
        "original_filename": "paper.pdf",
        "safe_filename": "paper.pdf",
        "blob_key": (
            "imports/user/"
            "d23c47e2668cdbc7f204ad3988579fb541ac7ca8abf6038d07236b8a2ba02c1f/"
            "paper.pdf"
        ),
        "type_concept_id": "#V#computer_file_copy",
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
        "source_identifier": "https://example.com/paper",
        "source_uri": "https://cdn.example.com/paper.pdf",
    }
    assert result["response"]["status_code"] == 200
    assert result["response"]["size_bytes"] == 7
    assert result["filename_resolution"]["source"] == "response.content_disposition"
    assert (
        result["content_type_resolution"]["effective_content_type"] == "application/pdf"
    )


def test_import_remote_url_file_copy_retains_registration_after_advisory(monkeypatch):
    from src.backend.services import remote_file_copy_ingestion_service as svc

    monkeypatch.setattr(
        svc,
        "download_remote_file_copy_bytes",
        lambda **_kwargs: {
            "success": True,
            "requested_url": "https://example.com/paper",
            "final_url": "https://cdn.example.com/paper.pdf",
            "redirects": [],
            "hops": [{"url": "https://example.com/paper", "host": "example.com"}],
            "status_code": 200,
            "size_bytes": 7,
            "data": b"pdfdata",
            "original_filename": "paper.pdf",
            "filename_source": "response.content_disposition",
            "response_content_type": "application/pdf",
            "content_type": "application/pdf",
            "content_type_source": "response.content_type",
            "response_headers": {"content_type": "application/pdf"},
        },
    )

    worker_released = threading.Event()

    def _slow_import_bytes_file_copy(**_kwargs):
        time.sleep(0.1)
        worker_released.set()
        return {"success": True, "concept_id": "#V#late_file"}

    monkeypatch.setattr(svc, "import_bytes_file_copy", _slow_import_bytes_file_copy)

    result = svc.import_remote_url_file_copy(
        url="https://example.com/paper",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        namespace_source="request.namespace",
        timeout_seconds=5,
        registration_advisory_seconds=0.02,
    )

    assert result["success"] is True
    assert result["concept_id"] == "#V#late_file"
    assert result["timeout_seconds"] == 5.0
    assert result["download_timeout_seconds"] == 5.0
    assert result["registration_timeout_seconds"] == 0.02
    assert result["registration_advisory_seconds"] == 0.02
    expected_lookup = {
        "sha256": (
            "d23c47e2668cdbc7f204ad3988579fb541ac7ca8abf6038d07236b8a2ba02c1f"
        ),
        "size_bytes": 7,
        "original_filename": "paper.pdf",
        "safe_filename": "paper.pdf",
        "blob_key": (
            "imports/user/"
            "d23c47e2668cdbc7f204ad3988579fb541ac7ca8abf6038d07236b8a2ba02c1f/"
            "paper.pdf"
        ),
        "type_concept_id": "#V#computer_file_copy",
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
        "source_identifier": "https://example.com/paper",
        "source_uri": "https://cdn.example.com/paper.pdf",
    }
    assert result["registration_lookup"] == expected_lookup
    assert result["registration_timing"] == {
        "elapsed_time_enforcement": "advisory",
        "advisory_timeout_seconds": 0.02,
        "advisory_exceeded": True,
        "hard_timeout_seconds": None,
    }
    assert result["requested_url"] == "https://example.com/paper"
    assert result["response"]["status_code"] == 200
    assert worker_released.wait(timeout=1.0)
