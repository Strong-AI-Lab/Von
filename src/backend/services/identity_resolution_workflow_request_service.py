"""Event launch helpers for entity identity-resolution workflow requests."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .workflow_event_integration_service import launch_event_workflow

IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE = "identity_resolution.requested"


def _normalise_string_sequence(values: Sequence[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def request_identity_resolution_for_materialised_scholarly_authors(
    *,
    author_concept_ids: Sequence[Any],
    paper_concept_id: str,
    trigger_source: str,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
    author_names: Sequence[Any] | None = None,
    event_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch identity resolution after scholarly author materialisation.

    The request is intentionally policy-light: it gives the existing
    ``#V#entity_identity_resolution_workflow`` concrete event context, but leaves
    duplicate scoring, merge direction, and review thresholds with that workflow.
    """

    author_ids = _normalise_string_sequence(author_concept_ids)
    paper_id = str(paper_concept_id or "").strip()
    source = str(trigger_source or "").strip() or "scholarly_author_materialisation"
    if not author_ids or not paper_id:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
            "reason": "missing_identity_resolution_context",
            "hint": "Identity resolution request needs a paper concept and at least one author concept.",
        }

    names = _normalise_string_sequence(author_names)
    payload = dict(event_payload or {})
    payload.setdefault("paper_concept_id", paper_id)
    payload.setdefault("author_concept_ids", author_ids)
    payload.setdefault("candidate_concept_ids", author_ids)
    payload.setdefault("author_names", names)
    payload.setdefault("trigger_source", source)

    inputs = {
        "trigger_source": source,
        "paper_concept_id": paper_id,
        "author_concept_ids": author_ids,
        "candidate_concept_ids": author_ids,
        "author_names": names,
    }

    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        event_id = f"{source}:{paper_id}:{','.join(author_ids)}"

    return launch_event_workflow(
        event_type=IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        event_id=event_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        event_payload=payload,
        inputs=inputs,
    )


__all__ = [
    "IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE",
    "request_identity_resolution_for_materialised_scholarly_authors",
]
