"""Resolve represented memory substrates for the main conversation-turn path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .context_bundle_service import (
    assemble_context_dossier,
    build_reconstructed_workspace,
    list_attached_context_dossier_ids,
    load_context_dossier_state,
    load_workflow_report_revision_state,
    resolve_effective_context,
)
from .episode_critique_memory_service import (
    list_recent_workflow_improvement_suggestions,
)

TURN_MEMORY_CONTEXT_SCHEMA_VERSION = "conversation_turn_memory_context.v1"
SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION = (
    "selected_workflow_policy_memory.v1"
)
_MAX_RECENT_USER_PROMPTS = 3
_MAX_OPEN_QUESTIONS = 3
_MAX_IMMEDIATE_CONTEXT_ITEMS = 4
_MAX_POLICY_SUGGESTIONS = 3


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clone_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_strings(values: Any, *, limit: int | None = None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if limit is not None and len(items) >= limit:
            break
    return items


def _truncate_text(value: Any, *, maximum: int = 240) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    if len(text) <= maximum:
        return text
    return text[:maximum].rstrip() + "..."


def _coerce_turn_memory_context_spec(
    turn_memory_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    spec = _clone_mapping(turn_memory_context)
    if "effective_context_bundle_ids" not in spec and isinstance(
        spec.get("context_bundle_ids"), (list, tuple)
    ):
        spec["effective_context_bundle_ids"] = list(spec.get("context_bundle_ids") or [])
    if "subject_kind" in spec:
        subject_kind = (_safe_str(spec.get("subject_kind")) or "").lower()
        spec["subject_kind"] = subject_kind or None
    return spec


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = _safe_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _select_latest_dossier_id(dossier_ids: Sequence[str]) -> str | None:
    latest_id: str | None = None
    latest_updated_at: datetime | None = None
    for dossier_id in _normalise_strings(dossier_ids):
        state = load_context_dossier_state(dossier_id)
        if not isinstance(state, Mapping):
            continue
        updated_at = _parse_iso_datetime(state.get("updated_at_utc"))
        if latest_id is None:
            latest_id = dossier_id
            latest_updated_at = updated_at
            continue
        if updated_at is not None and (
            latest_updated_at is None or updated_at > latest_updated_at
        ):
            latest_id = dossier_id
            latest_updated_at = updated_at
    return latest_id


def _build_workspace_seed_immediate_context(
    *,
    prompt: str,
    recent_user_prompts: Sequence[str],
    conversation_session_id: str | None,
    subject_role: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "current_turn_prompt": prompt,
        "subject_role": subject_role,
    }
    if isinstance(conversation_session_id, str) and conversation_session_id.strip():
        payload["conversation_session_id"] = conversation_session_id.strip()
    recent_prompts = [
        prompt_text
        for prompt_text in _normalise_strings(
            recent_user_prompts,
            limit=_MAX_RECENT_USER_PROMPTS,
        )
        if prompt_text != prompt
    ]
    if recent_prompts:
        payload["recent_user_prompts"] = recent_prompts
    return payload


def _build_materialised_dossier_name(
    *,
    subject_role: str,
    subject_id: str,
) -> str:
    role_label = subject_role.replace("_", " ").strip() or "turn"
    return f"{role_label.title()} turn context dossier for {subject_id}"


def _resolve_subject_specs(
    *,
    turn_memory_context: Mapping[str, Any] | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    explicit_subject_id = _safe_str(spec.get("subject_id"))
    explicit_subject_kind = (_safe_str(spec.get("subject_kind")) or "concept").lower()
    requested_dossier_id = _safe_str(spec.get("context_dossier_id"))
    requested_report_revision_id = _safe_str(spec.get("report_revision_id"))

    if explicit_subject_id:
        if explicit_subject_kind not in {"concept", "workflow"}:
            return [], {
                "status": "unavailable",
                "failure_reason": "invalid_subject_kind",
                "fail_closed": True,
            }
        return [
            {
                "subject_id": explicit_subject_id,
                "subject_kind": explicit_subject_kind,
                "subject_role": "explicit_subject",
                "is_primary": True,
            }
        ], {}

    if requested_dossier_id:
        dossier_state = load_context_dossier_state(requested_dossier_id)
        if not isinstance(dossier_state, Mapping):
            return [], {
                "status": "unavailable",
                "failure_reason": "requested_context_dossier_missing",
                "fail_closed": True,
            }
        subject_id = _safe_str(dossier_state.get("subject_id"))
        subject_kind = (_safe_str(dossier_state.get("subject_kind")) or "").lower()
        if subject_id and subject_kind in {"concept", "workflow"}:
            return [
                {
                    "subject_id": subject_id,
                    "subject_kind": subject_kind,
                    "subject_role": "dossier_subject",
                    "is_primary": True,
                }
            ], {}
        return [], {
            "status": "unavailable",
            "failure_reason": "requested_context_dossier_subject_missing",
            "fail_closed": True,
        }

    if requested_report_revision_id:
        revision_state = load_workflow_report_revision_state(requested_report_revision_id)
        if not isinstance(revision_state, Mapping):
            return [], {
                "status": "unavailable",
                "failure_reason": "requested_report_revision_missing",
                "fail_closed": True,
            }
        dossier_id = _safe_str(revision_state.get("dossier_id"))
        if dossier_id:
            dossier_state = load_context_dossier_state(dossier_id)
            subject_id = _safe_str((dossier_state or {}).get("subject_id"))
            subject_kind = (_safe_str((dossier_state or {}).get("subject_kind")) or "").lower()
            if subject_id and subject_kind in {"concept", "workflow"}:
                return [
                    {
                        "subject_id": subject_id,
                        "subject_kind": subject_kind,
                        "subject_role": "report_revision_subject",
                        "is_primary": True,
                    }
                ], {}
        return [], {
            "status": "unavailable",
            "failure_reason": "requested_report_revision_subject_missing",
            "fail_closed": True,
        }

    subjects: list[dict[str, Any]] = []
    if isinstance(org_concept_id, str) and org_concept_id.strip():
        subjects.append(
            {
                "subject_id": org_concept_id.strip(),
                "subject_kind": "concept",
                "subject_role": "organisation",
                "is_primary": True,
            }
        )
    if (
        isinstance(user_concept_id, str)
        and user_concept_id.strip()
        and user_concept_id.strip() != (org_concept_id or "").strip()
    ):
        subjects.append(
            {
                "subject_id": user_concept_id.strip(),
                "subject_kind": "concept",
                "subject_role": "user",
                "is_primary": not subjects,
            }
        )
    return subjects, {}


def _resolve_subject_memory_context(
    *,
    subject_spec: Mapping[str, Any],
    turn_memory_context: Mapping[str, Any] | None,
    prompt: str,
    recent_user_prompts: Sequence[str],
    conversation_session_id: str | None,
    namespace: str | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
) -> dict[str, Any]:
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    subject_id = _safe_str(subject_spec.get("subject_id"))
    subject_kind = (_safe_str(subject_spec.get("subject_kind")) or "").lower()
    subject_role = _safe_str(subject_spec.get("subject_role")) or "subject"
    is_primary = bool(subject_spec.get("is_primary"))
    strict = _coerce_bool(spec.get("strict"), default=False)
    apply_requested_state = is_primary

    requested_bundle_ids = (
        _normalise_strings(spec.get("effective_context_bundle_ids"))
        if apply_requested_state
        else []
    )
    requested_dossier_id = (
        _safe_str(spec.get("context_dossier_id")) if apply_requested_state else None
    )
    requested_report_revision_id = (
        _safe_str(spec.get("report_revision_id")) if apply_requested_state else None
    )
    materialise_context_dossier = bool(
        apply_requested_state
        and _coerce_bool(spec.get("materialise_context_dossier"), default=False)
    )
    materialise_report_revision = bool(
        apply_requested_state
        and _coerce_bool(spec.get("materialise_report_revision"), default=False)
    )
    build_workspace_requested = bool(
        apply_requested_state
        and _coerce_bool(spec.get("build_reconstructed_workspace"), default=False)
    )
    fail_closed = strict or bool(
        requested_bundle_ids
        or requested_dossier_id
        or requested_report_revision_id
        or materialise_context_dossier
    )

    if subject_kind not in {"concept", "workflow"} or not subject_id:
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable" if fail_closed else "none",
            "fail_closed": fail_closed,
            "failure_reason": (
                "subject_kind_and_subject_id_required" if fail_closed else None
            ),
        }

    resolution = resolve_effective_context(
        subject_kind=subject_kind,
        subject_id=subject_id,
        explicit_bundle_ids=requested_bundle_ids,
    )
    if not bool(resolution.get("success")):
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable" if fail_closed else "none",
            "fail_closed": fail_closed,
            "failure_reason": _safe_str(resolution.get("error"))
            or "context_bundle_resolution_failed",
        }

    diagnostics = _clone_mapping(resolution.get("diagnostics"))
    effective_context_bundle_ids = _normalise_strings(
        resolution.get("effective_context_bundle_ids")
    )
    effective_context_facet_ids = _normalise_strings(
        resolution.get("effective_context_facet_ids")
    )
    missing_bundle_ids = _normalise_strings(diagnostics.get("missing_bundle_ids"))
    if requested_bundle_ids and missing_bundle_ids:
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_context_bundle_missing",
            "requested_context_bundle_ids": requested_bundle_ids,
            "missing_context_bundle_ids": missing_bundle_ids,
            "context_bundle_resolution": {
                "effective_context_bundle_ids": effective_context_bundle_ids,
                "effective_context_facet_ids": effective_context_facet_ids,
                "diagnostics": diagnostics,
            },
        }

    dossier_id = requested_dossier_id
    if not dossier_id:
        dossier_id = _select_latest_dossier_id(list_attached_context_dossier_ids(subject_id))
    dossier_state = (
        load_context_dossier_state(dossier_id) if isinstance(dossier_id, str) else None
    )
    if requested_dossier_id and not isinstance(dossier_state, Mapping):
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_context_dossier_missing",
            "requested_context_dossier_id": requested_dossier_id,
        }

    report_revision_id = requested_report_revision_id
    report_revision_state = (
        load_workflow_report_revision_state(report_revision_id)
        if isinstance(report_revision_id, str)
        else None
    )
    if requested_report_revision_id and not isinstance(report_revision_state, Mapping):
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_report_revision_missing",
            "requested_report_revision_id": requested_report_revision_id,
        }

    materialised_dossier = False
    if materialise_context_dossier and not isinstance(dossier_state, Mapping):
        dossier_result = assemble_context_dossier(
            name=_build_materialised_dossier_name(
                subject_role=subject_role,
                subject_id=subject_id,
            ),
            subject_kind=subject_kind,
            subject_id=subject_id,
            effective_context_bundle_ids=effective_context_bundle_ids,
            open_questions=(
                "What prior context from the represented bundles matters most for this turn?",
                "Which open questions from this subject should influence the current response?",
            ),
            immediate_context=_build_workspace_seed_immediate_context(
                prompt=prompt,
                recent_user_prompts=recent_user_prompts,
                conversation_session_id=conversation_session_id,
                subject_role=subject_role,
            ),
            report_text=(
                "Initial main-turn memory dossier scaffold."
                if materialise_report_revision
                else None
            ),
            report_title="Main-turn memory dossier scaffold",
            report_summary={
                "subject_id": subject_id,
                "subject_kind": subject_kind,
                "source": "conversation_turn_memory_context_service",
            },
            namespace=namespace,
            user_id=user_concept_id,
            org_id=org_concept_id,
        )
        if not bool(dossier_result.get("success")):
            return {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_role": subject_role,
                "status": "unavailable",
                "fail_closed": True,
                "failure_reason": _safe_str(dossier_result.get("error"))
                or "context_dossier_materialisation_failed",
            }
        dossier_id = _safe_str(dossier_result.get("dossier_id"))
        if dossier_id:
            dossier_state = load_context_dossier_state(dossier_id)
        materialised_dossier = True
        if not requested_report_revision_id:
            report_revision_id = _safe_str(dossier_result.get("report_revision_id"))
            if report_revision_id:
                report_revision_state = load_workflow_report_revision_state(
                    report_revision_id
                )

    if not report_revision_state and isinstance(dossier_state, Mapping):
        report_revision_id = _safe_str(dossier_state.get("latest_report_revision_id"))
        if report_revision_id:
            report_revision_state = load_workflow_report_revision_state(report_revision_id)

    should_build_workspace = bool(
        build_workspace_requested
        or effective_context_bundle_ids
        or isinstance(dossier_state, Mapping)
    )
    reconstructed_workspace = None
    if should_build_workspace:
        workspace_result = build_reconstructed_workspace(
            subject_kind=subject_kind,
            subject_id=subject_id,
            question=prompt,
            task="Provide bounded authoritative turn memory context for the active turn.",
            dossier_id=dossier_id,
            report_revision_id=report_revision_id,
            effective_context_bundle_ids=effective_context_bundle_ids,
            immediate_context=(
                None
                if isinstance(dossier_state, Mapping)
                else _build_workspace_seed_immediate_context(
                    prompt=prompt,
                    recent_user_prompts=recent_user_prompts,
                    conversation_session_id=conversation_session_id,
                    subject_role=subject_role,
                )
            ),
        )
        if bool(workspace_result.get("success")):
            reconstructed_workspace = workspace_result.get("workspace")
        elif fail_closed:
            return {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_role": subject_role,
                "status": "unavailable",
                "fail_closed": True,
                "failure_reason": _safe_str(workspace_result.get("error"))
                or "reconstructed_workspace_unavailable",
            }

    status = "available"
    if not effective_context_bundle_ids and not dossier_state and not reconstructed_workspace:
        status = "none"

    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "subject_role": subject_role,
        "status": status,
        "fail_closed": False,
        "materialised_context_dossier": materialised_dossier,
        "effective_context_bundle_ids": effective_context_bundle_ids,
        "effective_context_facet_ids": effective_context_facet_ids,
        "context_bundle_resolution": {
            "effective_context_bundle_ids": effective_context_bundle_ids,
            "effective_context_facet_ids": effective_context_facet_ids,
            "diagnostics": diagnostics,
        },
        "context_dossier_id": dossier_id,
        "context_dossier": dict(dossier_state) if isinstance(dossier_state, Mapping) else None,
        "report_revision_id": report_revision_id,
        "report_revision": (
            dict(report_revision_state)
            if isinstance(report_revision_state, Mapping)
            else None
        ),
        "reconstructed_workspace": (
            dict(reconstructed_workspace)
            if isinstance(reconstructed_workspace, Mapping)
            else None
        ),
        "workspace_fingerprint": (
            _safe_str((reconstructed_workspace or {}).get("workspace_fingerprint"))
            if isinstance(reconstructed_workspace, Mapping)
            else None
        ),
    }


def build_turn_memory_context_state(
    *,
    prompt: str,
    recent_user_prompts: Sequence[str] = (),
    conversation_session_id: str | None = None,
    user_namespace: str | None = None,
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
    turn_memory_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    subject_specs, resolution_failure = _resolve_subject_specs(
        turn_memory_context=turn_memory_context,
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    if resolution_failure:
        return {
            "schema_version": TURN_MEMORY_CONTEXT_SCHEMA_VERSION,
            "status": "unavailable",
            "fail_closed": bool(resolution_failure.get("fail_closed")),
            "failure_reason": resolution_failure.get("failure_reason"),
            "subject_contexts": [],
            "requested_memory_context": spec,
            "user_namespace": user_namespace,
            "conversation_session_id": conversation_session_id,
        }

    subject_contexts = [
        _resolve_subject_memory_context(
            subject_spec=subject_spec,
            turn_memory_context=spec,
            prompt=prompt,
            recent_user_prompts=recent_user_prompts,
            conversation_session_id=conversation_session_id,
            namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
        )
        for subject_spec in subject_specs
    ]

    fail_closed = False
    failure_reason = None
    for subject_context in subject_contexts:
        if subject_context.get("status") == "unavailable" and bool(
            subject_context.get("fail_closed")
        ):
            fail_closed = True
            failure_reason = _safe_str(subject_context.get("failure_reason"))
            break

    available_subjects = [
        item for item in subject_contexts if item.get("status") == "available"
    ]
    if fail_closed:
        status = "unavailable"
    elif available_subjects:
        status = "available"
    else:
        status = "none"

    return {
        "schema_version": TURN_MEMORY_CONTEXT_SCHEMA_VERSION,
        "status": status,
        "fail_closed": fail_closed,
        "failure_reason": failure_reason,
        "requested_memory_context": spec,
        "user_namespace": user_namespace,
        "conversation_session_id": conversation_session_id,
        "subject_contexts": subject_contexts,
    }


def _render_workspace_lines(workspace: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    workspace_fingerprint = _safe_str(workspace.get("workspace_fingerprint"))
    if workspace_fingerprint:
        lines.append(f"- Workspace fingerprint: {workspace_fingerprint}")
    open_questions = _normalise_strings(
        workspace.get("open_questions"),
        limit=_MAX_OPEN_QUESTIONS,
    )
    if open_questions:
        lines.append("- Open questions: " + " | ".join(open_questions))
    immediate_context = workspace.get("immediate_context")
    if isinstance(immediate_context, Mapping):
        fragments: list[str] = []
        for key, value in list(immediate_context.items())[:_MAX_IMMEDIATE_CONTEXT_ITEMS]:
            key_text = _safe_str(key)
            value_text = _truncate_text(value, maximum=120)
            if key_text and value_text:
                fragments.append(f"{key_text}={value_text}")
        if fragments:
            lines.append("- Immediate context: " + " | ".join(fragments))
    evidence_receipts = workspace.get("evidence_receipts")
    if isinstance(evidence_receipts, Sequence) and not isinstance(
        evidence_receipts, (str, bytes, bytearray)
    ):
        lines.append(f"- Evidence receipts available: {len(list(evidence_receipts))}")
    return lines


def render_turn_memory_context_messages(
    state: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(state, Mapping):
        return []
    if state.get("status") != "available":
        return []

    messages: list[dict[str, str]] = []
    for subject_context in state.get("subject_contexts") or []:
        if not isinstance(subject_context, Mapping):
            continue
        if subject_context.get("status") != "available":
            continue
        role_label = (
            _safe_str(subject_context.get("subject_role")) or "subject"
        ).replace("_", " ")
        subject_kind = _safe_str(subject_context.get("subject_kind")) or "subject"
        subject_id = _safe_str(subject_context.get("subject_id")) or "unknown"
        lines = [
            f"AUTHORITATIVE TURN MEMORY CONTEXT ({role_label.title()}):",
            f"- Subject: {subject_kind} {subject_id}",
        ]
        bundle_ids = _normalise_strings(subject_context.get("effective_context_bundle_ids"))
        if bundle_ids:
            lines.append("- Effective context bundles: " + ", ".join(bundle_ids[:8]))
        dossier_id = _safe_str(subject_context.get("context_dossier_id"))
        if dossier_id:
            lines.append(f"- Context dossier: {dossier_id}")
        report_revision_id = _safe_str(subject_context.get("report_revision_id"))
        if report_revision_id:
            lines.append(f"- Report revision: {report_revision_id}")
        workspace = subject_context.get("reconstructed_workspace")
        if isinstance(workspace, Mapping):
            lines.extend(_render_workspace_lines(workspace))
        lines.append(
            "Use this as represented working context for planning, tool use, and final response construction."
        )
        messages.append({"role": "system", "content": "\n".join(lines)})
    return messages


def summarise_turn_memory_context_for_lineage(
    state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(state, Mapping) or not state:
        return None
    subject_contexts = [
        item
        for item in (state.get("subject_contexts") or [])
        if isinstance(item, Mapping)
    ]
    if not subject_contexts and state.get("status") == "none":
        return {
            "status": "none",
            "subject_count": 0,
        }
    summary = {
        "status": _safe_str(state.get("status")) or "none",
        "fail_closed": bool(state.get("fail_closed")),
        "failure_reason": _safe_str(state.get("failure_reason")),
        "subject_count": len(subject_contexts),
        "available_subject_count": sum(
            1 for item in subject_contexts if item.get("status") == "available"
        ),
        "context_dossier_ids": [
            dossier_id
            for dossier_id in (
                _safe_str(item.get("context_dossier_id")) for item in subject_contexts
            )
            if dossier_id
        ],
        "workspace_fingerprints": [
            fingerprint
            for fingerprint in (
                _safe_str(item.get("workspace_fingerprint")) for item in subject_contexts
            )
            if fingerprint
        ],
    }
    return {key: value for key, value in summary.items() if value not in (None, [], {})}


def build_selected_workflow_policy_memory_state(
    *,
    selected_workflow_id: str | None,
    namespace: str | None = None,
    limit: int = _MAX_POLICY_SUGGESTIONS,
) -> dict[str, Any]:
    workflow_id = _safe_str(selected_workflow_id)
    if not workflow_id:
        return {
            "schema_version": SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION,
            "status": "none",
            "selected_workflow_id": None,
            "suggestions": [],
        }

    suggestions = list_recent_workflow_improvement_suggestions(
        workflow_id,
        namespace=namespace,
        limit=max(1, min(int(limit), _MAX_POLICY_SUGGESTIONS)),
    )
    bounded_suggestions: list[dict[str, Any]] = []
    for item in suggestions[:_MAX_POLICY_SUGGESTIONS]:
        if not isinstance(item, Mapping):
            continue
        bounded_suggestions.append(
            {
                "memory_id": _safe_str(item.get("memory_id")),
                "suggestion_id": _safe_str(item.get("suggestion_id")),
                "category": _safe_str(item.get("category")),
                "priority": _safe_str(item.get("priority")),
                "target_surface": _safe_str(item.get("target_surface")),
                "title": _truncate_text(item.get("title"), maximum=140),
                "rationale": _truncate_text(item.get("rationale"), maximum=220),
                "request_id": _safe_str(item.get("request_id")),
            }
        )

    return {
        "schema_version": SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION,
        "status": "available" if bounded_suggestions else "none",
        "selected_workflow_id": workflow_id,
        "namespace": _safe_str(namespace),
        "suggestion_count": len(bounded_suggestions),
        "suggestions": bounded_suggestions,
    }


def render_selected_workflow_policy_memory_messages(
    state: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(state, Mapping):
        return []
    if state.get("status") != "available":
        return []
    workflow_id = _safe_str(state.get("selected_workflow_id")) or "selected workflow"
    suggestions = [
        item for item in (state.get("suggestions") or []) if isinstance(item, Mapping)
    ]
    if not suggestions:
        return []

    lines = [f"RECENT POLICY MEMORY FOR {workflow_id}:"]
    for item in suggestions[:_MAX_POLICY_SUGGESTIONS]:
        prefix = "/".join(
            segment
            for segment in (
                _safe_str(item.get("priority")),
                _safe_str(item.get("category")),
            )
            if segment
        )
        title = _safe_str(item.get("title")) or "Prior improvement signal"
        rationale = _safe_str(item.get("rationale"))
        if prefix:
            lines.append(f"- [{prefix}] {title}")
        else:
            lines.append(f"- {title}")
        if rationale:
            lines.append(f"- Rationale: {rationale}")
    lines.append(
        "Treat these as prior evaluated improvement signals. They do not override current-turn evidence or workflow authority."
    )
    return [{"role": "system", "content": "\n".join(lines)}]


def summarise_selected_workflow_policy_memory_for_lineage(
    state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(state, Mapping) or not state:
        return None
    summary = {
        "status": _safe_str(state.get("status")) or "none",
        "selected_workflow_id": _safe_str(state.get("selected_workflow_id")),
        "suggestion_count": int(state.get("suggestion_count") or 0),
        "memory_ids": [
            memory_id
            for memory_id in (
                _safe_str(item.get("memory_id"))
                for item in (state.get("suggestions") or [])
                if isinstance(item, Mapping)
            )
            if memory_id
        ],
        "suggestion_ids": [
            suggestion_id
            for suggestion_id in (
                _safe_str(item.get("suggestion_id"))
                for item in (state.get("suggestions") or [])
                if isinstance(item, Mapping)
            )
            if suggestion_id
        ],
    }
    return {key: value for key, value in summary.items() if value not in (None, [], {})}


__all__ = [
    "SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION",
    "TURN_MEMORY_CONTEXT_SCHEMA_VERSION",
    "build_selected_workflow_policy_memory_state",
    "build_turn_memory_context_state",
    "render_selected_workflow_policy_memory_messages",
    "render_turn_memory_context_messages",
    "summarise_selected_workflow_policy_memory_for_lineage",
    "summarise_turn_memory_context_for_lineage",
]
