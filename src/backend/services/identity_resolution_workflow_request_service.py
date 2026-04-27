"""Identity-resolution workflow request helpers.

This module is intentionally a thin support surface that maps a paper-ingest
identity-resolution request into the canonical event-driven workflow launch
path.  The actual routing decision (which durable workflow handles the
``identity_resolution.requested`` event) is governed by persisted
:class:`EventWorkflowBinding` rows registered via
``identity_resolution_schedule_bootstrap_service.ensure_identity_resolution_event_bindings``,
not by a hard-coded constant in this file.

The module follows the doctrine in ``AGENTS.md`` (workflow-first /
KB-authoritative) and JVNAUTOSCI-2150 phase 1: code here is responsible only
for shaping the event payload/inputs and delegating to
``launch_event_workflow``.  Changing which workflow handles this event must be
done in Vontology / event-binding state, not by editing Python.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE = "identity_resolution.requested"

# The canonical workflow ID is exported only as a documentation anchor and for
# the *separate* schedule-bootstrap path (see
# ``identity_resolution_schedule_bootstrap_service``).  The launch path in this
# module deliberately does NOT consult this constant; it uses the event-binding
# mechanism so routing remains a Vontology-authoritative decision.
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
    """Launch the identity-resolution workflow for candidate concepts.

    Routing is performed via the persistent event-binding registry by
    ``launch_event_workflow``.  The optional ``workflow_id`` argument is
    preserved for explicit overrides (mirroring ``launch_event_workflow``'s
    own override semantics) but the default path resolves bindings from
    Vontology-authoritative state.
    """

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
            "hint": (
                "Identity resolution request needs a paper concept and at "
                "least one candidate concept."
            ),
        }

    names = _normalise_string_sequence(candidate_names)
    resolved_event_id = _build_event_id(
        event_id=event_id,
        source=source,
        paper_id=paper_id,
        candidate_ids=candidate_ids,
    )

    payload: dict[str, Any] = dict(event_payload or {})
    payload.setdefault("paper_concept_id", paper_id)
    payload.setdefault("candidate_concept_ids", candidate_ids)
    payload.setdefault("candidate_names", names)
    payload.setdefault("author_concept_ids", candidate_ids)
    payload.setdefault("author_names", names)
    payload.setdefault("trigger_source", source)

    inputs: dict[str, Any] = dict(payload)
    inputs.setdefault("paper_concept_id", paper_id)
    inputs.setdefault("candidate_concept_ids", candidate_ids)
    inputs.setdefault("candidate_names", names)
    inputs.setdefault("author_concept_ids", candidate_ids)
    inputs.setdefault("author_names", names)
    inputs.setdefault("trigger_source", source)

    from .workflow_event_integration_service import launch_event_workflow

    explicit_workflow_id = (
        str(workflow_id).strip()
        if isinstance(workflow_id, str) and workflow_id.strip()
        else None
    )

    launch_result = launch_event_workflow(
        event_type=IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        event_id=resolved_event_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        inputs=inputs,
        workflow_id=explicit_workflow_id,
        event_payload=payload,
    )

    selected_workflow_id = (
        str(
            launch_result.get("selected_workflow_id")
            or launch_result.get("workflow_id")
            or ""
        ).strip()
        or None
    )

    verification = launch_result.get("verification")
    return {
        "success": bool(launch_result.get("success", False)),
        "triggered": bool(launch_result.get("triggered", False)),
        "outcome": str(launch_result.get("outcome") or "not_triggered"),
        "reason": str(launch_result.get("reason") or "not_triggered"),
        "workflow_id": selected_workflow_id,
        "selected_workflow_id": selected_workflow_id,
        "event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        "event_id": resolved_event_id,
        "source_event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        "source_event_id": resolved_event_id,
        "event_idempotency_key": launch_result.get("idempotency_key") or resolved_event_id,
        "payload": payload,
        "inputs": inputs,
        "status": launch_result.get("submission_status"),
        "instance_id": launch_result.get("instance_id"),
        "error_code": launch_result.get("error_code"),
        "error": launch_result.get("error"),
        "verification": dict(verification) if isinstance(verification, Mapping) else None,
        "hint": launch_result.get("hint"),
        "binding_id": launch_result.get("binding_id"),
        "binding_source": launch_result.get("binding_source"),
        "launch_strategy": launch_result.get("launch_strategy"),
        "launches": launch_result.get("launches"),
        "launch_count": launch_result.get("launch_count"),
        "idempotent_reused": bool(launch_result.get("idempotent_reused", False)),
        "cadence_policy": launch_result.get("cadence_policy"),
        "cadence_policy_source": launch_result.get("cadence_policy_source"),
        "launch_check_timings_ms": launch_result.get("launch_check_timings_ms"),
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

    event_id = (
        str((event_payload or {}).get("event_id") or "").strip() if event_payload else None
    )
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
