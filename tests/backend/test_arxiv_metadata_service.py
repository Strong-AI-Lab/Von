from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.arxiv_metadata_service import (
    ArxivMetadataError,
    fetch_arxiv_metadata,
)


class _FakeHeaders:
    def __init__(self, charset: str = "utf-8"):
        self._charset = charset

    def get_content_charset(self) -> str:
        return self._charset


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = _FakeHeaders()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_fetch_arxiv_metadata_merges_atom_and_abs_page(monkeypatch: pytest.MonkeyPatch) -> None:
    atom_payload = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2603.21702v1</id>
    <updated>2026-03-25T00:00:00Z</updated>
    <published>2026-03-24T00:00:00Z</published>
    <title>  Obscure Result  </title>
    <summary>  An obscure but important abstract.  </summary>
    <author><name>Author One</name></author>
    <author><name>Author Two</name></author>
    <link title="pdf" href="https://arxiv.org/pdf/2603.21702v1.pdf" type="application/pdf" />
    <arxiv:primary_category term="cs.AI" />
    <category term="cs.AI" />
  </entry>
</feed>
"""
    html_payload = """
<html><head>
  <meta name="citation_title" content="Obscure Result" />
  <meta name="citation_author" content="Author One" />
  <meta name="citation_author" content="Author Two" />
  <meta name="citation_abstract" content="An obscure but important abstract." />
  <meta name="citation_date" content="2026-03-24" />
  <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2603.21702v1.pdf" />
</head><body></body></html>
"""

    def _fake_urlopen(request: Any, timeout: float = 0.0) -> _FakeResponse:
        url = getattr(request, "full_url", str(request))
        if "export.arxiv.org" in url:
            return _FakeResponse(atom_payload)
        if "arxiv.org/abs/" in url:
            return _FakeResponse(html_payload)
        raise AssertionError(f"unexpected urlopen URL: {url}")

    monkeypatch.setattr(
        "src.backend.services.arxiv_metadata_service.urlopen",
        _fake_urlopen,
    )

    metadata = fetch_arxiv_metadata("https://arxiv.org/abs/2603.21702")

    assert metadata["id"] == "2603.21702"
    assert metadata["versioned_id"] == "2603.21702v1"
    assert metadata["title"] == "Obscure Result"
    assert metadata["summary"] == "An obscure but important abstract."
    assert metadata["abstract"] == "An obscure but important abstract."
    assert metadata["authors"] == ["Author One", "Author Two"]
    assert metadata["publication_date"] == "2026-03-24"
    assert metadata["pdf_url"] == "https://arxiv.org/pdf/2603.21702v1.pdf"
    assert metadata["source_uri"] == "https://arxiv.org/abs/2603.21702"
    assert metadata["metadata_source"] == "arxiv_atom_api+arxiv_abs_page"


def test_fetch_arxiv_metadata_fails_when_no_source_returns_complete_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.arxiv_metadata_service.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ArxivMetadataError("boom")),
    )

    with pytest.raises(ArxivMetadataError, match="arxiv_metadata_unavailable"):
        fetch_arxiv_metadata("2603.21702")


def test_fetch_arxiv_metadata_html_fallback_normalises_authors_dates_and_subject_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_payload = """
<html><head>
  <meta name="citation_title" content="Neutral representations of finite diagonalizable group schemes and fields of moduli" />
  <meta name="citation_author" content="Bresciani, Giulio" />
  <meta name="citation_author" content="Vistoli, Angelo" />
  <meta name="citation_author" content="Yang, Tianzhi" />
  <meta name="citation_abstract" content="An obscure but important abstract." />
  <meta name="citation_date" content="2026/03/23" />
  <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2603.21702" />
</head><body>
  <div class="metatable">
    <table summary="Additional metadata"><tr>
      <td class="tablecell label">Subjects:</td>
      <td class="tablecell subjects">
        <span class="primary-subject">Algebraic Geometry (math.AG)</span>; Number Theory (math.NT)
      </td>
    </tr></table>
  </div>
</body></html>
"""

    def _fake_urlopen(request: Any, timeout: float = 0.0) -> _FakeResponse:
        url = getattr(request, "full_url", str(request))
        if "export.arxiv.org" in url:
            raise ArxivMetadataError("arxiv_http_error:503")
        if "arxiv.org/abs/" in url:
            return _FakeResponse(html_payload)
        raise AssertionError(f"unexpected urlopen URL: {url}")

    monkeypatch.setattr(
        "src.backend.services.arxiv_metadata_service.urlopen",
        _fake_urlopen,
    )

    metadata = fetch_arxiv_metadata("2603.21702")

    assert metadata["title"] == "Neutral representations of finite diagonalizable group schemes and fields of moduli"
    assert metadata["authors"] == [
        "Giulio Bresciani",
        "Angelo Vistoli",
        "Tianzhi Yang",
    ]
    assert metadata["publication_date"] == "2026-03-23"
    assert metadata["primary_category"] == "math.AG"
    assert metadata["categories"] == ["math.AG", "math.NT"]
    assert metadata["metadata_source"] == "arxiv_abs_page"
