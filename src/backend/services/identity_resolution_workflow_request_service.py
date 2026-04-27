"""Identity-resolution workflow request helpers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE = "identity_resolution.requested"
ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID = "#V#entity_identity_resolution_workflow"


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


def _build_event_id(
    *,
    event_id: str | None,
    source: str,
    paper_id: str,
    candidate_ids: list[str],
) -> str:
    explicit_id = str(event_id or "").strip()
    if explicit_id:
        return explicit_id
    signature = ",".join(candidate_ids)
    if signature:
        return f"{source}:{paper_id}:{signature}"
    return f"{source}:{paper_id}:none"


def _submission_outcome(
    submission: Any,
) -> tuple[bool, str, str]:
    if not bool(getattr(submission, "success", False)):
        return (
            False,
            "not_triggered",
            str(getattr(submission, "error_code", "submission_failed") or "submission_failed"),
        )
    if bool(getattr(submission, "created_new", False)):
        return True, "triggered", "created_new_instance"
    if str(getattr(submission, "status", "")) == "reused":
        return False, "reused", "idempotent_reuse"
    return False, "not_triggered", "not_triggered"


def request_identity_resolution_for_candidate_concepts(
    *,
    candidate_concept_ids: Sequence[Any],
    paper_concept_id: str,
    trigger_source: str,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
    candidate_names: Sequence[Any] | None = None,
    workflow_id: str | None = None,
    event_payload: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Launch identity-resolution workflow for candidate concepts."""

    candidate_ids = _normalise_string_sequence(candidate_concept_ids)
    paper_id = str(paper_concept_id or "").strip()
    source = str(trigger_source or "").strip() or "identity_resolution_request"
    if not candidate_ids or not paper_id:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
            "reason": "missing_identity_resolution_context",
            "hint": "Identity resolution request needs a paper concept and at least one candidate concept.",
        }

    names = _normalise_string_sequence(candidate_names)
    selected_workflow_id = str(
        workflow_id or ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    ).strip()
    resolved_event_id = _build_event_id(
        event_id=event_id,
        source=source,
        paper_id=paper_id,
        candidate_ids=candidate_ids,
    )

    payload = dict(event_payload or {})
    payload.setdefault("paper_concept_id", paper_id)
    payload.setdefault("candidate_concept_ids", candidate_ids)
    payload.setdefault("candidate_names", names)
    payload.setdefault("author_concept_ids", candidate_ids)
    payload.setdefault("author_names", names)
    payload.setdefault("trigger_source", source)

    inputs = dict(payload)
    inputs.setdefault("paper_concept_id", paper_id)
    inputs.setdefault("candidate_concept_ids", candidate_ids)
    inputs.setdefault("candidate_names", names)
    inputs.setdefault("author_concept_ids", candidate_ids)
    inputs.setdefault("author_names", names)
    inputs.setdefault("trigger_source", source)

    from ..workflows.durable import WorkflowInstanceManager
    from ..workflows.durable.workflow_instance_submission_service import (
        submit_verified_workflow_instance,
    )

    submission = submit_verified_workflow_instance(
        manager=WorkflowInstanceManager(),
        workflow_id=selected_workflow_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        inputs=inputs,
        source_event_type=IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        source_event_id=resolved_event_id,
        event_idempotency_key=resolved_event_id,
    )

    triggered, outcome, reason = _submission_outcome(submission)
    error_code = getattr(submission, "error_code", None)
    return {
        "success": bool(getattr(submission, "success", False)),
        "triggered": bool(triggered),
        "outcome": outcome,
        "reason": reason,
        "workflow_id": selected_workflow_id,
        "selected_workflow_id": selected_workflow_id,
        "event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        "event_id": resolved_event_id,
        "source_event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        "source_event_id": resolved_event_id,
        "event_idempotency_key": resolved_event_id,
        "payload": payload,
        "inputs": inputs,
        "status": getattr(submission, "status", None),
        "instance_id": getattr(submission, "instance_id", None),
        "error_code": error_code,
        "error": getattr(submission, "error", None),
        "verification": (
            dict(getattr(submission, "verification", {}))
            if isinstance(getattr(submission, "verification", {}), Mapping)
            else None
        ),
        "hint": "created_new_instance" if triggered else error_code,
    }


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
    """Launch identity resolution after scholarly author materialisation."""

    event_id = str((event_payload or {}).get("event_id") or "").strip() if event_payload else None
    return request_identity_resolution_for_candidate_concepts(
        candidate_concept_ids=author_concept_ids,
        paper_concept_id=paper_concept_id,
        trigger_source=trigger_source,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        candidate_names=author_names,
        event_payload=event_payload,
        event_id=event_id,
    )


__all__ = [
    "IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE",
    "ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID",
    "request_identity_resolution_for_candidate_concepts",
    "request_identity_resolution_for_materialised_scholarly_authors",
]
