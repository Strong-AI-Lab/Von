"""Deliver newly active paper recommendations as direct Von messages."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from .coding_agent_identity_bootstrap_service import VON_SYSTEM_ID
from .message_service import create_message
from .paper_recommendation_constants import (
    PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID,
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
)
from .paper_recommendation_vontology_service import (
    list_subject_concept_ids_with_paper_matching_profiles,
    load_materialised_paper_recommendations,
)
from .relationship_write_service import add_relationship
from .workflow_prompt_authority_service import (
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
)
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_RECOMMENDATIONS_PER_MESSAGE = 3
_MAX_DELIVERY_PROMPT_CHARS = 12000
_RESEARCHER_TYPE_ID = "#V#researcher"
_VON_USER_TYPE_ID = "#V#von_user"
_ORGANISATION_PREDICATE_ID = "#V#member_of_organisation"


def _safe_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalise_concept_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = _safe_str(value)
        return [cleaned] if cleaned else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _safe_str(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        rows.append(cleaned)
    return rows


def _type_ids_for_concept(concept_doc: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(concept_doc, Mapping):
        return []
    return normalise_relationship_targets(
        (concept_doc.get("relationships") or {}).get("is_an_instance_of")
    )


def _type_is_or_inherits(
    type_id: str,
    *,
    target_type_id: str,
    memo: dict[str, bool] | None = None,
    visiting: set[str] | None = None,
) -> bool:
    cleaned_type_id = _safe_str(type_id)
    cleaned_target_id = _safe_str(target_type_id)
    if not cleaned_type_id or not cleaned_target_id:
        return False
    if cleaned_type_id == cleaned_target_id:
        return True

    cache = memo if isinstance(memo, dict) else {}
    if cleaned_type_id in cache:
        return cache[cleaned_type_id]

    seen = visiting if isinstance(visiting, set) else set()
    if cleaned_type_id in seen:
        cache[cleaned_type_id] = False
        return False
    seen.add(cleaned_type_id)

    concept_doc = load_concept(cleaned_type_id)
    parent_ids = normalise_relationship_targets(
        (concept_doc or {}).get("relationships", {}).get("is_a_type_of")
    )
    for parent_id in parent_ids:
        if _type_is_or_inherits(
            parent_id,
            target_type_id=cleaned_target_id,
            memo=cache,
            visiting=seen,
        ):
            cache[cleaned_type_id] = True
            return True

    cache[cleaned_type_id] = False
    return False


def is_paper_recommendation_delivery_subject(
    subject_concept_id: str,
    *,
    concept_doc: Mapping[str, Any] | None = None,
    user_type_memo: dict[str, bool] | None = None,
    researcher_type_memo: dict[str, bool] | None = None,
) -> bool:
    """Return True for Von users that are researchers (directly or by subtype)."""

    subject_doc = concept_doc if isinstance(concept_doc, Mapping) else load_concept(subject_concept_id)
    type_ids = _type_ids_for_concept(subject_doc)
    effective_user_type_memo = (
        user_type_memo if isinstance(user_type_memo, dict) else {}
    )
    if not any(
        _type_is_or_inherits(
            type_id,
            target_type_id=_VON_USER_TYPE_ID,
            memo=effective_user_type_memo,
        )
        for type_id in type_ids
    ):
        return False

    effective_researcher_type_memo = (
        researcher_type_memo if isinstance(researcher_type_memo, dict) else {}
    )
    return any(
        _type_is_or_inherits(
            type_id,
            target_type_id=_RESEARCHER_TYPE_ID,
            memo=effective_researcher_type_memo,
        )
        for type_id in type_ids
    )


def list_paper_recommendation_delivery_subject_ids(*, limit: int = 200) -> list[str]:
    """List researcher-user subjects eligible for recommendation delivery."""

    safe_limit = max(1, int(limit))
    subject_cursor = ConceptsRepository.find(
        {"relationships.is_an_instance_of": _VON_USER_TYPE_ID},
        projection={"concept_id": 1, "relationships.is_an_instance_of": 1},
        limit=max(200, safe_limit * 20),
    )

    rows: list[str] = []
    seen: set[str] = set()
    user_type_memo: dict[str, bool] = {}
    researcher_type_memo: dict[str, bool] = {}
    for row in subject_cursor:
        if not isinstance(row, Mapping):
            continue
        subject_id = _safe_str(row.get("concept_id"))
        if not subject_id or subject_id in seen:
            continue
        seen.add(subject_id)
        if is_paper_recommendation_delivery_subject(
            subject_id,
            concept_doc=row,
            user_type_memo=user_type_memo,
            researcher_type_memo=researcher_type_memo,
        ):
            rows.append(subject_id)
        if len(rows) >= safe_limit:
            return rows

    subject_ids = list_subject_concept_ids_with_paper_matching_profiles(
        limit=safe_limit * 4
    )
    for subject_id in subject_ids:
        if subject_id in seen:
            continue
        seen.add(subject_id)
        if is_paper_recommendation_delivery_subject(
            subject_id,
            user_type_memo=user_type_memo,
            researcher_type_memo=researcher_type_memo,
        ):
            rows.append(subject_id)
        if len(rows) >= safe_limit:
            break
    return rows


def _subject_display_name(subject_id: str, subject_doc: Mapping[str, Any] | None) -> str:
    return _safe_str((subject_doc or {}).get("name")) or subject_id


def _primary_org_id(subject_doc: Mapping[str, Any] | None) -> str | None:
    org_ids = normalise_relationship_targets(
        (subject_doc or {}).get("relationships", {}).get(_ORGANISATION_PREDICATE_ID)
    )
    return org_ids[0] if org_ids else None


def _resolve_delivery_subject_ids(
    *,
    refresh_reports: Sequence[Mapping[str, Any]] | None,
    subject_concept_ids: Sequence[str] | None,
) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for subject_id in _normalise_concept_id_list(subject_concept_ids):
        if subject_id not in seen:
            seen.add(subject_id)
            rows.append(subject_id)
    for report in refresh_reports or ():
        if not isinstance(report, Mapping):
            continue
        subject_id = _safe_str(report.get("subject_concept_id"))
        if not subject_id or subject_id in seen:
            continue
        seen.add(subject_id)
        rows.append(subject_id)
    return rows


def _format_recommendation_block(index: int, recommendation: Mapping[str, Any]) -> str:
    paper_title = _safe_str(recommendation.get("paper_title")) or _safe_str(
        recommendation.get("paper_concept_id")
    )
    rationale_summary = _safe_str(recommendation.get("rationale_summary"))
    paper_representation = (
        recommendation.get("evaluation", {}).get("paper_representation")
        if isinstance(recommendation.get("evaluation"), Mapping)
        else None
    )
    if not isinstance(paper_representation, Mapping):
        paper_representation = {}
    publication_date = _safe_str(paper_representation.get("publication_date"))
    author_names = ", ".join(
        _normalise_concept_id_list(paper_representation.get("author_names"))
    )
    topic_labels = ", ".join(
        _normalise_concept_id_list(paper_representation.get("topic_labels"))
    )
    score = float(recommendation.get("score") or 0.0)
    paper_concept_id = _safe_str(recommendation.get("paper_concept_id"))

    lines = [f"{index}. {paper_title}"]
    if rationale_summary:
        lines.append(f"Why it looks relevant: {rationale_summary}")
    lines.append(f"Recommendation score: {score:.2f}")
    if author_names:
        lines.append(f"Authors: {author_names}")
    if topic_labels:
        lines.append(f"Topics: {topic_labels}")
    if publication_date:
        lines.append(f"Publication date: {publication_date}")
    if paper_concept_id:
        lines.append(f"Paper concept ID: {paper_concept_id}")
    return "\n".join(lines)


def _render_delivery_message(
    *,
    recipient_display_name: str,
    recommendations: Sequence[Mapping[str, Any]],
    trigger_source: str | None,
) -> tuple[str | None, dict[str, Any]]:
    recommendation_count = len(recommendations)
    recommendation_items = "\n\n".join(
        _format_recommendation_block(index, recommendation)
        for index, recommendation in enumerate(recommendations, start=1)
    )
    resolved_prompt_id = resolve_linked_prompt_concept_id(
        workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
        prompt_concept_id=None,
        predicates=(PAPER_RECOMMENDATION_DELIVERY_PROMPT_LINK_PREDICATE_ID,),
        default_prompt_concept_id=PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables={
            "recipient_display_name": recipient_display_name,
            "recommendation_count": recommendation_count,
            "recommendation_noun": (
                "paper recommendation" if recommendation_count == 1 else "paper recommendations"
            ),
            "recommendation_items": recommendation_items,
            "review_hint": (
                "Review these in Settings -> Paper Recommendations, where you can also "
                "record feedback about usefulness and explanation quality."
            ),
            "trigger_source": _safe_str(trigger_source) or "paper_recommendation_refresh",
        },
        max_chars=_MAX_DELIVERY_PROMPT_CHARS,
        error_prefix="paper_recommendation_delivery_message",
    )
    if rendered is None:
        return None, diagnostics
    return rendered.text, diagnostics


def deliver_paper_recommendation_messages(
    *,
    refresh_reports: Sequence[Mapping[str, Any]] | None = None,
    subject_concept_ids: Sequence[str] | None = None,
    trigger_source: str | None = None,
    max_recommendations_per_message: int = DEFAULT_MAX_RECOMMENDATIONS_PER_MESSAGE,
) -> dict[str, Any]:
    """Create direct Von messages for newly active materialised recommendations."""

    resolved_subject_ids = _resolve_delivery_subject_ids(
        refresh_reports=refresh_reports,
        subject_concept_ids=subject_concept_ids,
    )
    if not resolved_subject_ids:
        return {
            "success": True,
            "triggered": False,
            "reason": "no_subjects_to_deliver",
            "delivered_message_count": 0,
            "delivered_assertion_count": 0,
            "delivered_message_ids": [],
            "delivered_subject_concept_ids": [],
            "subject_reports": [],
        }

    safe_max_per_message = max(1, min(int(max_recommendations_per_message or 1), 10))
    delivered_message_ids: list[str] = []
    delivered_subject_ids: list[str] = []
    subject_reports: list[dict[str, Any]] = []
    delivered_assertion_count = 0
    errors: list[str] = []

    for subject_id in resolved_subject_ids:
        subject_doc = load_concept(subject_id)
        if not is_paper_recommendation_delivery_subject(subject_id, concept_doc=subject_doc):
            subject_reports.append(
                {
                    "subject_concept_id": subject_id,
                    "triggered": False,
                    "reason": "subject_not_researcher_user",
                }
            )
            continue

        recommendation_payload = load_materialised_paper_recommendations(
            subject_concept_id=subject_id,
            include_inactive=False,
            limit=max(10, safe_max_per_message * 4),
        )
        if not recommendation_payload.get("success"):
            error = _safe_str(recommendation_payload.get("error")) or "recommendation_load_failed"
            errors.append(f"{subject_id}:{error}")
            subject_reports.append(
                {
                    "subject_concept_id": subject_id,
                    "triggered": False,
                    "reason": error,
                }
            )
            continue

        recommendations = [
            row
            for row in (recommendation_payload.get("recommendations") or [])
            if isinstance(row, Mapping)
            and bool(row.get("active"))
            and _safe_str(row.get("assertion_concept_id"))
            and not _normalise_concept_id_list(row.get("delivered_message_ids"))
        ][:safe_max_per_message]
        if not recommendations:
            subject_reports.append(
                {
                    "subject_concept_id": subject_id,
                    "triggered": False,
                    "reason": "no_new_active_recommendations",
                }
            )
            continue

        message_body, prompt_diagnostics = _render_delivery_message(
            recipient_display_name=_subject_display_name(subject_id, subject_doc),
            recommendations=recommendations,
            trigger_source=trigger_source,
        )
        if not message_body:
            error = _safe_str(prompt_diagnostics.get("error")) or "delivery_prompt_unavailable"
            errors.append(f"{subject_id}:{error}")
            subject_reports.append(
                {
                    "subject_concept_id": subject_id,
                    "triggered": False,
                    "reason": error,
                    "prompt_diagnostics": prompt_diagnostics,
                }
            )
            continue

        message_subject = (
            "New paper recommendation from Von"
            if len(recommendations) == 1
            else "New paper recommendations from Von"
        )
        assertion_ids = [
            _safe_str(row.get("assertion_concept_id"))
            for row in recommendations
            if _safe_str(row.get("assertion_concept_id"))
        ]
        paper_ids = [
            _safe_str(row.get("paper_concept_id"))
            for row in recommendations
            if _safe_str(row.get("paper_concept_id"))
        ]
        message_doc = create_message(
            sender_id=VON_SYSTEM_ID,
            recipient_ids=[subject_id],
            content=message_body,
            subject=message_subject,
            org_id=_primary_org_id(subject_doc),
            metadata={
                "attribution": "Sent by Von",
                "delivery_channel": "paper_recommendation_message",
                "intent": "paper_recommendation",
                "trigger_source": _safe_str(trigger_source) or "paper_recommendation_refresh",
                "recommendation_assertion_ids": assertion_ids,
                "recommendation_paper_concept_ids": paper_ids,
                "recommendation_count": len(recommendations),
            },
        )
        message_concept_id = _safe_str(message_doc.get("concept_id"))
        if not message_concept_id:
            errors.append(f"{subject_id}:message_create_failed")
            subject_reports.append(
                {
                    "subject_concept_id": subject_id,
                    "triggered": False,
                    "reason": "message_create_failed",
                }
            )
            continue

        for assertion_id in assertion_ids:
            try:
                add_relationship(
                    source_id=assertion_id,
                    predicate=PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID,
                    target=message_concept_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to link paper recommendation assertion %s to message %s: %s",
                    assertion_id,
                    message_concept_id,
                    exc,
                )
                errors.append(f"{assertion_id}:delivery_link_failed:{exc}")

        delivered_message_ids.append(message_concept_id)
        delivered_subject_ids.append(subject_id)
        delivered_assertion_count += len(assertion_ids)
        subject_reports.append(
            {
                "subject_concept_id": subject_id,
                "triggered": True,
                "message_concept_id": message_concept_id,
                "assertion_concept_ids": assertion_ids,
                "paper_concept_ids": paper_ids,
                "prompt_diagnostics": prompt_diagnostics,
            }
        )

    return {
        "success": not errors,
        "triggered": bool(delivered_message_ids),
        "reason": None if delivered_message_ids else "no_messages_created",
        "delivered_message_count": len(delivered_message_ids),
        "delivered_assertion_count": delivered_assertion_count,
        "delivered_message_ids": delivered_message_ids,
        "delivered_subject_concept_ids": delivered_subject_ids,
        "subject_reports": subject_reports,
        "errors": errors,
    }


__all__ = [
    "DEFAULT_MAX_RECOMMENDATIONS_PER_MESSAGE",
    "deliver_paper_recommendation_messages",
    "is_paper_recommendation_delivery_subject",
    "list_paper_recommendation_delivery_subject_ids",
]
