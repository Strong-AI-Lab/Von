"""Deterministic arXiv metadata retrieval helpers.

This service provides a stable fallback when the upstream arXiv MCP proxy does
not expose metadata-fetch operations. The returned payload is intentionally
small, normalised, and suitable for workflow verification surfaces.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import unescape
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ARXIV_METADATA_SCHEMA_VERSION = "arxiv_metadata_record.v1"
_ARXIV_API_URL_TEMPLATE = "https://export.arxiv.org/api/query?id_list={arxiv_id}"
_ARXIV_ABS_URL_TEMPLATE = "https://arxiv.org/abs/{arxiv_id}"
_ARXIV_DEFAULT_TIMEOUT_SECONDS = 15.0
_ARXIV_USER_AGENT = "Von/1.0 (+https://ai.ac.nz)"
_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_META_TAG_PATTERN = re.compile(r"<meta\s+[^>]*?>", re.IGNORECASE | re.DOTALL)
_HTML_ATTR_PATTERN = re.compile(
    r'([a-zA-Z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')',
    re.DOTALL,
)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_HTML_SUBJECTS_ROW_PATTERN = re.compile(
    r'<td[^>]*class="tablecell label"[^>]*>\s*Subjects:\s*</td>\s*'
    r'<td[^>]*class="tablecell subjects"[^>]*>(.*?)</td>',
    re.IGNORECASE | re.DOTALL,
)
_ARXIV_CATEGORY_CODE_PATTERN = re.compile(r"\b([a-z\-]+(?:\.[A-Z\-]+)+)\b")


class ArxivMetadataError(RuntimeError):
    """Raised when arXiv metadata cannot be retrieved or normalised."""


def _normalise_whitespace(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", unescape(value)).strip()
    return text or None


def _normalise_arxiv_id(arxiv_id: Any) -> str:
    text = str(arxiv_id or "").strip()
    if not text:
        raise ArxivMetadataError("arxiv_id_required")
    if text.lower().startswith("arxiv:"):
        text = text.split(":", 1)[1].strip()
    if "arxiv.org/" in text.lower():
        match = re.search(
            r"(?i)arxiv\.org/(?:abs|pdf)/((?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5})(?:v\d+)?)",
            text,
        )
        if match:
            text = match.group(1)
    text = text.removesuffix(".pdf").strip()
    if not text:
        raise ArxivMetadataError("arxiv_id_required")
    return text


def _strip_arxiv_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", str(arxiv_id or "").strip(), flags=re.IGNORECASE)


def _derive_publication_date(raw_value: Any) -> str | None:
    text = _normalise_whitespace(raw_value)
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0].strip()
    if re.match(r"^\d{4}/\d{2}/\d{2}$", text):
        text = text.replace("/", "-")
    match = re.match(r"^\d{4}-\d{2}-\d{2}$", text)
    return text if match else None


def _normalise_html_author_name(raw_value: Any) -> str | None:
    text = _normalise_whitespace(raw_value)
    if not text or "," not in text:
        return text
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) != 2:
        return text
    return f"{parts[1]} {parts[0]}".strip() or text


def _extract_subject_category_codes_from_html(html_text: str) -> list[str]:
    match = _HTML_SUBJECTS_ROW_PATTERN.search(html_text)
    if match is None:
        return []
    subjects_html = match.group(1)
    subjects_text = _normalise_whitespace(_HTML_TAG_PATTERN.sub(" ", subjects_html))
    if not subjects_text:
        return []
    categories: list[str] = []
    seen: set[str] = set()
    for item in _ARXIV_CATEGORY_CODE_PATTERN.findall(subjects_text):
        category = _normalise_whitespace(item)
        if not category:
            continue
        fingerprint = category.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        categories.append(category)
    return categories


def _http_get_text(url: str, *, timeout_seconds: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": _ARXIV_USER_AGENT,
            "Accept": "application/atom+xml, text/html;q=0.9, */*;q=0.1",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:  # pragma: no cover - network/error-path dependent
        raise ArxivMetadataError(f"arxiv_http_error:{exc.code}") from exc
    except URLError as exc:  # pragma: no cover - network/error-path dependent
        raise ArxivMetadataError(
            f"arxiv_network_error:{getattr(exc, 'reason', exc)}"
        ) from exc
    except TimeoutError as exc:  # pragma: no cover - network/error-path dependent
        raise ArxivMetadataError("arxiv_metadata_timeout") from exc


def _parse_atom_entry(xml_text: str) -> Mapping[str, Any] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    entry = root.find("atom:entry", _ATOM_NS)
    if entry is None:
        return None

    authors = [
        author_name
        for author_name in (
            _normalise_whitespace(item.text)
            for item in entry.findall("atom:author/atom:name", _ATOM_NS)
        )
        if author_name
    ]

    category_terms: list[str] = []
    seen_categories: set[str] = set()
    for item in entry.findall("atom:category", _ATOM_NS):
        term = _normalise_whitespace(item.attrib.get("term"))
        if not term or term.casefold() in seen_categories:
            continue
        seen_categories.add(term.casefold())
        category_terms.append(term)

    pdf_url = None
    for link in entry.findall("atom:link", _ATOM_NS):
        href = _normalise_whitespace(link.attrib.get("href"))
        title = _normalise_whitespace(link.attrib.get("title"))
        media_type = _normalise_whitespace(link.attrib.get("type"))
        if href and (title == "pdf" or media_type == "application/pdf"):
            pdf_url = href
            break

    raw_id = _normalise_whitespace(entry.findtext("atom:id", default="", namespaces=_ATOM_NS))
    versioned_id = None
    if raw_id:
        versioned_id = raw_id.rsplit("/", 1)[-1].strip()

    primary_category = None
    primary_node = entry.find("arxiv:primary_category", _ATOM_NS)
    if primary_node is not None:
        primary_category = _normalise_whitespace(primary_node.attrib.get("term"))

    summary = _normalise_whitespace(
        entry.findtext("atom:summary", default="", namespaces=_ATOM_NS)
    )
    published = _normalise_whitespace(
        entry.findtext("atom:published", default="", namespaces=_ATOM_NS)
    )
    updated = _normalise_whitespace(
        entry.findtext("atom:updated", default="", namespaces=_ATOM_NS)
    )

    return {
        "versioned_id": versioned_id,
        "title": _normalise_whitespace(
            entry.findtext("atom:title", default="", namespaces=_ATOM_NS)
        ),
        "summary": summary,
        "abstract": summary,
        "authors": authors,
        "categories": category_terms,
        "primary_category": primary_category,
        "published": published,
        "publication_date": _derive_publication_date(published),
        "updated": updated,
        "doi": _normalise_whitespace(
            entry.findtext("arxiv:doi", default="", namespaces=_ATOM_NS)
        ),
        "comments": _normalise_whitespace(
            entry.findtext("arxiv:comment", default="", namespaces=_ATOM_NS)
        ),
        "pdf_url": pdf_url,
    }


def _extract_html_meta_properties(html_text: str) -> dict[str, list[str]]:
    properties: dict[str, list[str]] = {}
    for raw_tag in _META_TAG_PATTERN.findall(html_text):
        attrs: dict[str, str] = {}
        for attr_name, double_value, single_value in _HTML_ATTR_PATTERN.findall(raw_tag):
            value = double_value if double_value != "" else single_value
            attrs[attr_name.lower()] = value
        name = _normalise_whitespace(attrs.get("name"))
        content = _normalise_whitespace(attrs.get("content"))
        if not name or not content:
            continue
        key = name.casefold()
        properties.setdefault(key, []).append(content)
    return properties


def _parse_abs_html(html_text: str) -> Mapping[str, Any] | None:
    properties = _extract_html_meta_properties(html_text)
    if not properties:
        return None

    authors = [
        author
        for author in (
            _normalise_html_author_name(item)
            for item in properties.get("citation_author", [])
        )
        if author
    ]
    title = next(iter(properties.get("citation_title", [])), None)
    abstract = next(iter(properties.get("citation_abstract", [])), None)
    published = (
        next(iter(properties.get("citation_date", [])), None)
        or next(iter(properties.get("citation_online_date", [])), None)
    )
    pdf_url = next(iter(properties.get("citation_pdf_url", [])), None)
    subject_categories = _extract_subject_category_codes_from_html(html_text)
    primary_category = next(iter(properties.get("citation_keywords", [])), None) or next(
        iter(subject_categories),
        None,
    )

    if not any((title, abstract, authors, published, pdf_url, primary_category)):
        return None

    categories = (
        subject_categories
        if subject_categories
        else [primary_category]
        if primary_category
        else []
    )
    return {
        "title": title,
        "summary": abstract,
        "abstract": abstract,
        "authors": authors,
        "categories": categories,
        "primary_category": primary_category,
        "published": published,
        "publication_date": _derive_publication_date(published),
        "updated": None,
        "doi": None,
        "comments": None,
        "pdf_url": pdf_url,
    }


def _merge_metadata_records(
    *,
    requested_id: str,
    atom_record: Mapping[str, Any] | None,
    html_record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    canonical_id = _strip_arxiv_version(requested_id)
    versioned_id = None
    if isinstance(atom_record, Mapping):
        versioned_id = _normalise_whitespace(atom_record.get("versioned_id"))

    merged: dict[str, Any] = {
        "schema_version": ARXIV_METADATA_SCHEMA_VERSION,
        "id": canonical_id,
        "versioned_id": versioned_id,
        "source_uri": _ARXIV_ABS_URL_TEMPLATE.format(arxiv_id=canonical_id),
    }
    for source_name, source_record in (
        ("arxiv_atom_api", atom_record),
        ("arxiv_abs_page", html_record),
    ):
        if not isinstance(source_record, Mapping):
            continue
        for key, value in source_record.items():
            if key in {"schema_version", "id", "source_uri", "metadata_source"}:
                continue
            existing = merged.get(key)
            if existing in (None, "", [], ()):
                merged[key] = value
        merged.setdefault("metadata_source_candidates", []).append(source_name)

    if not merged.get("pdf_url"):
        pdf_id = versioned_id or canonical_id
        merged["pdf_url"] = f"https://arxiv.org/pdf/{pdf_id}.pdf"
    if not merged.get("published") and merged.get("publication_date"):
        merged["published"] = merged.get("publication_date")
    if not merged.get("abstract") and merged.get("summary"):
        merged["abstract"] = merged.get("summary")
    if not merged.get("summary") and merged.get("abstract"):
        merged["summary"] = merged.get("abstract")
    if not isinstance(merged.get("authors"), list):
        merged["authors"] = []
    if not isinstance(merged.get("categories"), list):
        merged["categories"] = []

    candidates = merged.get("metadata_source_candidates")
    if isinstance(candidates, list) and candidates:
        merged["metadata_source"] = "+".join(candidates)
    else:
        merged["metadata_source"] = "unknown"
    return merged


def fetch_arxiv_metadata(
    arxiv_id: str,
    *,
    timeout_seconds: float = _ARXIV_DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch normalised arXiv metadata for one paper.

    Raises:
        ArxivMetadataError: If metadata could not be retrieved from either the
            Atom API or the abstract page fallback.
    """

    requested_id = _normalise_arxiv_id(arxiv_id)
    api_url = _ARXIV_API_URL_TEMPLATE.format(arxiv_id=quote(requested_id, safe=""))
    abs_url = _ARXIV_ABS_URL_TEMPLATE.format(arxiv_id=_strip_arxiv_version(requested_id))

    atom_record: Mapping[str, Any] | None = None
    html_record: Mapping[str, Any] | None = None
    fetch_errors: list[str] = []

    try:
        atom_record = _parse_atom_entry(
            _http_get_text(api_url, timeout_seconds=timeout_seconds)
        )
    except ArxivMetadataError as exc:
        fetch_errors.append(str(exc))

    try:
        html_record = _parse_abs_html(
            _http_get_text(abs_url, timeout_seconds=timeout_seconds)
        )
    except ArxivMetadataError as exc:
        fetch_errors.append(str(exc))

    if atom_record is None and html_record is None:
        error_suffix = f":{'|'.join(fetch_errors)}" if fetch_errors else ""
        raise ArxivMetadataError(f"arxiv_metadata_unavailable{error_suffix}")

    merged = _merge_metadata_records(
        requested_id=requested_id,
        atom_record=atom_record,
        html_record=html_record,
    )
    if not merged.get("title") or not merged.get("authors"):
        raise ArxivMetadataError("arxiv_metadata_incomplete")
    return merged


__all__ = [
    "ARXIV_METADATA_SCHEMA_VERSION",
    "ArxivMetadataError",
    "fetch_arxiv_metadata",
]
