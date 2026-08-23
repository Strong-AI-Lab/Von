"""Exact public DOI metadata retrieval through the Crossref REST API.

The caller supplies only a DOI.  This module owns the fixed HTTPS endpoint,
normalises and verifies the returned DOI, and projects a bounded
source-neutral paper-metadata record.  It deliberately does not follow URLs
or expose arbitrary fetched content, so an untrusted email or model output
cannot turn this read into an SSRF primitive or enlarge write authority.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, unquote

import requests

from .gateway import MethodDefinition
from .schemas import Schema

_CROSSREF_API_BASE_URL = "https://api.crossref.org/works"
_CROSSREF_USER_AGENT = "VonResearchAssistant/1.0 (https://github.com/Strong-AI-Lab/Von)"
_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def normalise_doi(value: Any) -> str:
    """Return one canonical comparison form for a DOI, or ``""``."""

    text = unquote(str(value or "")).strip().strip("<>")
    lowered = text.lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip().rstrip(".,;")
    if not _DOI_PATTERN.fullmatch(text):
        return ""
    return text.lower()


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return ""


def _author_names(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    names: list[str] = []
    for item in value[:500]:
        if not isinstance(item, Mapping):
            continue
        name = _first_text(item.get("name"))
        if not name:
            name = " ".join(
                part
                for part in (
                    _first_text(item.get("given")),
                    _first_text(item.get("family")),
                )
                if part
            ).strip()
        if name and name not in names:
            names.append(name[:500])
    return names


def _publication_date(message: Mapping[str, Any]) -> str:
    for key in (
        "published-print",
        "published-online",
        "published",
        "issued",
        "created",
    ):
        value = message.get(key)
        if not isinstance(value, Mapping):
            continue
        date_parts = value.get("date-parts")
        if not isinstance(date_parts, Sequence) or not date_parts:
            continue
        first = date_parts[0]
        if not isinstance(first, Sequence) or isinstance(
            first, (str, bytes, bytearray)
        ):
            continue
        integers = [part for part in first[:3] if isinstance(part, int)]
        if not integers:
            continue
        year = integers[0]
        if len(integers) == 1:
            return f"{year:04d}"
        month = integers[1]
        if len(integers) == 2:
            return f"{year:04d}-{month:02d}"
        return f"{year:04d}-{month:02d}-{integers[2]:02d}"
    return ""


def _clean_abstract(value: Any) -> str:
    text = _first_text(value)
    if not text:
        return ""
    return " ".join(html.unescape(_HTML_TAG_PATTERN.sub(" ", text)).split())[:50_000]


def _error(error_code: str, message: str, *, doi: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "error_code": error_code,
        "error": message,
        "doi": doi or None,
        "exact_doi_verified": False,
        "metadata_source": "crossref_rest_api",
    }


def fetch_crossref_doi_metadata(
    doi: Any,
    *,
    timeout_sec: float = 20.0,
    session: Any = None,
) -> dict[str, Any]:
    """Fetch, verify, and project public metadata for one exact DOI."""

    canonical_doi = normalise_doi(doi)
    if not canonical_doi:
        return _error("invalid_doi", "A valid DOI is required.")

    api_url = f"{_CROSSREF_API_BASE_URL}/{quote(canonical_doi, safe='')}"
    client = session or requests
    try:
        response = client.get(
            api_url,
            headers={
                "Accept": "application/json",
                "User-Agent": _CROSSREF_USER_AGENT,
            },
            timeout=max(1.0, min(float(timeout_sec), 30.0)),
        )
    except requests.Timeout:
        return _error(
            "crossref_timeout",
            "Crossref metadata retrieval timed out.",
            doi=canonical_doi,
        )
    except requests.RequestException as exc:
        return _error(
            "crossref_request_failed",
            f"Crossref metadata retrieval failed: {type(exc).__name__}.",
            doi=canonical_doi,
        )

    if int(getattr(response, "status_code", 0) or 0) == 404:
        return _error(
            "crossref_doi_not_found",
            "Crossref did not find the supplied DOI.",
            doi=canonical_doi,
        )
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        return _error(
            "crossref_response_invalid",
            f"Crossref returned an unusable response: {type(exc).__name__}.",
            doi=canonical_doi,
        )

    if not isinstance(payload, Mapping) or payload.get("status") != "ok":
        return _error(
            "crossref_response_invalid",
            "Crossref returned an unusable response envelope.",
            doi=canonical_doi,
        )
    message = payload.get("message")
    if not isinstance(message, Mapping):
        return _error(
            "crossref_response_invalid",
            "Crossref returned no work metadata.",
            doi=canonical_doi,
        )

    observed_doi = normalise_doi(message.get("DOI"))
    if observed_doi != canonical_doi:
        return _error(
            "crossref_doi_mismatch",
            "Crossref metadata did not read back the requested DOI.",
            doi=canonical_doi,
        )

    title = _first_text(message.get("title"))[:2_000]
    if not title:
        return _error(
            "crossref_metadata_incomplete",
            "Crossref returned no paper title for the requested DOI.",
            doi=canonical_doi,
        )

    author_names = _author_names(message.get("author"))
    abstract = _clean_abstract(message.get("abstract"))
    publication_date = _publication_date(message)
    topic_labels = [
        text[:500]
        for item in (message.get("subject") or [])[:100]
        if (text := _first_text(item))
    ]
    source_uri = normalise_doi(message.get("URL"))
    source_uri = (
        f"https://doi.org/{source_uri}"
        if source_uri
        else f"https://doi.org/{canonical_doi}"
    )

    paper_metadata: dict[str, Any] = {
        "title": title,
        "doi": canonical_doi,
        "source_uri": source_uri,
    }
    if author_names:
        paper_metadata["authors"] = author_names
    if abstract:
        paper_metadata["abstract"] = abstract
    if publication_date:
        paper_metadata["publication_date"] = publication_date
    if topic_labels:
        paper_metadata["categories"] = topic_labels

    return {
        "success": True,
        "doi": canonical_doi,
        "source_uri": source_uri,
        "title": title,
        "author_names": author_names,
        "summary": abstract or None,
        "publication_date": publication_date or None,
        "topic_labels": topic_labels,
        "paper_metadata": paper_metadata,
        "exact_doi_verified": True,
        "metadata_source": "crossref_rest_api",
        "metadata_source_uri": api_url,
    }


def build_crossref_metadata_tool_definition() -> MethodDefinition:
    """Return the self-contained internal-MCP definition for exact DOI reads."""

    return MethodDefinition(
        name="get_doi_metadata",
        handler=lambda **kwargs: fetch_crossref_doi_metadata(kwargs.get("doi")),
        input_schema=Schema(
            required={"doi": str},
            optional={},
            allow_unknown=False,
            description=(
                "get_doi_metadata input: doi (str, required, exact DOI or doi.org "
                "URL). The tool queries only the fixed Crossref REST endpoint."
            ),
        ),
        output_schema=Schema(
            required={
                "success": bool,
                "exact_doi_verified": bool,
                "metadata_source": str,
            },
            optional={
                "doi": (str, type(None)),
                "source_uri": (str, type(None)),
                "title": (str, type(None)),
                "author_names": (list, type(None)),
                "summary": (str, type(None)),
                "publication_date": (str, type(None)),
                "topic_labels": (list, type(None)),
                "paper_metadata": (dict, type(None)),
                "metadata_source_uri": (str, type(None)),
                "error_code": (str, type(None)),
                "error": (str, type(None)),
            },
            allow_unknown=False,
            description=(
                "get_doi_metadata output: exact-DOI-verified Crossref metadata in "
                "a source-neutral paper_metadata record, or a typed failure."
            ),
        ),
        category="read",
        ordinary_turn_public=True,
        timeout_sec=25.0,
        description=(
            "Resolve one exact DOI to public scholarly metadata through the fixed "
            "Crossref REST endpoint. The returned DOI must exactly match the request."
        ),
    )


__all__ = [
    "build_crossref_metadata_tool_definition",
    "fetch_crossref_doi_metadata",
    "normalise_doi",
]
