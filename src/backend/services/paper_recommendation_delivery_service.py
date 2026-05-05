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
from .paper_recommendation_policy_authority_service import (
    PaperRecommendationPolicy,
    resolve_paper_recommendation_policy,
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
    policy: PaperRecommendationPolicy | None = None,
    type_memo_by_target: dict[str, dict[str, bool]] | None = None,
    user_type_memo: dict[str, bool] | None = None,
    researcher_type_memo: dict[str, bool] | None = None,
) -> bool:
    """Return True when the represented delivery profile says a subject is eligible."""

    active_policy = policy or resolve_paper_recommendation_policy()
    subject_doc = concept_doc if isinstance(concept_doc, Mapping) else load_concept(subject_concept_id)
    type_ids = _type_ids_for_concept(subject_doc)
    if not type_ids:
        return False
    memo_by_target = type_memo_by_target if isinstance(type_memo_by_target, dict) else {}
    required_type_ids = active_policy.delivery_required_type_ids
    for required_type_id in required_type_ids:
        memo = memo_by_target.setdefault(required_type_id, {})
        if not any(
            _type_is_or_inherits(
                type_id,
                target_type_id=required_type_id,
                memo=memo,
            )
            for type_id in type_ids
        ):
            return False
    return True


def list_paper_recommendation_delivery_subject_ids(*, limit: int = 200) -> list[str]:
    """List researcher-user subjects eligible for recommendation delivery."""

    policy = resolve_paper_recommendation_policy()
    safe_limit = max(1, int(limit))
    seed_type_id = policy.delivery_candidate_seed_type_id or (
        policy.delivery_required_type_ids[0] if policy.delivery_required_type_ids else ""
    )
    subject_cursor = ConceptsRepository.find(
        {"relationships.is_an_instance_of": seed_type_id},
        projection={"concept_id": 1, "relationships.is_an_instance_of": 1},
        limit=max(200, safe_limit * 20),
    )

    rows: list[str] = []
    seen: set[str] = set()
    type_memo_by_target: dict[str, dict[str, bool]] = {}
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
            policy=policy,
            type_memo_by_target=type_memo_by_target,
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
            policy=policy,
            type_memo_by_target=type_memo_by_target,
        ):
            rows.append(subject_id)
        if len(rows) >= safe_limit:
            break
    return rows


def _subject_display_name(subject_id: str, subject_doc: Mapping[str, Any] | None) -> str:
    return _safe_str((subject_doc or {}).get("name")) or subject_id


def _primary_org_id(
    subject_doc: Mapping[str, Any] | None,
    *,
    policy: PaperRecommendationPolicy,
) -> str | None:
    organisation_predicate_id = policy.delivery_organisation_predicate_id
    if not organisation_predicate_id:
        return None
    org_ids = normalise_relationship_targets(
        (subject_doc or {}).get("relationships", {}).get(organisation_predicate_id)
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


def _user_facing_rationale_text(
    recommendation: Mapping[str, Any],
    *,
    policy: PaperRecommendationPolicy,
) -> str:
    summary = _safe_str(recommendation.get("rationale_summary"))
    if summary:
        return summary
    rationale = _safe_str(recommendation.get("rationale"))
    if rationale:
        return rationale
    raw_evaluation = recommendation.get("evaluation")
    evaluation: Mapping[str, Any]
    if isinstance(raw_evaluation, Mapping):
        evaluation = raw_evaluation
    else:
        evaluation = {}
    evaluation_rationale = _safe_str(evaluation.get("rationale"))
    if evaluation_rationale:
        return evaluation_rationale
    raw_rationale_generation = evaluation.get("rationale_generation")
    rationale_generation: Mapping[str, Any] = (
        raw_rationale_generation
        if isinstance(raw_rationale_generation, Mapping)
        else {}
    )
    if rationale_generation.get("status") == "unavailable":
        return policy.rationale_unavailable_text
    return ""


def _format_recommendation_block(
    index: int,
    recommendation: Mapping[str, Any],
    *,
    policy: PaperRecommendationPolicy,
) -> str:
    paper_title = _safe_str(recommendation.get("paper_title")) or _safe_str(
        recommendation.get("paper_concept_id")
    )
    rationale_summary = _user_facing_rationale_text(recommendation, policy=policy)
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

    field_values = {
        "rationale_text": rationale_summary,
        "score": f"{score:.2f}",
        "author_names": author_names,
        "topic_labels": topic_labels,
        "publication_date": publication_date,
        "paper_concept_id": paper_concept_id,
    }
    lines = [f"{index}. {paper_title}"]
    for field_spec in policy.delivery_item_fields():
        field_name = _safe_str(field_spec.get("field"))
        label = _safe_str(field_spec.get("label"))
        if not field_name or not label:
            continue
        value = _safe_str(field_values.get(field_name))
        if not value and not bool(field_spec.get("include_if_empty")):
            continue
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _render_delivery_message(
    *,
    recipient_display_name: str,
    recommendations: Sequence[Mapping[str, Any]],
    trigger_source: str | None,
    policy: PaperRecommendationPolicy,
) -> tuple[str | None, dict[str, Any]]:
    recommendation_count = len(recommendations)
    recommendation_items = "\n\n".join(
        _format_recommendation_block(index, recommendation, policy=policy)
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
            "recommendation_noun": policy.delivery_recommendation_noun(
                recommendation_count
            ),
            "recommendation_items": recommendation_items,
            "review_hint": policy.delivery_review_hint,
            "trigger_source": _safe_str(trigger_source)
            or policy.delivery_trigger_source_default,
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
    max_recommendations_per_message: int | None = None,
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

    policy = resolve_paper_recommendation_policy()
    safe_max_per_message = max(
        1,
        min(
            int(
                max_recommendations_per_message
                or policy.default_max_recommendations_per_message
            ),
            policy.max_recommendations_per_message,
        ),
    )
    delivered_message_ids: list[str] = []
    delivered_subject_ids: list[str] = []
    subject_reports: list[dict[str, Any]] = []
    delivered_assertion_count = 0
    errors: list[str] = []

    for subject_id in resolved_subject_ids:
        subject_doc = load_concept(subject_id)
        if not is_paper_recommendation_delivery_subject(
            subject_id,
            concept_doc=subject_doc,
            policy=policy,
        ):
            subject_reports.append(
                {
                    "subject_concept_id": subject_id,
                    "triggered": False,
                    "reason": "subject_not_delivery_eligible",
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
            policy=policy,
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

        message_subject = policy.delivery_message_subject(len(recommendations))
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
            org_id=_primary_org_id(subject_doc, policy=policy),
            metadata={
                "attribution": "Sent by Von",
                "delivery_channel": "paper_recommendation_message",
                "intent": "paper_recommendation",
                "trigger_source": _safe_str(trigger_source)
                or policy.delivery_trigger_source_default,
                "paper_recommendation_policy_version": policy.policy_version,
                "paper_recommendation_policy_concept_id": policy.concept_id,
                "recommendation_subject_concept_id": subject_id,
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
        "policy": dict(policy.diagnostics),
        "subject_reports": subject_reports,
        "errors": errors,
    }


__all__ = [
    "DEFAULT_MAX_RECOMMENDATIONS_PER_MESSAGE",
    "deliver_paper_recommendation_messages",
    "is_paper_recommendation_delivery_subject",
    "list_paper_recommendation_delivery_subject_ids",
]
