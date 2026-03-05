from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from . import concept_search_service
from .relationship_write_service import add_relationship
from .text_value_service import upsert_text_for_concept

_URL_PATTERN = re.compile(r"\bhttps?://[^\s<>()\"']+", re.IGNORECASE)
_COMPANY_LABEL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:company|organisation|organization|business|startup|firm|name)\s*[:\-]\s*([^\n]{2,200})",
    re.IGNORECASE,
)
_ABOUT_NAME_PATTERN = re.compile(
    r"\babout\s+([A-Z][A-Za-z0-9&'`.,\- ]{2,100})",
    re.IGNORECASE,
)
_WEB_FILENAME_HINT_PATTERN = re.compile(
    r"\b(web[\s\-_]*page|website|about[\s\-_]*us|company[\s\-_]*profile|home[\s\-_]*page)\b",
    re.IGNORECASE,
)
_COMPANY_TEXT_HINT_PATTERN = re.compile(
    r"\b(company|organisation|organization|business|startup|our\s+mission|about\s+us|services)\b",
    re.IGNORECASE,
)
_COMPANY_SUFFIX_HINT_PATTERN = re.compile(
    r"\b(inc|inc\.|ltd|limited|llc|plc|corp|corp\.|corporation|company|co\.|group|labs?|systems?|technologies?)\b",
    re.IGNORECASE,
)
_GENERIC_HEADER_PATTERN = re.compile(
    r"^(about\s+us|home|contact|privacy\s+policy|terms|blog|careers|news)$",
    re.IGNORECASE,
)
_NON_NAME_HINT_PATTERN = re.compile(
    r"\b(email|phone|mobile|address|website|www\.|http|privacy|terms)\b",
    re.IGNORECASE,
)
_WHITESPACE_SEPARATOR_PATTERN = re.compile(r"[\s._\-]+")


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _dedupe_casefold(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _normalise_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n;|")


def _extract_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        normalised = _normalise_line(raw_line)
        if not normalised:
            continue
        lines.append(normalised)
    return lines


def _extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        value = _normalise_line(match.group(0))
        if value:
            urls.append(value)
    return _dedupe_casefold(urls)


def _host_to_company_candidate(host: str) -> str | None:
    cleaned_host = host.strip().lower().rstrip(".")
    if not cleaned_host:
        return None
    if cleaned_host.startswith("www."):
        cleaned_host = cleaned_host[4:]
    parts = [part for part in cleaned_host.split(".") if part]
    if not parts:
        return None

    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"}:
        label = parts[-3]
    elif len(parts) >= 2:
        label = parts[-2]
    else:
        label = parts[0]

    tokens = [token for token in _WHITESPACE_SEPARATOR_PATTERN.split(label) if token]
    if not tokens:
        return None
    candidate = " ".join(token.capitalize() for token in tokens)
    return candidate.strip() or None


def _looks_like_company_name(candidate: str) -> bool:
    text = _normalise_line(candidate)
    if not text:
        return False
    if len(text) < 2 or len(text) > 140:
        return False
    if _GENERIC_HEADER_PATTERN.fullmatch(text):
        return False
    if _NON_NAME_HINT_PATTERN.search(text):
        return False
    if text.count("http://") or text.count("https://"):
        return False
    if sum(char.isalpha() for char in text) < 2:
        return False

    tokens = [token for token in _WHITESPACE_SEPARATOR_PATTERN.split(text) if token]
    if len(tokens) < 1 or len(tokens) > 8:
        return False
    if not _COMPANY_SUFFIX_HINT_PATTERN.search(text) and len(tokens) == 1 and len(tokens[0]) < 4:
        return False
    return True


def _extract_candidate_company_names(
    *,
    text: str,
    lines: list[str],
    urls: list[str],
) -> list[str]:
    candidates: list[str] = []

    for match in _COMPANY_LABEL_PATTERN.finditer(text):
        candidate = _normalise_line(match.group(1))
        if _looks_like_company_name(candidate):
            candidates.append(candidate)

    for match in _ABOUT_NAME_PATTERN.finditer(text):
        candidate = _normalise_line(match.group(1))
        if _looks_like_company_name(candidate):
            candidates.append(candidate)

    for line in lines[:12]:
        if _looks_like_company_name(line) and (
            _COMPANY_SUFFIX_HINT_PATTERN.search(line) or line[0].isupper()
        ):
            candidates.append(line)

    for url in urls:
        parsed = urlparse(url)
        host = _safe_str(parsed.netloc)
        if not host:
            continue
        fallback = _host_to_company_candidate(host)
        if fallback and _looks_like_company_name(fallback):
            candidates.append(fallback)

    return _dedupe_casefold(candidates)


def _extract_descriptor_lines(lines: list[str]) -> list[str]:
    descriptors: list[str] = []
    descriptor_hint_pattern = re.compile(
        r"\b(mission|vision|services|products|founded|headquarters|industry|platform|solutions)\b",
        re.IGNORECASE,
    )
    for line in lines:
        if _URL_PATTERN.search(line):
            continue
        if descriptor_hint_pattern.search(line):
            descriptors.append(line)
    return _dedupe_casefold(descriptors)[:4]


def _infer_representation_mode(
    *,
    original_filename: str | None,
    text: str,
    urls: list[str],
) -> str | None:
    filename = (original_filename or "").strip().lower()
    if filename and _WEB_FILENAME_HINT_PATTERN.search(filename):
        return "web_page"
    if urls and _COMPANY_TEXT_HINT_PATTERN.search(text):
        return "web_page"
    return None


def _stable_named_instance_concept_id(name: str, *, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not slug:
        slug = "unnamed"
    digest = hashlib.sha256(str(name or "").strip().casefold().encode("utf-8")).hexdigest()[
        :8
    ]
    return f"#V#{prefix}_{slug}_{digest}"


def _resolve_or_create_company_concept_id(
    *,
    user_concept_id: str,
    company_name: str,
    logger: Any | None = None,
) -> tuple[str, bool]:
    from . import concept_service

    for instance_type in ("#V#company", "#V#organization"):
        search_result = concept_search_service.search_concepts(
            query=company_name,
            instance_of=instance_type,
            match_type="exact",
            limit=10,
        )
        for item in search_result.get("results") or []:
            if not isinstance(item, Mapping):
                continue
            candidate_id = _safe_str(item.get("concept_id"))
            candidate_name = _safe_str(item.get("name"))
            if not candidate_id:
                continue
            if candidate_name and candidate_name.casefold() == company_name.casefold():
                return candidate_id, False

    concept_id = _stable_named_instance_concept_id(company_name, prefix="company")
    try:
        concept_service.create_concept(
            name=company_name,
            concept_id=concept_id,
            parent_concept_ids=["#V#company"],
            create_as_instance=True,
            system_tags=["company", "representation", "file_copy"],
        )
        concept_service.update_concept(
            concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[company_file_representation] Company concept create failed for %s: %s",
                company_name,
                exc,
            )
    return concept_id, True


def _write_text_relation(
    *,
    subject_concept_id: str,
    predicate: str,
    text: str,
    context: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        relation = upsert_text_for_concept(
            subject_concept_id=subject_concept_id,
            predicate=predicate,
            text=text,
            lang="en-NZ",
            context=dict(context or {}),
        )
        return dict(relation) if isinstance(relation, Mapping) else None, None
    except Exception as exc:
        return None, str(exc)


def materialise_company_representation_for_file_copy(
    *,
    user_concept_id: str | None,
    file_copy_concept_id: str,
    extracted_text: str | None,
    original_filename: str | None = None,
    interpretation: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Deterministically materialise a company representation from webpage artefacts."""

    report: dict[str, Any] = {
        "success": False,
        "attempted": False,
        "verified": False,
        "reason": "not_company_artefact",
        "representation_mode": None,
        "file_copy_concept_id": file_copy_concept_id,
        "company_concept_id": None,
        "company_name": None,
        "aliases": [],
        "urls": [],
        "descriptors": [],
        "created_company_concept": False,
        "persisted_text_relations": [],
        "persisted_structural_relations": [],
        "errors": [],
    }

    text = _safe_str(extracted_text)
    if not text:
        report["reason"] = "no_extracted_text"
        return report

    lines = _extract_lines(text)
    urls = _extract_urls(text)
    if isinstance(interpretation, Mapping):
        for key in ("source_url", "url", "canonical_url"):
            value = _safe_str(interpretation.get(key))
            if value:
                urls.append(value)
    urls = _dedupe_casefold(urls)[:4]
    report["urls"] = list(urls)

    representation_mode = _infer_representation_mode(
        original_filename=original_filename,
        text=text,
        urls=urls,
    )
    if representation_mode is None:
        return report

    report["attempted"] = True
    report["representation_mode"] = representation_mode

    actor_id = _safe_str(user_concept_id)
    if not actor_id:
        report["reason"] = "missing_user_context_for_company_representation"
        return report

    company_names = _extract_candidate_company_names(text=text, lines=lines, urls=urls)
    company_name = company_names[0] if company_names else None
    aliases = company_names[1:4]
    descriptors = _extract_descriptor_lines(lines)

    report["company_name"] = company_name
    report["aliases"] = list(aliases)
    report["descriptors"] = list(descriptors)

    if not company_name:
        report["reason"] = "company_identity_unresolved"
        return report
    if not urls:
        report["reason"] = "company_url_unresolved"
        return report

    company_concept_id, created_company = _resolve_or_create_company_concept_id(
        user_concept_id=actor_id,
        company_name=company_name,
        logger=logger,
    )
    report["company_concept_id"] = company_concept_id
    report["created_company_concept"] = created_company

    relation_specs: list[tuple[str, str, Mapping[str, Any]]] = [
        (
            "hasName",
            company_name,
            {
                "name_type": "NL",
                "source": "company_file_representation",
                "source_file_copy_concept_id": file_copy_concept_id,
            },
        )
    ]
    for alias in aliases:
        relation_specs.append(
            (
                "hasName",
                alias,
                {
                    "name_type": "NL",
                    "is_alias": True,
                    "source": "company_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for url in urls:
        relation_specs.append(
            (
                "#V#has_url",
                url,
                {
                    "source": "company_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )
    for descriptor in descriptors:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Descriptor: {descriptor}",
                {
                    "note_type": "company_descriptor",
                    "source": "company_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )

    interpretation_description = (
        _safe_str(interpretation.get("description"))
        if isinstance(interpretation, Mapping)
        else None
    )
    if interpretation_description:
        relation_specs.append(
            (
                "#V#hasNote",
                f"Interpretation summary: {interpretation_description}",
                {
                    "note_type": "interpretation_summary",
                    "source": "company_file_representation",
                    "source_file_copy_concept_id": file_copy_concept_id,
                },
            )
        )

    relation_specs.append(
        (
            "#V#hasNote",
            f"Source file copy: {file_copy_concept_id}",
            {
                "note_type": "source_linkage",
                "source": "company_file_representation",
                "source_file_copy_concept_id": file_copy_concept_id,
            },
        )
    )

    name_persisted = False
    url_persisted = False
    for predicate, value, context in relation_specs:
        relation, error = _write_text_relation(
            subject_concept_id=company_concept_id,
            predicate=predicate,
            text=value,
            context=context,
        )
        if error:
            report["errors"].append(
                {
                    "stage": "text_relation_upsert",
                    "predicate": predicate,
                    "text": value,
                    "error": error,
                }
            )
            continue
        if predicate == "hasName" and value.casefold() == company_name.casefold():
            name_persisted = True
        if predicate == "#V#has_url":
            url_persisted = True
        report["persisted_text_relations"].append(
            {
                "predicate": predicate,
                "relation_id": relation.get("relation_id") if isinstance(relation, Mapping) else None,
            }
        )

    evidence_relation = add_relationship(
        source_id=file_copy_concept_id,
        predicate="#V#documentary_evidence_for",
        target=company_concept_id,
    )
    if isinstance(evidence_relation, Mapping) and evidence_relation.get("success") is True:
        report["persisted_structural_relations"].append(
            {
                "predicate": "#V#documentary_evidence_for",
                "source_id": file_copy_concept_id,
                "target_id": company_concept_id,
                "modified": bool(evidence_relation.get("forward_modified")),
            }
        )
        evidence_linked = True
    else:
        evidence_linked = False
        report["errors"].append(
            {
                "stage": "source_evidence_link",
                "predicate": "#V#documentary_evidence_for",
                "source_id": file_copy_concept_id,
                "target_id": company_concept_id,
                "error": (
                    evidence_relation.get("error")
                    if isinstance(evidence_relation, Mapping)
                    else "unexpected_relationship_response"
                ),
                "details": evidence_relation,
            }
        )

    if name_persisted and url_persisted and evidence_linked:
        report["verified"] = True
        report["success"] = True
        report["reason"] = "company_representation_verified"
    elif not name_persisted:
        report["reason"] = "company_name_persist_failed"
    elif not url_persisted:
        report["reason"] = "company_url_persist_failed"
    else:
        report["reason"] = "source_linkage_failed"
    return report
