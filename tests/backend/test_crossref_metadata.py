from __future__ import annotations

import requests

from src.backend.integrations.internal_mcp.crossref_metadata import (
    fetch_crossref_doi_metadata,
    normalise_doi,
)


class _Response:
    def __init__(self, payload, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response=None, *, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


def test_normalise_doi_accepts_exact_doi_and_doi_url() -> None:
    assert normalise_doi(" DOI:10.1007/Example.1 ") == "10.1007/example.1"
    assert normalise_doi("https://doi.org/10.1145%2FABC.DEF") == ("10.1145/abc.def")
    assert normalise_doi("https://example.org/not-a-doi") == ""


def test_fetch_crossref_doi_metadata_verifies_and_projects_exact_work() -> None:
    session = _Session(
        _Response(
            {
                "status": "ok",
                "message": {
                    "DOI": "10.1007/978-3-031-82039-7_10",
                    "URL": "https://doi.org/10.1007/978-3-031-82039-7_10",
                    "title": [
                        "Norm Violation Detection in Multi-Agent Systems Using Large Language Models"
                    ],
                    "author": [
                        {"given": "Ada", "family": "Lovelace"},
                        {"name": "Grace Hopper"},
                    ],
                    "abstract": "<jats:p>A public abstract.</jats:p>",
                    "published": {"date-parts": [[2025, 2, 3]]},
                    "subject": ["Artificial intelligence"],
                },
            }
        )
    )

    result = fetch_crossref_doi_metadata(
        "https://doi.org/10.1007/978-3-031-82039-7_10",
        session=session,
    )

    assert result["success"] is True
    assert result["exact_doi_verified"] is True
    assert result["doi"] == "10.1007/978-3-031-82039-7_10"
    assert result["author_names"] == ["Ada Lovelace", "Grace Hopper"]
    assert result["publication_date"] == "2025-02-03"
    assert result["summary"] == "A public abstract."
    assert result["paper_metadata"]["categories"] == ["Artificial intelligence"]
    assert session.calls[0]["url"].endswith("/10.1007%2F978-3-031-82039-7_10")


def test_fetch_crossref_doi_metadata_rejects_mismatched_readback() -> None:
    session = _Session(
        _Response(
            {
                "status": "ok",
                "message": {
                    "DOI": "10.1000/a-different-work",
                    "title": ["Wrong work"],
                },
            }
        )
    )

    result = fetch_crossref_doi_metadata("10.1000/requested", session=session)

    assert result["success"] is False
    assert result["error_code"] == "crossref_doi_mismatch"
    assert result["exact_doi_verified"] is False


def test_fetch_crossref_doi_metadata_returns_typed_not_found_and_timeout() -> None:
    not_found = fetch_crossref_doi_metadata(
        "10.1000/missing",
        session=_Session(_Response({}, status_code=404)),
    )
    timed_out = fetch_crossref_doi_metadata(
        "10.1000/slow",
        session=_Session(error=requests.Timeout("slow")),
    )

    assert not_found["error_code"] == "crossref_doi_not_found"
    assert timed_out["error_code"] == "crossref_timeout"
