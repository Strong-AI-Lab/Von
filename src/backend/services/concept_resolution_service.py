from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bson import ObjectId

from .concept_search_service import _search_text_relations
from .text_value_service import get_texts_for_concept
from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..vontology.code_concepts_registry import is_code_concept_id
from ..vontology.utils_vontology import get_vontology_node_and_descendant_ids


_CODE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _strip_diacritics(text: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def _alnum_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", text) if t]


def _person_signature(tokens: Sequence[str]) -> Optional[Tuple[str, str, str]]:
    if len(tokens) < 2:
        return None
    first = tokens[0]
    last = tokens[-1]
    middle_initials = "".join(t[0] for t in tokens[1:-1] if t)
    return (first, last, middle_initials)


def _slug_to_concept_id(value: str) -> Optional[str]:
    raw = value.strip()
    if not raw or " " in raw:
        return None
    if raw.startswith("#V#"):
        return raw
    if not _CODE_IDENTIFIER_RE.match(raw):
        return None
    return f"#V#{raw}"


@dataclass(frozen=True)
class _CandidateMatch:
    concept_id: str
    score: int
    stage: str
    matched_name: str
    language: Optional[str]
    name_type: Optional[str]


def resolve_concept_by_name(
    *,
    name: str,
    preferred_languages: Optional[Sequence[str]] = None,
    allowed_languages: Optional[Sequence[str]] = None,
    instance_of: Optional[str] = None,
    match_code_strings: bool = True,
    normalisation_level: str = "default",
    max_results: int = 5,
) -> Dict[str, Any]:
    """Resolve a Vontology concept deterministically from a user-provided surface form.

    This is a read-only helper intended for MCP tooling. It does not mutate the Vontology.

    Returns a dict with:
    - success: bool
    - status: 'resolved' | 'ambiguous' | 'not_found'
    - resolved_concept_id: str | None
    - candidates: list (for ambiguous)
    - audit: list of steps
    """

    raw = _collapse_whitespace(name)
    if not raw:
        return {
            "success": False,
            "status": "not_found",
            "error": "Missing 'name' parameter",
            "resolved_concept_id": None,
            "candidates": [],
            "audit": [],
        }

    audit: list[dict[str, Any]] = []
    preferred = [str(l) for l in (preferred_languages or []) if str(l).strip()]
    allowed = [str(l) for l in (allowed_languages or []) if str(l).strip()]
    allowed_set = set(allowed) if allowed else None

    # Stage 0: direct concept-id/code identifier resolution.
    if match_code_strings:
        maybe_id = _slug_to_concept_id(raw)
        if maybe_id:
            audit.append({"stage": "code_string", "candidate": maybe_id})
            doc = ConceptsRepository.find_one({"concept_id": maybe_id}, {"_id": 1})
            if doc or is_code_concept_id(maybe_id):
                return {
                    "success": True,
                    "status": "resolved",
                    "resolved_concept_id": maybe_id,
                    "match": {"stage": "code_string", "score": 1000},
                    "candidates": [],
                    "audit": audit,
                }

    query_variants: list[tuple[str, str]] = [(raw, "raw")]
    stripped = _strip_diacritics(raw)
    if stripped != raw:
        query_variants.append((stripped, "diacritics_stripped"))

    candidate_ids: set[str] = set()

    # Stage 1: exact name match via text relations.
    for query_text, variant in query_variants:
        hits = _search_text_relations(query_text, exact=True)
        if hits:
            audit.append(
                {
                    "stage": "candidate_generation",
                    "method": "text_relations_exact",
                    "variant": variant,
                    "query": query_text,
                    "hits": len(hits),
                }
            )
            candidate_ids.update(hits)

    # Stage 2+: broaden if nothing found.
    if not candidate_ids:
        hits = _search_text_relations(raw, prefix=True)
        audit.append(
            {
                "stage": "candidate_generation",
                "method": "text_relations_prefix",
                "query": raw,
                "hits": len(hits),
            }
        )
        candidate_ids.update(hits)

    if not candidate_ids:
        hits = _search_text_relations(raw)
        audit.append(
            {
                "stage": "candidate_generation",
                "method": "text_relations_substring",
                "query": raw,
                "hits": len(hits),
            }
        )
        candidate_ids.update(hits)

    # Stage 3b: diacritic-insensitive fallback.
    # If the user types ASCII (e.g. 'Gael') but the stored name contains diacritics
    # (e.g. 'Gaël'), Mongo text/regex matching may not find it. Do a bounded scan of
    # hasName text relations and compare with diacritics stripped.
    if not candidate_ids:
        query_cf_stripped = _strip_diacritics(raw.casefold())
        if query_cf_stripped:
            relations = list(
                TextRelationsRepository.find(
                    {"predicate": "hasName"},
                    limit=10000,
                )
            )

            # Map text_value_id -> subject_concept_ids
            tv_to_subjects: dict[str, set[str]] = {}
            tv_object_ids: list[ObjectId] = []
            for rel in relations:
                subj = rel.get("subject_concept_id")
                obj = rel.get("object_text_id")
                if not isinstance(subj, str) or not subj:
                    continue
                obj_str = str(obj) if obj is not None else ""
                if not obj_str:
                    continue
                tv_to_subjects.setdefault(obj_str, set()).add(subj)
                if ObjectId.is_valid(obj_str):
                    tv_object_ids.append(ObjectId(obj_str))

            if tv_object_ids:
                text_value_docs = list(
                    TextValuesRepository.find(
                        {"_id": {"$in": tv_object_ids}},
                        limit=5000,
                    )
                )
            else:
                text_value_docs = []

            diacritic_hits: set[str] = set()
            for tv in text_value_docs:
                tv_id = str(tv.get("_id"))
                text = tv.get("text")
                if not isinstance(text, str) or not text:
                    continue
                if _strip_diacritics(text.casefold()) == query_cf_stripped:
                    diacritic_hits.update(tv_to_subjects.get(tv_id, set()))

            audit.append(
                {
                    "stage": "candidate_generation",
                    "method": "text_relations_diacritic_scan",
                    "query": raw,
                    "hits": len(diacritic_hits),
                    "relation_scan_cap": 10000,
                }
            )
            candidate_ids.update(diacritic_hits)

    # Deterministic cap to avoid pathological scans.
    candidate_pool_cap = max(50, min(500, max_results * 50))
    if len(candidate_ids) > candidate_pool_cap:
        candidate_ids = set(sorted(candidate_ids)[:candidate_pool_cap])
        audit.append(
            {
                "stage": "candidate_generation",
                "method": "cap",
                "cap": candidate_pool_cap,
                "note": "Deterministic cap applied to candidate pool",
            }
        )

    if not candidate_ids:
        return {
            "success": True,
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
            "audit": audit,
        }

    # Filter out stale text relations pointing at non-existent concepts.
    # This can happen if a concept is deleted but its text_relations (e.g. hasName) remain.
    # Also keep virtual/code concepts, which intentionally have no DB row.
    existing: set[str] = set()
    try:
        cursor = ConceptsRepository.find(
            {"concept_id": {"$in": list(candidate_ids)}},
            {"concept_id": 1},
        )
        for doc in cursor:
            cid = doc.get("concept_id")
            if isinstance(cid, str) and cid:
                existing.add(cid)
    except Exception as exc:  # pragma: no cover
        audit.append(
            {
                "stage": "filter",
                "method": "existing_concepts_error",
                "error": str(exc),
            }
        )

    existing.update({cid for cid in candidate_ids if is_code_concept_id(cid)})

    if len(existing) != len(candidate_ids):
        audit.append(
            {
                "stage": "filter",
                "method": "existing_concepts",
                "before": len(candidate_ids),
                "after": len(existing),
            }
        )
        candidate_ids = existing

    # Optional instance_of filter (recursive, includes descendants).
    if instance_of:
        try:
            descendant_ids = get_vontology_node_and_descendant_ids(instance_of)
        except Exception as exc:
            return {
                "success": False,
                "status": "not_found",
                "error": f"Failed to expand instance_of descendants: {exc}",
                "resolved_concept_id": None,
                "candidates": [],
                "audit": audit,
            }

        # Be robust to incomplete type-hierarchy data: if descendant expansion fails to
        # locate the start node, still treat instance_of as a valid direct filter.
        if not descendant_ids:
            descendant_ids = [instance_of]
            audit.append(
                {
                    "stage": "filter",
                    "method": "instance_of_descendants_fallback",
                    "instance_of": instance_of,
                    "descendants": 1,
                }
            )

        descendant_set = {str(cid) for cid in descendant_ids if cid}

        filtered: set[str] = set()
        # Filter in Python rather than relying on nested $in queries. This is robust
        # across Mongo backends and avoids subtle driver/mongomock incompatibilities.
        cursor = ConceptsRepository.find(
            {"concept_id": {"$in": list(candidate_ids)}},
            {"concept_id": 1, "relationships": 1},
        )
        for doc in cursor:
            cid = doc.get("concept_id")
            rels = doc.get("relationships") or {}
            raw_instances = (
                rels.get("is_an_instance_of") if isinstance(rels, dict) else None
            )

            if isinstance(raw_instances, str):
                instance_ids = [raw_instances]
            elif isinstance(raw_instances, list):
                instance_ids = [x for x in raw_instances if isinstance(x, str)]
            else:
                instance_ids = []

            if cid and any(inst in descendant_set for inst in instance_ids):
                filtered.add(cid)

        audit.append(
            {
                "stage": "filter",
                "method": "instance_of",
                "instance_of": instance_of,
                "before": len(candidate_ids),
                "after": len(filtered),
            }
        )
        candidate_ids = filtered

    if not candidate_ids:
        return {
            "success": True,
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
            "audit": audit,
        }

    query_casefold = raw.casefold()
    query_casefold_stripped = _strip_diacritics(query_casefold)
    query_tokens = [t.casefold() for t in _alnum_tokens(raw)]
    query_signature = _person_signature(query_tokens)

    preferred_rank: dict[str, int] = {
        lang: (len(preferred) - idx) for idx, lang in enumerate(preferred)
    }

    matches: list[_CandidateMatch] = []

    for concept_id in sorted(candidate_ids):
        names = get_texts_for_concept(
            concept_id,
            predicate="hasName",
            limit=200,
        )

        best: Optional[_CandidateMatch] = None
        for name_doc in names:
            candidate_text = name_doc.get("text")
            if not candidate_text:
                continue

            lang = name_doc.get("lang")
            if allowed_set is not None and lang not in allowed_set:
                continue

            context = name_doc.get("context") or {}
            name_type = context.get("name_type") if isinstance(context, dict) else None

            candidate_norm = _collapse_whitespace(str(candidate_text))
            candidate_cf = candidate_norm.casefold()
            candidate_cf_stripped = _strip_diacritics(candidate_cf)

            stage = None
            score = 0

            if candidate_norm == raw:
                stage = "exact"
                score = 400
            elif candidate_cf == query_casefold:
                stage = "casefold_exact"
                score = 390
            elif candidate_cf_stripped == query_casefold_stripped:
                stage = "diacritic_insensitive"
                score = 380
            else:
                candidate_tokens = [t.casefold() for t in _alnum_tokens(candidate_norm)]
                if candidate_tokens == query_tokens and candidate_tokens:
                    stage = "token_exact"
                    score = 360
                else:
                    cand_signature = _person_signature(candidate_tokens)
                    if (
                        query_signature
                        and cand_signature
                        and cand_signature == query_signature
                    ):
                        stage = "person_signature"
                        score = 340

            if stage is None or score <= 0:
                continue

            if lang and lang in preferred_rank:
                score += preferred_rank[lang]

            candidate_match = _CandidateMatch(
                concept_id=concept_id,
                score=score,
                stage=stage,
                matched_name=candidate_norm,
                language=lang,
                name_type=name_type,
            )

            if best is None or candidate_match.score > best.score:
                best = candidate_match
            elif (
                candidate_match.score == best.score
                and candidate_match.matched_name < best.matched_name
            ):
                # Deterministic tiebreak within a concept.
                best = candidate_match

        if best is not None:
            matches.append(best)

    if not matches:
        return {
            "success": True,
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
            "audit": audit,
        }

    matches.sort(key=lambda m: (-m.score, m.concept_id))

    top_score = matches[0].score
    top = [m for m in matches if m.score == top_score]

    if len(top) == 1:
        winner = matches[0]
        return {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": winner.concept_id,
            "match": {
                "score": winner.score,
                "stage": winner.stage,
                "matched_name": winner.matched_name,
                "language": winner.language,
                "name_type": winner.name_type,
            },
            "candidates": [],
            "audit": audit,
        }

    # Ambiguous: return a deterministic top-N of the tied results.
    tied = sorted(top, key=lambda m: (m.concept_id, m.matched_name))
    return {
        "success": True,
        "status": "ambiguous",
        "resolved_concept_id": None,
        "candidates": [
            {
                "concept_id": m.concept_id,
                "score": m.score,
                "stage": m.stage,
                "matched_name": m.matched_name,
                "language": m.language,
                "name_type": m.name_type,
            }
            for m in tied[:max_results]
        ],
        "audit": audit,
    }
