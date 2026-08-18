from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from bson import ObjectId

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import (
    filter_accessible_concept_ids,
    get_effective_user_concept_id,
)
from ..vontology.code_concepts_registry import is_code_concept_id
from ..vontology.utils_vontology import get_vontology_node_and_descendant_ids
from .concept_search_service import _search_text_relations
from .ontology_publication_authority_service import (
    PublicationContextKind,
    concept_publication_context,
)
from .text_value_service import get_texts_for_concept, get_texts_for_concepts

_CODE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_PERSON_TYPE_ID = "#V#person"


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


def _person_comma_order_variant(text: str) -> Optional[str]:
    """Return the exact natural-order lookup form of ``family, given ...``.

    The transformation is intentionally narrow: it accepts exactly one comma
    with non-empty text on both sides.  It is used only when resolution is
    explicitly restricted to ``#V#person``; the supplied source name remains
    unchanged.
    """

    if text.count(",") != 1:
        return None
    family, given = (_collapse_whitespace(part) for part in text.split(",", 1))
    if not family or not given:
        return None
    if not _alnum_tokens(family) or not _alnum_tokens(given):
        return None
    return f"{given} {family}"


def _uses_person_name_ordering(instance_of: Optional[str]) -> bool:
    return (
        isinstance(instance_of, str)
        and instance_of.strip().casefold() == _PERSON_TYPE_ID.casefold()
    )


def _accessible_candidate_ids(candidate_ids: Sequence[str] | set[str]) -> set[str]:
    """Discard name-index hits whose subject concepts are not actor-visible."""

    if not candidate_ids:
        return set()
    return filter_accessible_concept_ids(candidate_ids)


def _actor_private_candidate_ids(
    candidate_ids: Sequence[str] | set[str],
) -> set[str]:
    """Keep only concepts published solely to the trusted current actor."""

    actor_id = get_effective_user_concept_id()
    if not actor_id or not candidate_ids:
        return set()

    private_ids: set[str] = set()
    for concept_id in sorted(set(candidate_ids)):
        try:
            publication_context = concept_publication_context(concept_id)
        except (LookupError, ValueError):
            continue
        if (
            publication_context.kind == PublicationContextKind.USER
            and publication_context.concept_id == actor_id
        ):
            private_ids.add(concept_id)
    return private_ids


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
    require_actor_private: bool = False,
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
    preferred = [
        str(language)
        for language in (preferred_languages or [])
        if str(language).strip()
    ]
    allowed = [
        str(language) for language in (allowed_languages or []) if str(language).strip()
    ]
    allowed_set = set(allowed) if allowed else None

    # Stage 0: direct concept-id/code identifier resolution.
    if match_code_strings:
        maybe_id = _slug_to_concept_id(raw)
        if maybe_id:
            audit.append({"stage": "code_string", "candidate": maybe_id})
            doc = ConceptsRepository.find_one({"concept_id": maybe_id}, {"_id": 1})
            actor_private_ids = (
                _actor_private_candidate_ids({maybe_id})
                if require_actor_private
                else {maybe_id}
            )
            if (doc or is_code_concept_id(maybe_id)) and maybe_id in actor_private_ids:
                return {
                    "success": True,
                    "status": "resolved",
                    "resolved_concept_id": maybe_id,
                    "match": {"stage": "code_string", "score": 1000},
                    "candidates": [],
                    "audit": audit,
                }

    person_order_variant = (
        _person_comma_order_variant(raw)
        if _uses_person_name_ordering(instance_of)
        else None
    )
    query_variants: list[tuple[str, str]] = [(raw, "raw")]
    if person_order_variant and person_order_variant != raw:
        query_variants.append((person_order_variant, "person_comma_order"))
    for query_text, variant in list(query_variants):
        stripped = _strip_diacritics(query_text)
        if stripped != query_text and all(
            existing_text != stripped for existing_text, _ in query_variants
        ):
            query_variants.append((stripped, f"{variant}_diacritics_stripped"))

    candidate_ids: set[str] = set()

    # Stage 1: exact name match via text relations.
    for query_text, variant in query_variants:
        hits = _accessible_candidate_ids(
            _search_text_relations(
                query_text,
                exact=True,
                result_limit=max_results,
                allow_fallback_scan=False,
            )
        )
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
        hits = _accessible_candidate_ids(
            _search_text_relations(
                raw,
                prefix=True,
                result_limit=max_results,
            )
        )
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
        hits = _accessible_candidate_ids(
            _search_text_relations(raw, result_limit=max_results)
        )
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

            accessible_diacritic_hits = _accessible_candidate_ids(diacritic_hits)
            audit.append(
                {
                    "stage": "candidate_generation",
                    "method": "text_relations_diacritic_scan",
                    "query": raw,
                    "hits": len(accessible_diacritic_hits),
                    "relation_scan_cap": 10000,
                }
            )
            candidate_ids.update(accessible_diacritic_hits)

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

    if require_actor_private:
        actor_private_ids = _actor_private_candidate_ids(candidate_ids)
        audit.append(
            {
                "stage": "filter",
                "method": "actor_private",
                "before": len(candidate_ids),
                "after": len(actor_private_ids),
            }
        )
        candidate_ids = actor_private_ids

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
    person_order_casefold = (
        person_order_variant.casefold() if person_order_variant else None
    )
    person_order_casefold_stripped = (
        _strip_diacritics(person_order_casefold) if person_order_casefold else None
    )

    preferred_rank: dict[str, int] = {
        lang: (len(preferred) - idx) for idx, lang in enumerate(preferred)
    }

    matches: list[_CandidateMatch] = []
    ordered_candidate_ids = sorted(candidate_ids)
    name_query_metadata: dict[str, Any] = {}
    names_by_concept_id = get_texts_for_concepts(
        ordered_candidate_ids,
        predicate="hasName",
        limit_per_concept=200,
        query_metadata=name_query_metadata,
    )
    if name_query_metadata.get("relation_query_truncated") is True:
        names_by_concept_id = {
            concept_id: get_texts_for_concept(
                concept_id,
                predicate="hasName",
                limit=200,
            )
            for concept_id in ordered_candidate_ids
        }

    for concept_id in ordered_candidate_ids:
        names = names_by_concept_id.get(concept_id, [])

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
            elif person_order_casefold and (
                candidate_cf == person_order_casefold
                or candidate_cf_stripped == person_order_casefold_stripped
            ):
                stage = "person_comma_order_exact"
                score = 370
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
