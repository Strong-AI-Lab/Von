"""Semantic paper recommendation materialisation for generic subject concepts."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from math import sqrt
from typing import Any, Mapping, Sequence

from ..languagemodels.llm_interface import get_llm_client
from .concept_embedding_service import build_concept_searchable_text
from .concept_search_service import search_concepts
from .concept_service import get_concept_by_concept_id_exact
from .concept_similarity_service import build_text_embedding
from .paper_recommendation_constants import (
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_RATIONALE_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
    SCHOLARLY_ARTICLE_TYPE_ID,
)
from .paper_recommendation_delivery_service import (
    list_paper_recommendation_delivery_subject_ids,
)
from .paper_recommendation_policy_authority_service import (
    PaperRecommendationPolicy,
    resolve_paper_recommendation_policy,
)
from .paper_recommendation_vontology_service import (
    list_subject_concept_ids_with_paper_matching_profiles,
    load_materialised_paper_recommendations,
    load_subject_paper_matching_profile,
    resolve_subject_ids_for_legacy_profile_concept,
    upsert_paper_recommendation_assertion,
)
from .rag_backends.llamaindex_backend import LlamaIndexRAGService
from .text_value_service import get_texts_for_concept
from .workflow_event_integration_service import (
    EVENT_TYPE_RELATIONSHIP_ADDED,
    EVENT_TYPE_RELATIONSHIP_REMOVED,
    EVENT_TYPE_SCOPED_ASSERTION_RETRACTED,
    EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
    EVENT_TYPE_TEXT_RELATION_UPDATED,
    EVENT_TYPE_TEXT_RELATION_UPSERTED,
)
from .workflow_prompt_authority_service import (
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
)

logger = logging.getLogger(__name__)

_SUBJECT_PROMPT_MAX_CHARS = 12000
_LEGACY_PROFILE_JSON_PREDICATE_ID = "#V#has_paper_recommendation_profile_json"
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


def _excerpt_text(value: Any, *, limit: int) -> str:
    text = _safe_str(value)
    if not text or limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    shortened = text[: max(1, limit - 3)].rstrip()
    return shortened + "..."


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


def _first_text_with_predicate(
    subject_concept_id: str,
    predicates: Sequence[str],
    *,
    lookup_cache: _LookupCache | None = None,
) -> tuple[str | None, str | None]:
    for predicate in predicates:
        text = _first_text(
            subject_concept_id,
            (predicate,),
            lookup_cache=lookup_cache,
        )
        if text:
            return text, predicate
    return None, None


def _relationship_rows(
    concept_doc: Mapping[str, Any],
    *,
    policy: PaperRecommendationPolicy,
    per_predicate_limit: int | None = None,
    lookup_cache: _LookupCache | None = None,
) -> list[dict[str, str]]:
    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return []
    limit = int(per_predicate_limit or policy.relationship_per_predicate_limit)
    structural_rel_keys = set(policy.matching_string_tuple("relationship_exclusion_predicates"))
    visibility_rel_keys = set(policy.matching_string_tuple("visibility_exclusion_predicates"))
    rows: list[dict[str, str]] = []
    for predicate, raw_targets in relationships.items():
        predicate_id = _safe_str(predicate)
        if not predicate_id or predicate_id in structural_rel_keys:
            continue
        if predicate_id in visibility_rel_keys:
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
            if added >= limit:
                break
    return rows


def _build_subject_bundle(
    subject_concept_id: str,
    *,
    policy: PaperRecommendationPolicy,
    lookup_cache: _LookupCache | None = None,
) -> dict[str, Any]:
    profile_payload = load_subject_paper_matching_profile(subject_concept_id)
    if not profile_payload.get("success"):
        return {
            "success": False,
            "error": profile_payload.get("error") or "subject_profile_load_failed",
            "subject_concept_id": subject_concept_id,
        }
    if profile_payload.get("profile_conflict"):
        return {
            "success": False,
            "error": "paper_matching_profile_conflict",
            "subject_concept_id": subject_concept_id,
            "profile_diagnostics": profile_payload.get("profile_diagnostics"),
        }

    subject_doc = dict(profile_payload.get("subject_doc") or {})
    subject_name = _safe_str(subject_doc.get("name")) or subject_concept_id
    related_rows = _relationship_rows(subject_doc, policy=policy, lookup_cache=lookup_cache)
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
        f"{policy.context_label('subject_context_labels', 'subject_concept', 'Subject concept')}: {subject_name}",
        f"{policy.context_label('subject_context_labels', 'concept_id', 'Concept ID')}: {subject_concept_id}",
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
            label = policy.context_label(
                "subject_context_labels",
                "research_interests",
                "Research interests",
            )
            fact_lines.append(label + ": " + ", ".join(interest_names))
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
            label = policy.context_label(
                "subject_context_labels",
                "organisations",
                "Organisations",
            )
            fact_lines.append(label + ": " + ", ".join(organisation_names))
    if profile:
        profile_lines: list[str] = []
        for field, value in profile.items():
            if field in {"schema_version", "subject_concept_id", "updated_at"}:
                continue
            label = policy.profile_field_label(field)
            if isinstance(value, list) and value:
                profile_lines.append(
                    f"{label}: {', '.join(_normalise_string_list(value))}"
                )
            elif isinstance(value, str) and value.strip():
                profile_lines.append(f"{label}: {value.strip()}")
        if profile_lines:
            label = policy.context_label(
                "subject_context_labels",
                "profile_overlay",
                "Profile overlay",
            )
            fact_lines.append(label + ":\n" + "\n".join(profile_lines))

    return {
        "success": True,
        "subject_concept_id": subject_concept_id,
        "subject_name": subject_name,
        "profile": profile,
        "profile_present": bool(profile_payload.get("profile_present")),
        "profile_concept_id": profile_payload.get("profile_concept_id"),
        "profile_source_predicate": profile_payload.get("profile_source_predicate"),
        "profile_diagnostics": profile_payload.get("profile_diagnostics"),
        "research_interest_concepts": research_interest_rows,
        "organisation_concept_ids": organisation_ids,
        "related_concepts": related_rows,
        "matching_text": "\n\n".join(line for line in fact_lines if line),
    }


def _build_paper_bundle(
    paper_concept_id: str,
    *,
    policy: PaperRecommendationPolicy,
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
    summary, summary_source_predicate = _first_text_with_predicate(
        paper_id,
        policy.paper_text_predicates("summary"),
        lookup_cache=lookup_cache,
    )
    summary = summary or ""
    content_text = (
        _first_text(
            paper_id,
            ("hasContent",),
            lookup_cache=lookup_cache,
        )
        or ""
    )
    publication_date = _first_text(
        paper_id,
        policy.paper_text_predicates("publication_date"),
        lookup_cache=lookup_cache,
    )
    topic_label_text = _first_text(
        paper_id,
        policy.paper_text_predicates("topic_labels"),
        lookup_cache=lookup_cache,
    )
    topic_labels = _normalise_string_list(topic_label_text)
    relationship_rows = _relationship_rows(paper_doc, policy=policy, lookup_cache=lookup_cache)
    author_relation_predicates = set(policy.matching_string_tuple("author_relation_predicates"))
    topic_relation_predicates = set(policy.matching_string_tuple("topic_relation_predicates"))
    author_names = [
        row["target_name"]
        for row in relationship_rows
        if row.get("predicate") in author_relation_predicates
    ]
    if not topic_labels:
        topic_labels = [
            row["target_name"]
            for row in relationship_rows
            if row.get("predicate") in topic_relation_predicates
        ]
    base_text = build_concept_searchable_text(paper_doc)
    paper_context_text = content_text or summary or base_text
    paper_context_source = (
        "hasContent"
        if content_text
        else summary_source_predicate or "concept_searchable_text"
    )
    context_lines = [
        f"{policy.context_label('paper_context_labels', 'paper_title', 'Paper title')}: {paper_title}",
        f"{policy.context_label('paper_context_labels', 'paper_concept_id', 'Paper concept ID')}: {paper_id}",
    ]
    if base_text:
        context_lines.append(base_text)
    if summary and summary not in base_text:
        label = policy.context_label(
            "paper_context_labels",
            "summary",
            "Abstract or summary",
        )
        context_lines.append(label + ": " + summary)
    if author_names:
        label = policy.context_label("paper_context_labels", "authors", "Authors")
        context_lines.append(label + ": " + ", ".join(author_names))
    if topic_labels:
        label = policy.context_label("paper_context_labels", "topics", "Topics")
        context_lines.append(label + ": " + ", ".join(topic_labels))
    if publication_date:
        label = policy.context_label(
            "paper_context_labels",
            "publication_date",
            "Publication date",
        )
        context_lines.append(label + ": " + publication_date)

    bundle = {
        "paper_concept_id": paper_id,
        "paper_title": paper_title,
        "paper_summary": summary,
        "summary_excerpt": _excerpt_text(summary, limit=policy.summary_excerpt_chars),
        "summary_source_predicate": summary_source_predicate,
        "author_names": author_names,
        "topic_labels": topic_labels,
        "publication_date": publication_date,
        "paper_context_excerpt": _excerpt_text(
            paper_context_text,
            limit=policy.paper_context_excerpt_chars,
        ),
        "paper_context_source": paper_context_source,
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
    policy: PaperRecommendationPolicy,
    embed_text,
    lookup_cache: _LookupCache | None = None,
) -> tuple[list[dict[str, Any]], int]:
    subject_embedding = list(embed_text(subject_text))
    rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        paper_bundle = _build_paper_bundle(
            candidate_id,
            policy=policy,
            lookup_cache=lookup_cache,
        )
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
    policy: PaperRecommendationPolicy,
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
            policy=policy,
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
            policy=policy,
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
            policy=policy,
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


def _resolve_rationale_prompt() -> tuple[str | None, dict[str, Any]]:
    prompt_id = resolve_linked_prompt_concept_id(
        workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
        prompt_concept_id=None,
        predicates=(PAPER_RECOMMENDATION_RATIONALE_PROMPT_LINK_PREDICATE_ID,),
        default_prompt_concept_id=PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=prompt_id,
        variables={},
        max_chars=_SUBJECT_PROMPT_MAX_CHARS,
        error_prefix="paper_recommendation_rationale",
    )
    return (rendered.text if rendered is not None else None), diagnostics


def _normalise_evidence_list(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            rows.append(dict(item))
            continue
        text = _safe_str(item)
        if text:
            rows.append(text)
    return rows


def _is_placeholder_rationale(text: Any, *, policy: PaperRecommendationPolicy) -> bool:
    cleaned = _safe_str(text)
    if not cleaned:
        return False
    return cleaned == policy.placeholder_rationale_summary


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
            "paper_context_excerpt": (
                (row.get("paper_bundle") or {}).get("paper_context_excerpt")
            ),
            "paper_context_source": (
                (row.get("paper_bundle") or {}).get("paper_context_source")
            ),
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
                "evidence": _normalise_evidence_list(row.get("evidence")),
            }
        )
        if len(results) >= max_results:
            break

    return results, {
        **prompt_diagnostics,
        "decision_mode": "embedding_plus_llm",
        "llm_used": True,
    }


def _llm_generate_authoritative_rationale(
    *,
    subject_bundle: Mapping[str, Any],
    candidate_row: Mapping[str, Any],
    decision_mode: str,
    selection_rule: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    prompt_text, prompt_diagnostics = _resolve_rationale_prompt()
    if not prompt_text:
        diagnostics = dict(prompt_diagnostics)
        diagnostics["status"] = "unavailable"
        return None, diagnostics

    client = _build_embedding_client(
        _safe_str(subject_bundle.get("subject_concept_id"))
    )
    paper_bundle = dict(candidate_row.get("paper_bundle") or {})
    paper_payload = {
        "paper_concept_id": candidate_row.get("paper_concept_id"),
        "paper_title": paper_bundle.get("paper_title"),
        "summary_excerpt": paper_bundle.get("summary_excerpt"),
        "summary_source_predicate": paper_bundle.get("summary_source_predicate"),
        "paper_context_excerpt": paper_bundle.get("paper_context_excerpt"),
        "paper_context_source": paper_bundle.get("paper_context_source"),
        "author_names": list(paper_bundle.get("author_names") or []),
        "topic_labels": list(paper_bundle.get("topic_labels") or []),
        "publication_date": paper_bundle.get("publication_date"),
    }
    recommendation_context = {
        "decision_mode": decision_mode,
        "selection_rule": selection_rule,
        "score": round(float(candidate_row.get("score") or 0.0), 6),
        "embedding_score": round(float(candidate_row.get("embedding_score") or 0.0), 6),
    }
    prompt = (
        prompt_text
        + "\n\nSubject bundle JSON:\n"
        + json.dumps(
            subject_bundle,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\nPaper bundle JSON:\n"
        + json.dumps(
            paper_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\nRecommendation context JSON:\n"
        + json.dumps(
            recommendation_context,
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
            "status": "unavailable",
            "llm_error": str(exc),
        }
    parsed = _extract_json_value(raw_response)
    if not isinstance(parsed, Mapping):
        return None, {
            **prompt_diagnostics,
            "status": "unavailable",
            "llm_parse_error": "missing_rationale_object",
        }

    rationale_summary = _safe_str(parsed.get("rationale_summary")) or _safe_str(
        parsed.get("rationale")
    )
    rationale = _safe_str(parsed.get("rationale")) or rationale_summary
    if not rationale_summary:
        return None, {
            **prompt_diagnostics,
            "status": "unavailable",
            "llm_parse_error": "missing_rationale_summary",
        }

    return (
        {
            "rationale_summary": rationale_summary,
            "rationale": rationale,
            "evidence": _normalise_evidence_list(parsed.get("evidence")),
            "rationale_generation": {
                "status": "generated",
                "source": "rationale_prompt",
                "prompt_concept_id": prompt_diagnostics.get(
                    "loaded_prompt_concept_id"
                )
                or prompt_diagnostics.get("resolved_prompt_concept_id"),
                "paper_context_source": paper_bundle.get("paper_context_source"),
                "used_summary_proxy": paper_bundle.get("paper_context_source")
                != "hasContent",
            },
        },
        {
            **prompt_diagnostics,
            "status": "generated",
            "paper_concept_id": candidate_row.get("paper_concept_id"),
        },
    )


def _embedding_only_rank(
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    policy: PaperRecommendationPolicy,
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
                    score > 0.0 and index <= policy.embedding_only_active_limit
                ),
                "rationale_summary": "",
                "rationale": "",
                "evidence": [
                    {
                        "kind": "embedding_similarity",
                        "score": round(score, 6),
                    }
                ],
            }
        )
    return rows


def _apply_authoritative_rationale_generation(
    *,
    subject_bundle: Mapping[str, Any],
    ranked_rows: Sequence[Mapping[str, Any]],
    policy: PaperRecommendationPolicy,
    max_results: int,
    decision_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved_rows: list[dict[str, Any]] = []
    diagnostics_rows: list[dict[str, Any]] = []
    for index, row in enumerate(ranked_rows):
        resolved = dict(row)
        paper_bundle = dict(row.get("paper_bundle") or {})
        existing_summary = _safe_str(resolved.get("rationale_summary"))
        existing_rationale = _safe_str(resolved.get("rationale"))
        if _is_placeholder_rationale(existing_summary, policy=policy):
            existing_summary = ""
        if _is_placeholder_rationale(existing_rationale, policy=policy):
            existing_rationale = ""
        resolved["rationale_summary"] = existing_summary
        resolved["rationale"] = existing_rationale
        resolved["evidence"] = _normalise_evidence_list(resolved.get("evidence"))

        default_generation = {
            "status": "generated" if existing_summary or existing_rationale else "unavailable",
            "source": "reranker_llm"
            if decision_mode == "embedding_plus_llm"
            else "embedding_only_fallback",
            "paper_context_source": paper_bundle.get("paper_context_source"),
            "used_summary_proxy": paper_bundle.get("paper_context_source")
            != "hasContent",
        }
        should_attempt = index < max_results and (
            decision_mode != "embedding_plus_llm" or not existing_summary
        )
        if should_attempt:
            selection_rule = _safe_str(resolved.get("selection_rule")) or (
                "score_threshold"
                if decision_mode == "embedding_plus_llm"
                else "embedding_only_top_shortlist"
            )
            generated, rationale_diagnostics = _llm_generate_authoritative_rationale(
                subject_bundle=subject_bundle,
                candidate_row=resolved,
                decision_mode=decision_mode,
                selection_rule=selection_rule,
            )
            diagnostics_rows.append(rationale_diagnostics)
            if generated is not None:
                resolved.update(generated)
            else:
                default_generation = {
                    "status": "unavailable",
                    "source": "rationale_prompt",
                    "reason_code": rationale_diagnostics.get("error")
                    or rationale_diagnostics.get("llm_error")
                    or rationale_diagnostics.get("llm_parse_error")
                    or "authoritative_rationale_unavailable",
                    "prompt_concept_id": rationale_diagnostics.get(
                        "loaded_prompt_concept_id"
                    )
                    or rationale_diagnostics.get("resolved_prompt_concept_id"),
                    "paper_context_source": paper_bundle.get("paper_context_source"),
                    "used_summary_proxy": paper_bundle.get("paper_context_source")
                    != "hasContent",
                }

        resolved["rationale_generation"] = dict(
            resolved.get("rationale_generation") or default_generation
        )
        resolved_rows.append(resolved)
    return resolved_rows, diagnostics_rows


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
    max_results: int | None = None,
    candidate_limit: int | None = None,
    include_all_candidates: bool = False,
    min_score: float | None = None,
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

    policy = resolve_paper_recommendation_policy()
    safe_max_results = _normalise_int(
        max_results,
        default=policy.default_max_results,
        minimum=1,
        maximum=policy.max_max_results,
    )
    safe_candidate_limit = _normalise_int(
        candidate_limit,
        default=policy.default_candidate_recall_limit,
        minimum=1,
        maximum=policy.max_candidate_recall_limit,
    )
    safe_min_score = (
        float(min_score)
        if min_score is not None
        else float(policy.min_recommendation_score)
    )
    subject_bundle = _build_subject_bundle(
        subject_id,
        policy=policy,
        lookup_cache=lookup_cache,
    )
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
        policy=policy,
        lookup_cache=lookup_cache,
        embedding_backend_ready=embedding_backend_ready,
        embedding_backend_error=embedding_backend_error,
    )
    llm_candidate_rows = [
        row for row in scored_candidates if row.get("status") == "scored"
    ][: min(policy.default_llm_candidate_limit, safe_max_results * 3)]

    llm_rows, llm_diagnostics = _llm_rerank_candidates(
        subject_bundle=subject_bundle,
        candidate_rows=llm_candidate_rows,
        max_results=safe_max_results,
    )
    decision_mode = llm_diagnostics.get("decision_mode") or "embedding_only_fallback"
    if llm_rows is None:
        llm_rows = _embedding_only_rank(
            llm_candidate_rows,
            policy=policy,
            max_results=safe_max_results,
        )
    merged_rows = _merge_rankings(
        candidate_rows=llm_candidate_rows,
        reranked_rows=llm_rows,
    )
    merged_rows, rationale_diagnostics = _apply_authoritative_rationale_generation(
        subject_bundle=subject_bundle,
        ranked_rows=merged_rows,
        policy=policy,
        max_results=safe_max_results,
        decision_mode=decision_mode,
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
            min_score=safe_min_score,
        )
        row["selection_rule"] = selection_rule
        evaluation_payload = {
            "schema_version": "paper_recommendation_assertion.v1",
            "policy_version": policy.policy_version,
            "decision_mode": decision_mode,
            "selection_rule": selection_rule,
            "active": active,
            "score": score,
            "embedding_score": float(row.get("embedding_score") or 0.0),
            "fallback_rank": row.get("fallback_rank"),
            "rationale_summary": _safe_str(row.get("rationale_summary")),
            "rationale": _safe_str(row.get("rationale")),
            "evidence": _normalise_evidence_list(row.get("evidence")),
            "rationale_generation": dict(row.get("rationale_generation") or {}),
            "trigger_source": _safe_str(trigger_source) or None,
            "paper_title": paper_bundle.get("paper_title"),
            "paper_representation": {
                "paper_title": paper_bundle.get("paper_title"),
                "summary_excerpt": paper_bundle.get("summary_excerpt"),
                "summary_source_predicate": paper_bundle.get("summary_source_predicate"),
                "paper_context_source": paper_bundle.get("paper_context_source"),
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
                "evidence": _normalise_evidence_list(row.get("evidence")),
                "rationale_generation": dict(row.get("rationale_generation") or {}),
                "paper_representation": evaluation_payload["paper_representation"],
                "provenance": {
                    "decision_mode": decision_mode,
                    "assertion_concept_id": persistence.get("assertion_concept_id"),
                    "policy_version": policy.policy_version,
                    "selection_rule": selection_rule,
                    "semantic_recall_score": semantic_score_map.get(paper_concept_id),
                    "embedding_score": float(row.get("embedding_score") or 0.0),
                    "fallback_rank": row.get("fallback_rank"),
                    "rationale_status": (
                        (row.get("rationale_generation") or {}).get("status")
                    ),
                    "rationale_source": (
                        (row.get("rationale_generation") or {}).get("source")
                    ),
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
                "policy_version": policy.policy_version,
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
        "recommendation_policy_version": policy.policy_version,
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
            "rationale_generation": rationale_diagnostics,
            "policy": dict(policy.diagnostics),
        },
    }


def _impacted_subjects_from_text_mutation(
    event_payload: Mapping[str, Any],
    *,
    policy: PaperRecommendationPolicy,
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
    affecting_text_predicates = set(
        policy.matching_string_tuple("affecting_paper_text_predicates")
    )
    if _is_scholarly_article(concept_doc) and predicate in affecting_text_predicates:
        return _default_refresh_subject_ids(limit=50), [subject_concept_id]
    return [], []


def _impacted_subjects_from_relationship_mutation(
    event_payload: Mapping[str, Any],
    *,
    policy: PaperRecommendationPolicy,
) -> tuple[list[str], list[str]]:
    source_id = _safe_str(event_payload.get("source_id"))
    predicate = _safe_str(event_payload.get("predicate"))
    if not source_id or not predicate:
        return [], []
    affecting_subject_predicates = set(
        policy.matching_string_tuple("affecting_subject_relationship_predicates")
    )
    if predicate in affecting_subject_predicates:
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
    candidate_limit: int | None = None,
    max_results: int | None = None,
    trigger_source: str | None = None,
    discover_subjects_if_missing: bool = False,
) -> dict[str, Any]:
    """Resolve affected subjects/candidate papers from an event and refresh them."""

    payload = dict(event_payload or {})
    policy = resolve_paper_recommendation_policy()
    resolved_subject_ids = _normalise_subject_ids(target_subject_concept_ids)
    resolved_candidate_ids = _normalise_candidate_ids(candidate_paper_concept_ids)
    event_type = _safe_str(payload.get("event_type"))

    if not resolved_subject_ids and event_type in {
        EVENT_TYPE_SCOPED_ASSERTION_RETRACTED,
        EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
        EVENT_TYPE_TEXT_RELATION_UPSERTED,
        EVENT_TYPE_TEXT_RELATION_UPDATED,
    }:
        event_subjects, event_candidates = _impacted_subjects_from_text_mutation(
            payload,
            policy=policy,
        )
        resolved_subject_ids = event_subjects
        if not resolved_candidate_ids:
            resolved_candidate_ids = event_candidates
    if not resolved_subject_ids and event_type in {
        EVENT_TYPE_RELATIONSHIP_ADDED,
        EVENT_TYPE_RELATIONSHIP_REMOVED,
    }:
        event_subjects, event_candidates = _impacted_subjects_from_relationship_mutation(
            payload,
            policy=policy,
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
            # These fields are part of the represented workflow's declared
            # output envelope even for a successful no-op. Omitting them made
            # the durable step fail after the tool had correctly decided that
            # no subject was affected.
            "reports": [],
            "subject_concept_ids": [],
            "candidate_paper_concept_ids": [],
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
            trigger_source=trigger_source or event_type or policy.delivery_trigger_source_default,
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
        "policy": dict(policy.diagnostics),
        "reports": reports,
    }


__all__ = [
    "materialise_paper_recommendations_for_subject",
    "materialise_paper_recommendations_from_event",
]
