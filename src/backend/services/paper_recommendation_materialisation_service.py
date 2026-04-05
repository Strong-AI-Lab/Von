"""Semantic paper recommendation materialisation for generic subject concepts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from math import sqrt
import time
from typing import Any, Mapping, Sequence

from ..languagemodels.llm_interface import get_llm_client
from .concept_embedding_service import build_concept_searchable_text
from .concept_similarity_service import build_text_embedding
from .concept_search_service import search_concepts
from .concept_service import get_concept_by_concept_id_exact
from .paper_recommendation_constants import (
    DEFAULT_CANDIDATE_RECALL_LIMIT,
    DEFAULT_LLM_CANDIDATE_LIMIT,
    DEFAULT_MAX_RESULTS,
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    MAX_CANDIDATE_RECALL_LIMIT,
    MAX_MAX_RESULTS,
    MIN_RECOMMENDATION_SCORE,
    PAPER_RECOMMENDATION_POLICY_VERSION,
    PAPER_RECOMMENDATION_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
    SCHOLARLY_ARTICLE_TYPE_ID,
    SUMMARY_EXCERPT_CHARS,
)
from .paper_recommendation_vontology_service import (
    list_subject_concept_ids_with_paper_matching_profiles,
    load_materialised_paper_recommendations,
    load_subject_paper_matching_profile,
    resolve_subject_ids_for_legacy_profile_concept,
    upsert_paper_recommendation_assertion,
)
from .paper_recommendation_delivery_service import (
    list_paper_recommendation_delivery_subject_ids,
)
from .rag_backends.llamaindex_backend import LlamaIndexRAGService
from .text_value_service import get_texts_for_concept
from .workflow_event_integration_service import (
    EVENT_TYPE_RELATIONSHIP_ADDED,
    EVENT_TYPE_RELATIONSHIP_REMOVED,
    EVENT_TYPE_TEXT_RELATION_UPDATED,
    EVENT_TYPE_TEXT_RELATION_UPSERTED,
)
from .workflow_prompt_authority_service import (
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
)

logger = logging.getLogger(__name__)

_PAPER_TEXT_PREDICATES: dict[str, tuple[str, ...]] = {
    "summary": ("hasDescription", "hasContent"),
    "publication_date": ("#V#has_publication_date",),
    "topic_labels": ("#V#has_topic_labels",),
}
_SUBJECT_PROMPT_MAX_CHARS = 12000
_STRUCTURAL_REL_KEYS = {
    "is_a_type_of",
    "has_subtype",
    "is_an_instance_of",
    "has_instance",
    "linked_to",
}
_VISIBILITY_REL_KEYS = {
    "#V#specific_to_user",
    "#V#specific_to_organisation",
}
_LEGACY_PROFILE_JSON_PREDICATE_ID = "#V#has_paper_recommendation_profile_json"
_AFFECTING_SUBJECT_RELATIONSHIP_PREDICATES = {
    "#V#has_research_interest",
    "#V#member_of_organisation",
    "#V#has_project",
    "#V#working_on_project",
}
_AFFECTING_PAPER_TEXT_PREDICATES = {
    "hasName",
    "hasDescription",
    "hasContent",
    "#V#has_topic_labels",
    "#V#has_publication_date",
}
_EMBEDDING_ONLY_ACTIVE_LIMIT = 3
_EMBEDDING_BACKEND_FAILURE_BACKOFF_SECONDS = 300.0
_embedding_backend_backoff_until = 0.0
_embedding_backend_backoff_reason: str | None = None


@dataclass(slots=True)
class _LookupCache:
    concept_docs: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    first_texts: dict[tuple[str, str], str | None] = field(default_factory=dict)
    paper_bundles: dict[str, dict[str, Any]] = field(default_factory=dict)


def _safe_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalise_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(resolved, maximum))


def _normalise_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _safe_str(item)
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        rows.append(cleaned)
    return rows


def _normalise_candidate_ids(candidate_paper_concept_ids: Sequence[str] | None) -> list[str]:
    if not isinstance(candidate_paper_concept_ids, Sequence) or isinstance(
        candidate_paper_concept_ids, (str, bytes, bytearray)
    ):
        return []
    rows: list[str] = []
    seen: set[str] = set()
    for item in candidate_paper_concept_ids:
        cleaned = _safe_str(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        rows.append(cleaned)
    return rows


def _normalise_subject_ids(raw_subject_ids: Any) -> list[str]:
    if isinstance(raw_subject_ids, str):
        return [_safe_str(raw_subject_ids)] if _safe_str(raw_subject_ids) else []
    return _normalise_candidate_ids(raw_subject_ids)


def _get_concept(
    concept_id: str,
    *,
    lookup_cache: _LookupCache | None = None,
) -> dict[str, Any] | None:
    concept_id = _safe_str(concept_id)
    if not concept_id:
        return None
    if lookup_cache is not None and concept_id in lookup_cache.concept_docs:
        return lookup_cache.concept_docs[concept_id]
    try:
        concept = get_concept_by_concept_id_exact(concept_id)
    except Exception:
        resolved = None
    else:
        resolved = dict(concept) if isinstance(concept, Mapping) else None
    if lookup_cache is not None:
        lookup_cache.concept_docs[concept_id] = resolved
    return resolved


def _is_scholarly_article(concept_doc: Mapping[str, Any] | None) -> bool:
    relationships = (
        concept_doc.get("relationships")
        if isinstance(concept_doc, Mapping)
        else None
    )
    if not isinstance(relationships, Mapping):
        return False
    raw = relationships.get("is_an_instance_of")
    if isinstance(raw, str):
        return raw == SCHOLARLY_ARTICLE_TYPE_ID
    if isinstance(raw, list):
        return SCHOLARLY_ARTICLE_TYPE_ID in raw
    return False


def _first_text(
    subject_concept_id: str,
    predicates: Sequence[str],
    *,
    lookup_cache: _LookupCache | None = None,
) -> str | None:
    for predicate in predicates:
        cache_key = (subject_concept_id, predicate)
        text: str | None
        if lookup_cache is not None and cache_key in lookup_cache.first_texts:
            text = lookup_cache.first_texts[cache_key]
        else:
            text = None
            rows = get_texts_for_concept(
                subject_concept_id=subject_concept_id,
                predicate=predicate,
                limit=8,
            )
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                text = _safe_str(row.get("text"))
                if text:
                    break
            if lookup_cache is not None:
                lookup_cache.first_texts[cache_key] = text
        if text:
            return text
    return None


def _relationship_rows(
    concept_doc: Mapping[str, Any],
    *,
    per_predicate_limit: int = 6,
    lookup_cache: _LookupCache | None = None,
) -> list[dict[str, str]]:
    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return []
    rows: list[dict[str, str]] = []
    for predicate, raw_targets in relationships.items():
        predicate_id = _safe_str(predicate)
        if not predicate_id or predicate_id in _STRUCTURAL_REL_KEYS:
            continue
        if predicate_id in _VISIBILITY_REL_KEYS:
            continue
        targets = raw_targets
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list):
            continue
        added = 0
        for target in targets:
            target_id = _safe_str(target)
            if not target_id:
                continue
            target_doc = _get_concept(target_id, lookup_cache=lookup_cache) or {}
            target_name = _safe_str(target_doc.get("name")) or target_id
            rows.append(
                {
                    "predicate": predicate_id,
                    "target_concept_id": target_id,
                    "target_name": target_name,
                }
            )
            added += 1
            if added >= per_predicate_limit:
                break
    return rows


def _build_subject_bundle(
    subject_concept_id: str,
    *,
    lookup_cache: _LookupCache | None = None,
) -> dict[str, Any]:
    profile_payload = load_subject_paper_matching_profile(subject_concept_id)
    if not profile_payload.get("success"):
        return {
            "success": False,
            "error": profile_payload.get("error") or "subject_profile_load_failed",
            "subject_concept_id": subject_concept_id,
        }

    subject_doc = dict(profile_payload.get("subject_doc") or {})
    subject_name = _safe_str(subject_doc.get("name")) or subject_concept_id
    related_rows = _relationship_rows(subject_doc, lookup_cache=lookup_cache)
    profile = dict(profile_payload.get("profile") or {})
    legacy_payload = profile_payload.get("legacy_profile_payload") or {}
    derived_context = {}
    if isinstance(legacy_payload, Mapping):
        derived_context = dict(legacy_payload.get("derived_context") or {})
    research_interest_rows = list(derived_context.get("research_interest_concepts") or [])
    organisation_ids = _normalise_string_list(
        derived_context.get("organisation_concept_ids")
    )

    fact_lines: list[str] = [
        f"Subject concept: {subject_name}",
        f"Concept ID: {subject_concept_id}",
    ]
    base_text = build_concept_searchable_text(subject_doc)
    if base_text:
        fact_lines.append(base_text)
    if related_rows:
        fact_lines.append(
            "\n".join(
                f"Relation {row['predicate']}: {row['target_name']} ({row['target_concept_id']})"
                for row in related_rows[:24]
            )
        )
    if research_interest_rows:
        interest_names = [
            _safe_str(item.get("name")) or _safe_str(item.get("concept_id"))
            for item in research_interest_rows
            if isinstance(item, Mapping)
        ]
        if interest_names:
            fact_lines.append("Research interests: " + ", ".join(interest_names))
    if organisation_ids:
        organisation_names: list[str] = []
        for organisation_id in organisation_ids[:8]:
            organisation_doc = _get_concept(
                organisation_id,
                lookup_cache=lookup_cache,
            ) or {}
            organisation_names.append(
                _safe_str(organisation_doc.get("name")) or organisation_id
            )
        if organisation_names:
            fact_lines.append("Organisations: " + ", ".join(organisation_names))
    if profile:
        profile_lines: list[str] = []
        for field, value in profile.items():
            if field in {"schema_version", "subject_concept_id", "updated_at"}:
                continue
            if isinstance(value, list) and value:
                profile_lines.append(
                    f"{field}: {', '.join(_normalise_string_list(value))}"
                )
            elif isinstance(value, str) and value.strip():
                profile_lines.append(f"{field}: {value.strip()}")
        if profile_lines:
            fact_lines.append("Profile overlay:\n" + "\n".join(profile_lines))

    return {
        "success": True,
        "subject_concept_id": subject_concept_id,
        "subject_name": subject_name,
        "profile": profile,
        "profile_present": bool(profile_payload.get("profile_present")),
        "profile_concept_id": profile_payload.get("profile_concept_id"),
        "profile_source_predicate": profile_payload.get("profile_source_predicate"),
        "research_interest_concepts": research_interest_rows,
        "organisation_concept_ids": organisation_ids,
        "related_concepts": related_rows,
        "matching_text": "\n\n".join(line for line in fact_lines if line),
    }


def _build_paper_bundle(
    paper_concept_id: str,
    *,
    lookup_cache: _LookupCache | None = None,
) -> dict[str, Any]:
    paper_id = _safe_str(paper_concept_id)
    if lookup_cache is not None and paper_id in lookup_cache.paper_bundles:
        return lookup_cache.paper_bundles[paper_id]
    paper_doc = _get_concept(paper_id, lookup_cache=lookup_cache)
    if paper_doc is None:
        bundle = {
            "paper_concept_id": paper_id,
            "representation_complete": False,
            "representation_failures": ["paper_not_found"],
        }
        if lookup_cache is not None:
            lookup_cache.paper_bundles[paper_id] = bundle
        return bundle

    paper_title = _safe_str(paper_doc.get("name")) or paper_id
    summary = (
        _first_text(
            paper_id,
            _PAPER_TEXT_PREDICATES["summary"],
            lookup_cache=lookup_cache,
        )
        or ""
    )
    publication_date = _first_text(
        paper_id,
        _PAPER_TEXT_PREDICATES["publication_date"],
        lookup_cache=lookup_cache,
    )
    topic_label_text = _first_text(
        paper_id,
        _PAPER_TEXT_PREDICATES["topic_labels"],
        lookup_cache=lookup_cache,
    )
    topic_labels = _normalise_string_list(topic_label_text)
    relationship_rows = _relationship_rows(paper_doc, lookup_cache=lookup_cache)
    author_names = [
        row["target_name"]
        for row in relationship_rows
        if row.get("predicate") == "#V#authored_by"
    ]
    if not topic_labels:
        topic_labels = [
            row["target_name"]
            for row in relationship_rows
            if row.get("predicate") == "#V#about"
        ]
    context_lines = [
        f"Paper title: {paper_title}",
        f"Paper concept ID: {paper_id}",
    ]
    base_text = build_concept_searchable_text(paper_doc)
    if base_text:
        context_lines.append(base_text)
    if summary and summary not in base_text:
        context_lines.append("Abstract or summary: " + summary)
    if author_names:
        context_lines.append("Authors: " + ", ".join(author_names))
    if topic_labels:
        context_lines.append("Topics: " + ", ".join(topic_labels))
    if publication_date:
        context_lines.append("Publication date: " + publication_date)

    return {
        "paper_concept_id": paper_id,
        "paper_title": paper_title,
        "paper_summary": summary,
        "summary_excerpt": summary[:SUMMARY_EXCERPT_CHARS] if summary else "",
        "author_names": author_names,
        "topic_labels": topic_labels,
        "publication_date": publication_date,
        "representation_complete": True,
        "representation_failures": [],
        "matching_text": "\n\n".join(line for line in context_lines if line),
    }
    if lookup_cache is not None:
        lookup_cache.paper_bundles[paper_id] = bundle
    return bundle


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right):
        dot += float(left_value) * float(right_value)
        left_norm += float(left_value) * float(left_value)
        right_norm += float(right_value) * float(right_value)
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (sqrt(left_norm) * sqrt(right_norm))


def _build_embedding_client(subject_concept_id: str):
    return get_llm_client(
        force_init=False,
        user_concept_id=subject_concept_id,
        org_concept_id=subject_concept_id,
    )


def _semantic_search_uses_openai_embeddings() -> bool:
    try:
        embed_model = LlamaIndexRAGService().get_runtime_embed_model()
    except Exception:
        return False
    return type(embed_model).__name__ == "OpenAIEmbedding"


def _probe_embedding_backend(
    subject_concept_id: str,
) -> tuple[bool, str | None]:
    global _embedding_backend_backoff_until
    global _embedding_backend_backoff_reason

    now = time.monotonic()
    if _embedding_backend_backoff_until > now:
        return False, _embedding_backend_backoff_reason

    try:
        client = _build_embedding_client(subject_concept_id)
        client.get_embedding("paper recommendation embedding healthcheck")
    except Exception as exc:
        reason = str(exc)
        _embedding_backend_backoff_until = (
            now + _EMBEDDING_BACKEND_FAILURE_BACKOFF_SECONDS
        )
        _embedding_backend_backoff_reason = reason
        logger.warning(
            "paper_recommendation embedding backend probe failed for %s: %s",
            subject_concept_id,
            exc,
        )
        return False, reason

    _embedding_backend_backoff_until = 0.0
    _embedding_backend_backoff_reason = None
    return True, None


def _semantic_candidate_recall(
    *,
    subject_bundle: Mapping[str, Any],
    limit: int,
) -> tuple[list[str], dict[str, float], str]:
    query_text = _safe_str(subject_bundle.get("matching_text"))
    if not query_text:
        return [], {}, "missing_subject_text"
    try:
        result = search_concepts(
            query=query_text,
            match_type="semantic",
            filter_kind=["individual"],
            instance_of=SCHOLARLY_ARTICLE_TYPE_ID,
            limit=limit,
        )
    except Exception as exc:
        logger.warning("paper_recommendation semantic recall failed: %s", exc)
        return [], {}, f"semantic_search_failed:{exc}"
    rows = (result.get("results") or []) if isinstance(result, Mapping) else []
    candidate_ids: list[str] = []
    score_map: dict[str, float] = {}
    for row in rows if isinstance(rows, Sequence) else []:
        if not isinstance(row, Mapping):
            continue
        concept_id = _safe_str(row.get("concept_id"))
        if not concept_id:
            continue
        candidate_ids.append(concept_id)
        score_map[concept_id] = float(
            row.get("semantic_score")
            or row.get("score")
            or row.get("similarity_score")
            or 0.0
        )
    return _normalise_candidate_ids(candidate_ids), score_map, "semantic_search"


def _recent_candidate_pool(limit: int) -> list[str]:
    try:
        result = search_concepts(
            query="",
            instance_of=SCHOLARLY_ARTICLE_TYPE_ID,
            filter_kind=["individual"],
            limit=limit,
            match_type="substring",
        )
    except Exception as exc:
        logger.warning("paper_recommendation candidate fallback failed: %s", exc)
        return []
    rows = result.get("results") if isinstance(result, Mapping) else []
    if not isinstance(rows, list):
        return []
    return _normalise_candidate_ids(
        [
            _safe_str(row.get("concept_id"))
            for row in rows
            if isinstance(row, Mapping) and _safe_str(row.get("concept_id"))
        ]
    )


def _score_candidate_rows_with_embedding_function(
    *,
    subject_text: str,
    candidate_ids: Sequence[str],
    embed_text,
    lookup_cache: _LookupCache | None = None,
) -> tuple[list[dict[str, Any]], int]:
    subject_embedding = list(embed_text(subject_text))
    rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        paper_bundle = _build_paper_bundle(candidate_id, lookup_cache=lookup_cache)
        if not paper_bundle.get("representation_complete"):
            rows.append(
                {
                    "paper_concept_id": candidate_id,
                    "paper_bundle": paper_bundle,
                    "embedding_score": 0.0,
                    "status": "skipped",
                }
            )
            continue
        paper_embedding = list(embed_text(_safe_str(paper_bundle.get("matching_text"))))
        score = _cosine_similarity(subject_embedding, paper_embedding)
        rows.append(
            {
                "paper_concept_id": candidate_id,
                "paper_bundle": paper_bundle,
                "embedding_score": score,
                "status": "scored",
            }
        )
    rows.sort(
        key=lambda item: (
            -float(item.get("embedding_score") or 0.0),
            _safe_str((item.get("paper_bundle") or {}).get("paper_title")).casefold(),
            _safe_str(item.get("paper_concept_id")).casefold(),
        )
    )
    return rows, len(subject_embedding)


def _score_candidates_with_embeddings(
    *,
    subject_bundle: Mapping[str, Any],
    candidate_ids: Sequence[str],
    lookup_cache: _LookupCache | None = None,
    embedding_backend_ready: bool | None = None,
    embedding_backend_error: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject_text = _safe_str(subject_bundle.get("matching_text"))
    subject_concept_id = _safe_str(subject_bundle.get("subject_concept_id"))
    client = None

    if embedding_backend_ready is False:
        rows, dimensions = _score_candidate_rows_with_embedding_function(
            subject_text=subject_text,
            candidate_ids=candidate_ids,
            embed_text=lambda text: build_text_embedding(text).tolist(),
            lookup_cache=lookup_cache,
        )
        return rows, {
            "embedding_backend": "local_text_hashing_fallback",
            "embedding_client": None,
            "embedding_error": embedding_backend_error or "embedding_preflight_failed",
            "subject_embedding_dimensions": dimensions,
        }

    try:
        client = _build_embedding_client(subject_concept_id)
        rows, dimensions = _score_candidate_rows_with_embedding_function(
            subject_text=subject_text,
            candidate_ids=candidate_ids,
            embed_text=lambda text: client.get_embedding(text),
            lookup_cache=lookup_cache,
        )
        return rows, {
            "embedding_backend": "active_llm_client",
            "embedding_client": type(client).__name__,
            "subject_embedding_dimensions": dimensions,
        }
    except Exception as exc:
        logger.warning(
            "paper_recommendation embedding backend unavailable for %s; "
            "falling back to local deterministic text embeddings: %s",
            subject_concept_id,
            exc,
        )
        rows, dimensions = _score_candidate_rows_with_embedding_function(
            subject_text=subject_text,
            candidate_ids=candidate_ids,
            embed_text=lambda text: build_text_embedding(text).tolist(),
            lookup_cache=lookup_cache,
        )
        return rows, {
            "embedding_backend": "local_text_hashing_fallback",
            "embedding_client": type(client).__name__ if client is not None else None,
            "embedding_error": str(exc),
            "subject_embedding_dimensions": dimensions,
        }


def _extract_json_value(raw_text: str) -> Any:
    text = _safe_str(raw_text)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start_positions = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
    if not start_positions:
        return None
    start = min(start_positions)
    for end in range(len(text), start + 1, -1):
        fragment = text[start:end]
        try:
            return json.loads(fragment)
        except json.JSONDecodeError:
            continue
    return None


def _resolve_reranker_prompt() -> tuple[str | None, dict[str, Any]]:
    prompt_id = resolve_linked_prompt_concept_id(
        workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
        prompt_concept_id=None,
        predicates=(PAPER_RECOMMENDATION_PROMPT_LINK_PREDICATE_ID,),
        default_prompt_concept_id=PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=prompt_id,
        variables={},
        max_chars=_SUBJECT_PROMPT_MAX_CHARS,
        error_prefix="paper_recommendation_reranker",
    )
    return (rendered.text if rendered is not None else None), diagnostics


def _llm_rerank_candidates(
    *,
    subject_bundle: Mapping[str, Any],
    candidate_rows: Sequence[Mapping[str, Any]],
    max_results: int,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    prompt_text, prompt_diagnostics = _resolve_reranker_prompt()
    if not prompt_text:
        diagnostics = dict(prompt_diagnostics)
        diagnostics["decision_mode"] = "embedding_only_fallback"
        diagnostics["llm_used"] = False
        return None, diagnostics

    client = _build_embedding_client(
        _safe_str(subject_bundle.get("subject_concept_id"))
    )
    candidate_payload = [
        {
            "paper_concept_id": row.get("paper_concept_id"),
            "paper_title": (row.get("paper_bundle") or {}).get("paper_title"),
            "embedding_score": round(float(row.get("embedding_score") or 0.0), 6),
            "summary": (row.get("paper_bundle") or {}).get("summary_excerpt"),
            "authors": list((row.get("paper_bundle") or {}).get("author_names") or []),
            "topic_labels": list(
                (row.get("paper_bundle") or {}).get("topic_labels") or []
            ),
            "publication_date": (row.get("paper_bundle") or {}).get("publication_date"),
        }
        for row in candidate_rows
    ]
    prompt = (
        prompt_text
        + "\n\nSubject bundle JSON:\n"
        + json.dumps(
            subject_bundle,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\nCandidate paper bundle JSON:\n"
        + json.dumps(
            candidate_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\nReturn JSON only."
    )
    try:
        raw_response = client.generate(prompt, llm_params={"temperature": 0.0})
    except Exception as exc:
        return None, {
            **prompt_diagnostics,
            "decision_mode": "embedding_only_fallback",
            "llm_used": True,
            "llm_error": str(exc),
        }
    parsed = _extract_json_value(raw_response)
    parsed_rows = parsed.get("recommendations") if isinstance(parsed, Mapping) else parsed
    if not isinstance(parsed_rows, Sequence) or isinstance(
        parsed_rows, (str, bytes, bytearray)
    ):
        return None, {
            **prompt_diagnostics,
            "decision_mode": "embedding_only_fallback",
            "llm_used": True,
            "llm_parse_error": "missing_recommendations_array",
        }

    allowed_ids = {
        _safe_str(item.get("paper_concept_id"))
        for item in candidate_rows
        if isinstance(item, Mapping)
    }
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in parsed_rows:
        if not isinstance(row, Mapping):
            continue
        paper_concept_id = _safe_str(row.get("paper_concept_id"))
        if not paper_concept_id or paper_concept_id not in allowed_ids:
            continue
        if paper_concept_id in seen:
            continue
        seen.add(paper_concept_id)
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        results.append(
            {
                "paper_concept_id": paper_concept_id,
                "score": max(0.0, min(score, 1.0)),
                "rationale_summary": _safe_str(row.get("rationale_summary")),
                "rationale": _safe_str(row.get("rationale")),
                "evidence": list(row.get("evidence") or [])
                if isinstance(row.get("evidence"), Sequence)
                and not isinstance(row.get("evidence"), (str, bytes, bytearray))
                else [],
            }
        )
        if len(results) >= max_results:
            break

    return results, {
        **prompt_diagnostics,
        "decision_mode": "embedding_plus_llm",
        "llm_used": True,
    }


def _embedding_only_rank(
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, candidate_row in enumerate(candidate_rows[:max_results], start=1):
        paper_concept_id = _safe_str(candidate_row.get("paper_concept_id"))
        score = float(candidate_row.get("embedding_score") or 0.0)
        rows.append(
            {
                "paper_concept_id": paper_concept_id,
                "score": score,
                "fallback_rank": index,
                "selected_by_fallback_shortlist": (
                    score > 0.0 and index <= _EMBEDDING_ONLY_ACTIVE_LIMIT
                ),
                "rationale_summary": (
                    "Selected by semantic embedding similarity between the "
                    "subject context and the paper representation."
                ),
                "rationale": (
                    "Embedding-only fallback was used because the authoritative "
                    "reranker prompt or LLM response was unavailable. "
                    "A bounded shortlist was materialised from the strongest "
                    "available semantic matches."
                ),
                "evidence": [
                    {
                        "kind": "embedding_similarity",
                        "score": round(score, 6),
                    }
                ],
            }
        )
    return rows


def _should_mark_recommendation_active(
    *,
    row: Mapping[str, Any],
    decision_mode: str,
    min_score: float,
) -> tuple[bool, str]:
    score = float(row.get("score") or 0.0)
    if score >= float(min_score):
        return True, "score_threshold"
    if decision_mode != "embedding_plus_llm" and bool(
        row.get("selected_by_fallback_shortlist")
    ):
        return True, "embedding_only_top_shortlist"
    return False, "below_threshold"


def _merge_rankings(
    *,
    candidate_rows: Sequence[Mapping[str, Any]],
    reranked_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_candidate_id = {
        _safe_str(row.get("paper_concept_id")): dict(row)
        for row in candidate_rows
        if isinstance(row, Mapping)
    }
    merged: list[dict[str, Any]] = []
    for reranked in reranked_rows:
        paper_concept_id = _safe_str(reranked.get("paper_concept_id"))
        if not paper_concept_id:
            continue
        candidate_row = dict(by_candidate_id.get(paper_concept_id) or {})
        paper_bundle = dict(candidate_row.get("paper_bundle") or {})
        merged.append(
            {
                "paper_concept_id": paper_concept_id,
                "paper_bundle": paper_bundle,
                "embedding_score": float(candidate_row.get("embedding_score") or 0.0),
                "score": float(reranked.get("score") or 0.0),
                "fallback_rank": reranked.get("fallback_rank"),
                "selected_by_fallback_shortlist": bool(
                    reranked.get("selected_by_fallback_shortlist")
                ),
                "rationale_summary": _safe_str(reranked.get("rationale_summary")),
                "rationale": _safe_str(reranked.get("rationale")),
                "evidence": list(reranked.get("evidence") or [])
                if isinstance(reranked.get("evidence"), Sequence)
                and not isinstance(reranked.get("evidence"), (str, bytes, bytearray))
                else [],
            }
        )
    merged.sort(
        key=lambda item: (
            -float(item.get("score") or 0.0),
            -float(item.get("embedding_score") or 0.0),
            _safe_str((item.get("paper_bundle") or {}).get("paper_title")).casefold(),
            _safe_str(item.get("paper_concept_id")).casefold(),
        )
    )
    return merged


def materialise_paper_recommendations_for_subject(
    *,
    subject_concept_id: str,
    candidate_paper_concept_ids: Sequence[str] | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    candidate_limit: int = DEFAULT_CANDIDATE_RECALL_LIMIT,
    include_all_candidates: bool = False,
    min_score: float = MIN_RECOMMENDATION_SCORE,
    trigger_source: str | None = None,
) -> dict[str, Any]:
    """Semantic candidate recall + reranking + Vontology materialisation."""

    lookup_cache = _LookupCache()
    subject_id = _safe_str(subject_concept_id)
    if not subject_id:
        return {
            "success": False,
            "error": "missing_subject_concept_id",
            "message": "A target subject concept is required.",
        }

    safe_max_results = _normalise_int(
        max_results,
        default=DEFAULT_MAX_RESULTS,
        minimum=1,
        maximum=MAX_MAX_RESULTS,
    )
    safe_candidate_limit = _normalise_int(
        candidate_limit,
        default=DEFAULT_CANDIDATE_RECALL_LIMIT,
        minimum=1,
        maximum=MAX_CANDIDATE_RECALL_LIMIT,
    )
    subject_bundle = _build_subject_bundle(subject_id, lookup_cache=lookup_cache)
    if not subject_bundle.get("success"):
        return {
            "success": False,
            "error": subject_bundle.get("error") or "subject_bundle_failed",
            "subject_concept_id": subject_id,
            "message": "Could not assemble the recommendation subject context.",
        }

    explicit_candidate_ids = _normalise_candidate_ids(candidate_paper_concept_ids)
    recall_diagnostics: dict[str, Any] = {
        "trigger_source": _safe_str(trigger_source) or None,
    }
    embedding_backend_ready: bool | None = None
    embedding_backend_error: str | None = None
    if explicit_candidate_ids:
        candidate_ids = explicit_candidate_ids
        semantic_score_map: dict[str, float] = {}
        recall_diagnostics["candidate_source"] = "explicit_candidate_ids"
    else:
        if _semantic_search_uses_openai_embeddings():
            embedding_backend_ready, embedding_backend_error = _probe_embedding_backend(
                subject_id
            )
            recall_diagnostics["semantic_backend_ready"] = embedding_backend_ready
            if embedding_backend_error:
                recall_diagnostics["semantic_backend_error"] = embedding_backend_error
        if embedding_backend_ready is False:
            candidate_ids = []
            semantic_score_map = {}
            recall_diagnostics["candidate_source"] = "semantic_search_preflight_skip"
        else:
            candidate_ids, semantic_score_map, candidate_source = _semantic_candidate_recall(
                subject_bundle=subject_bundle,
                limit=safe_candidate_limit,
            )
            candidate_ids = _normalise_candidate_ids(candidate_ids)
            recall_diagnostics["candidate_source"] = candidate_source
        if not candidate_ids:
            candidate_ids = _recent_candidate_pool(safe_candidate_limit)
            semantic_score_map = {}
            recall_diagnostics["candidate_source"] = "recent_fallback_pool"

    if not candidate_ids:
        return {
            "success": False,
            "error": "no_candidate_papers",
            "subject_concept_id": subject_id,
            "message": "No candidate scholarly papers were available for evaluation.",
            "profile_concept_id": subject_bundle.get("profile_concept_id"),
            "profile_diagnostics": subject_bundle,
        }

    scored_candidates, embedding_diagnostics = _score_candidates_with_embeddings(
        subject_bundle=subject_bundle,
        candidate_ids=candidate_ids,
        lookup_cache=lookup_cache,
        embedding_backend_ready=embedding_backend_ready,
        embedding_backend_error=embedding_backend_error,
    )
    llm_candidate_rows = [
        row for row in scored_candidates if row.get("status") == "scored"
    ][: min(DEFAULT_LLM_CANDIDATE_LIMIT, safe_max_results * 3)]

    llm_rows, llm_diagnostics = _llm_rerank_candidates(
        subject_bundle=subject_bundle,
        candidate_rows=llm_candidate_rows,
        max_results=safe_max_results,
    )
    decision_mode = llm_diagnostics.get("decision_mode") or "embedding_only_fallback"
    if llm_rows is None:
        llm_rows = _embedding_only_rank(
            llm_candidate_rows,
            max_results=safe_max_results,
        )
    merged_rows = _merge_rankings(
        candidate_rows=llm_candidate_rows,
        reranked_rows=llm_rows,
    )

    existing_rows = load_materialised_paper_recommendations(
        subject_concept_id=subject_id,
        include_inactive=True,
        limit=500,
    )
    existing_by_paper_id = {
        _safe_str(row.get("paper_concept_id")): row
        for row in (existing_rows.get("recommendations") or [])
        if isinstance(row, Mapping)
    }

    active_paper_ids: set[str] = set()
    persisted_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if decision_mode != "embedding_plus_llm":
        warnings.append(
            "Authoritative LLM reranking was unavailable; a bounded embedding-only "
            "fallback shortlist was used."
        )
    for row in merged_rows:
        paper_concept_id = _safe_str(row.get("paper_concept_id"))
        if not paper_concept_id:
            continue
        score = float(row.get("score") or 0.0)
        paper_bundle = dict(row.get("paper_bundle") or {})
        active, selection_rule = _should_mark_recommendation_active(
            row=row,
            decision_mode=decision_mode,
            min_score=float(min_score),
        )
        evaluation_payload = {
            "schema_version": "paper_recommendation_assertion.v1",
            "policy_version": PAPER_RECOMMENDATION_POLICY_VERSION,
            "decision_mode": decision_mode,
            "selection_rule": selection_rule,
            "active": active,
            "score": score,
            "embedding_score": float(row.get("embedding_score") or 0.0),
            "fallback_rank": row.get("fallback_rank"),
            "rationale_summary": _safe_str(row.get("rationale_summary")),
            "rationale": _safe_str(row.get("rationale")),
            "evidence": list(row.get("evidence") or []),
            "trigger_source": _safe_str(trigger_source) or None,
            "paper_title": paper_bundle.get("paper_title"),
            "paper_representation": {
                "paper_title": paper_bundle.get("paper_title"),
                "summary_excerpt": paper_bundle.get("summary_excerpt"),
                "author_names": list(paper_bundle.get("author_names") or []),
                "topic_labels": list(paper_bundle.get("topic_labels") or []),
                "publication_date": paper_bundle.get("publication_date"),
            },
            "subject_profile_concept_id": subject_bundle.get("profile_concept_id"),
            "subject_profile_source_predicate": subject_bundle.get(
                "profile_source_predicate"
            ),
        }
        persistence = upsert_paper_recommendation_assertion(
            subject_concept_id=subject_id,
            paper_concept_id=paper_concept_id,
            evaluation_payload=evaluation_payload,
            rationale_summary=_safe_str(row.get("rationale_summary")),
            provenance={"source": "paper_recommendation_materialisation_service"},
            context={
                "decision_mode": decision_mode,
                "profile_predicate": subject_bundle.get("profile_source_predicate"),
            },
        )
        if active:
            active_paper_ids.add(paper_concept_id)
        persisted_rows.append(
            {
                "paper_concept_id": paper_concept_id,
                "paper_title": paper_bundle.get("paper_title") or paper_concept_id,
                "status": "ranked",
                "score": score,
                "recommendation_tier": "recommended" if active else "inactive",
                "rationale_summary": _safe_str(row.get("rationale_summary")),
                "rationale": _safe_str(row.get("rationale")),
                "evidence": list(row.get("evidence") or []),
                "paper_representation": evaluation_payload["paper_representation"],
                "provenance": {
                    "decision_mode": decision_mode,
                    "assertion_concept_id": persistence.get("assertion_concept_id"),
                    "policy_version": PAPER_RECOMMENDATION_POLICY_VERSION,
                    "selection_rule": selection_rule,
                    "semantic_recall_score": semantic_score_map.get(paper_concept_id),
                    "embedding_score": float(row.get("embedding_score") or 0.0),
                    "fallback_rank": row.get("fallback_rank"),
                },
                "active": active,
            }
        )

    for paper_concept_id, existing_row in existing_by_paper_id.items():
        if paper_concept_id in active_paper_ids:
            continue
        existing_evaluation = existing_row.get("evaluation")
        if not isinstance(existing_evaluation, Mapping):
            continue
        if not bool(existing_evaluation.get("active", True)):
            continue
        upsert_paper_recommendation_assertion(
            subject_concept_id=subject_id,
            paper_concept_id=paper_concept_id,
            evaluation_payload={
                **dict(existing_evaluation),
                "active": False,
                "policy_version": PAPER_RECOMMENDATION_POLICY_VERSION,
                "decision_mode": decision_mode,
                "trigger_source": _safe_str(trigger_source) or None,
            },
            rationale_summary=_safe_str(existing_evaluation.get("rationale_summary")),
            provenance={"source": "paper_recommendation_materialisation_service"},
            context={"inactive_reason": "not_selected_in_latest_refresh"},
        )

    skipped_rows = [
        {
            "paper_concept_id": row.get("paper_concept_id"),
            "paper_title": (row.get("paper_bundle") or {}).get("paper_title")
            or row.get("paper_concept_id"),
            "status": "skipped",
            "skip_reason": ",".join(
                list(((row.get("paper_bundle") or {}).get("representation_failures") or []))
            )
            or "paper_bundle_incomplete",
            "paper_representation": {
                "paper_title": (row.get("paper_bundle") or {}).get("paper_title"),
            },
            "provenance": {"decision_mode": decision_mode},
        }
        for row in scored_candidates
        if row.get("status") != "scored"
    ]

    if include_all_candidates:
        results = persisted_rows + skipped_rows
    else:
        active_rows = [
            row for row in persisted_rows if bool(row.get("active"))
        ][:safe_max_results]
        inactive_rows = [
            row for row in persisted_rows if not bool(row.get("active"))
        ]
        results = active_rows + skipped_rows
        if not active_rows:
            results.extend(inactive_rows[:safe_max_results])

    return {
        "success": True,
        "user_concept_id": subject_id,
        "subject_concept_id": subject_id,
        "profile_concept_id": subject_bundle.get("profile_concept_id"),
        "recommendation_policy_version": PAPER_RECOMMENDATION_POLICY_VERSION,
        "generated_at": None,
        "results": results,
        "ranked_count": len(persisted_rows),
        "skipped_count": len(skipped_rows),
        "candidate_count_requested": len(candidate_ids),
        "warning_count": len(warnings),
        "warnings": warnings,
        "profile_signal_summary": {
            "usable": True,
            "profile_present": bool(subject_bundle.get("profile_present")),
            "related_concept_count": len(subject_bundle.get("related_concepts") or []),
            "research_interest_count": len(
                subject_bundle.get("research_interest_concepts") or []
            ),
            "organisation_count": len(
                subject_bundle.get("organisation_concept_ids") or []
            ),
        },
        "profile_diagnostics": {
            "subject_bundle": subject_bundle,
            "recall": recall_diagnostics,
            "embedding": embedding_diagnostics,
            "reranker": llm_diagnostics,
        },
    }


def _impacted_subjects_from_text_mutation(
    event_payload: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    subject_concept_id = _safe_str(event_payload.get("subject_concept_id"))
    predicate = _safe_str(event_payload.get("predicate"))
    if not subject_concept_id:
        return [], []
    if predicate == GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID:
        return [subject_concept_id], []
    if predicate == _LEGACY_PROFILE_JSON_PREDICATE_ID:
        return resolve_subject_ids_for_legacy_profile_concept(subject_concept_id), []
    concept_doc = _get_concept(subject_concept_id)
    if _is_scholarly_article(concept_doc) and predicate in _AFFECTING_PAPER_TEXT_PREDICATES:
        return _default_refresh_subject_ids(limit=50), [subject_concept_id]
    return [], []


def _impacted_subjects_from_relationship_mutation(
    event_payload: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    source_id = _safe_str(event_payload.get("source_id"))
    predicate = _safe_str(event_payload.get("predicate"))
    if not source_id or not predicate:
        return [], []
    if predicate in _AFFECTING_SUBJECT_RELATIONSHIP_PREDICATES:
        return [source_id], []
    source_doc = _get_concept(source_id)
    if _is_scholarly_article(source_doc):
        return _default_refresh_subject_ids(limit=50), [source_id]
    return [], []


def _default_refresh_subject_ids(*, limit: int = 50) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for subject_id in list_subject_concept_ids_with_paper_matching_profiles(limit=limit):
        if subject_id not in seen:
            seen.add(subject_id)
            rows.append(subject_id)
        if len(rows) >= limit:
            return rows
    for subject_id in list_paper_recommendation_delivery_subject_ids(limit=limit):
        if subject_id not in seen:
            seen.add(subject_id)
            rows.append(subject_id)
        if len(rows) >= limit:
            break
    return rows


def materialise_paper_recommendations_from_event(
    *,
    event_payload: Mapping[str, Any] | None = None,
    target_subject_concept_ids: Sequence[str] | None = None,
    candidate_paper_concept_ids: Sequence[str] | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_RECALL_LIMIT,
    max_results: int = DEFAULT_MAX_RESULTS,
    trigger_source: str | None = None,
    discover_subjects_if_missing: bool = False,
) -> dict[str, Any]:
    """Resolve affected subjects/candidate papers from an event and refresh them."""

    payload = dict(event_payload or {})
    resolved_subject_ids = _normalise_subject_ids(target_subject_concept_ids)
    resolved_candidate_ids = _normalise_candidate_ids(candidate_paper_concept_ids)
    event_type = _safe_str(payload.get("event_type"))

    if not resolved_subject_ids and event_type in {
        EVENT_TYPE_TEXT_RELATION_UPSERTED,
        EVENT_TYPE_TEXT_RELATION_UPDATED,
    }:
        event_subjects, event_candidates = _impacted_subjects_from_text_mutation(payload)
        resolved_subject_ids = event_subjects
        if not resolved_candidate_ids:
            resolved_candidate_ids = event_candidates
    if not resolved_subject_ids and event_type in {
        EVENT_TYPE_RELATIONSHIP_ADDED,
        EVENT_TYPE_RELATIONSHIP_REMOVED,
    }:
        event_subjects, event_candidates = _impacted_subjects_from_relationship_mutation(
            payload
        )
        resolved_subject_ids = event_subjects
        if not resolved_candidate_ids:
            resolved_candidate_ids = event_candidates
    if not resolved_subject_ids and resolved_candidate_ids:
        resolved_subject_ids = _default_refresh_subject_ids(limit=50)
    if not resolved_subject_ids and bool(discover_subjects_if_missing):
        resolved_subject_ids = list_paper_recommendation_delivery_subject_ids(limit=50)

    if not resolved_subject_ids:
        return {
            "success": True,
            "triggered": False,
            "refreshed_subject_count": 0,
            "refreshed_paper_count": 0,
            "reason": "no_impacted_subjects",
            "event_type": event_type or None,
        }

    reports: list[dict[str, Any]] = []
    for subject_id in resolved_subject_ids:
        report = materialise_paper_recommendations_for_subject(
            subject_concept_id=subject_id,
            candidate_paper_concept_ids=resolved_candidate_ids or None,
            candidate_limit=candidate_limit,
            max_results=max_results,
            include_all_candidates=False,
            trigger_source=trigger_source or event_type or "event_refresh",
        )
        reports.append(report)

    return {
        "success": all(bool(report.get("success")) for report in reports),
        "triggered": True,
        "event_type": event_type or None,
        "refreshed_subject_count": len(resolved_subject_ids),
        "refreshed_paper_count": len(resolved_candidate_ids),
        "subject_concept_ids": resolved_subject_ids,
        "candidate_paper_concept_ids": resolved_candidate_ids,
        "reports": reports,
    }


__all__ = [
    "materialise_paper_recommendations_for_subject",
    "materialise_paper_recommendations_from_event",
]
