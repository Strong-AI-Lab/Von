import base64
from hashlib import sha256

from src.backend.mcp_server import mcp_server as server


class Response:
    ok = True
    status_code = 200

    def __init__(self, chunks, *, inline=False, fail=False):
        self.headers = {"Content-Type": "application/octet-stream"}
        self.chunks = chunks
        self.inline = inline
        self.fail = fail
        self.closed = False
        self.consumed = 0

    @property
    def content(self):
        assert self.inline, "Staged transfer must stream, never buffer response.content"
        return b"".join(self.chunks)

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk
        if self.fail:
            raise server.requests.exceptions.ConnectionError("interrupted")

    def close(self):
        self.closed = True


def test_attachment_streams_into_parent_directory_with_integrity_receipt(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("VON_JIRA_ATTACHMENT_STAGING_DIR", str(tmp_path))
    monkeypatch.setattr(
        server, "jira_get", lambda _: {"size": 6, "filename": "../../source.bin"}
    )
    response = Response([b"abc", b"def"])

    def get(url, **kwargs):
        assert kwargs["stream"] is True
        return response

    monkeypatch.setattr(server.requests, "get", get)
    result = server.jira_get_attachment_content(attachment_id="1", max_size_bytes=10)
    assert result["success"] and "content_base64" not in result
    receipt = result["staged_file"]
    assert (tmp_path / receipt["name"]).read_bytes() == b"abcdef"
    assert receipt["sha256"] == sha256(b"abcdef").hexdigest()
    assert receipt["size_bytes"] == 6
    assert response.closed


def test_stream_byte_limit_stops_oversized_body_and_removes_partial_file(
    monkeypatch, tmp_path
):
    response = Response([b"abc", b"def", b"must not be consumed"])
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: response)
    destination = tmp_path / "file.bin"
    result = server._request_bytes(
        "https://example.invalid", destination=destination, max_size_bytes=5
    )
    assert result == {"success": False, "error": "attachment_too_large"}
    assert response.consumed == 2 and response.closed
    assert not destination.exists()


def test_interrupted_stream_retries_without_appending_partial_bytes(monkeypatch, tmp_path):
    first = Response([b"partial"], fail=True)
    second = Response([b"complete"])
    responses = iter([first, second])
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    destination = tmp_path / "file.bin"
    result = server._request_bytes(
        "https://example.invalid", destination=destination, max_size_bytes=100
    )
    assert result["success"]
    assert destination.read_bytes() == b"complete"
    assert result["staged_file"]["sha256"] == sha256(b"complete").hexdigest()
    assert first.closed and second.closed


def test_default_helper_contract_still_returns_base64(monkeypatch):
    monkeypatch.delenv("VON_JIRA_ATTACHMENT_STAGING_DIR", raising=False)
    monkeypatch.setattr(server, "jira_get", lambda _: {"size": 3, "filename": "a.bin"})
    response = Response([b"abc"], inline=True)
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: response)
    result = server.jira_get_attachment_content(attachment_id="1")
    assert result["success"]
    assert base64.b64decode(result["content_base64"]) == b"abc"
    assert "staged_file" not in result
