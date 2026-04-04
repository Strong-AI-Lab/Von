

class _StubResponse:
    def __init__(self, *, ok: bool, payload: dict, status_code: int = 200):
        self.ok = ok
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return dict(self._payload)


def test_rag_get_status_defaults_to_von_web_port_5000(monkeypatch):
    monkeypatch.delenv("VON_HTTP_BASE_URL", raising=False)
    monkeypatch.delenv("VON_WEB_BASE_URL", raising=False)
    monkeypatch.setenv("VON_DEFAULT_NAMESPACE", "#V#test_user")

    captured = {}

    def _fake_get(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return _StubResponse(ok=True, payload={"total": 1, "indexed": 1})

    monkeypatch.setattr("requests.get", _fake_get)

    from src.backend.integrations.internal_mcp.catalogue import _rag_get_status

    payload = _rag_get_status()

    assert captured["timeout"] == 5
    assert captured["url"].startswith("http://127.0.0.1:5000/admin/rag_status")
    assert "namespace=#V#test_user" in captured["url"]
    assert payload.get("success") is not False


def test_rag_get_status_honours_configured_http_base_url(monkeypatch):
    monkeypatch.setenv("VON_HTTP_BASE_URL", "http://127.0.0.1:5999")
    monkeypatch.setenv("VON_DEFAULT_NAMESPACE", "#V#test_user")

    captured = {}

    def _fake_get(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return _StubResponse(ok=True, payload={"total": 2, "indexed": 2})

    monkeypatch.setattr("requests.get", _fake_get)

    from src.backend.integrations.internal_mcp.catalogue import _rag_get_status

    _rag_get_status(detail=True)

    assert captured["url"].startswith("http://127.0.0.1:5999/admin/rag_status")
    assert "detail=1" in captured["url"]
