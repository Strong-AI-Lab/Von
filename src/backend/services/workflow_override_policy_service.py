"""Routing-time policy for launchability-based custom workflow replacement.

This module is intentionally narrow. It must not infer prompt meaning,
authoring intent, or lexical fit from free text. The selector/LLM owns
semantic workflow choice. Python may only help choose a replacement when:

- a custom workflow was already selected or discovered,
- launchability is known from workflow/runtime checks, and
- workflow-authored metadata provides explicit policy signals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

_ROLE_EXECUTION = "execution"
_ROLE_AUTHORING = "authoring"
_ROLE_MAINTENANCE = "maintenance"
_ROLE_UNKNOWN = "unknown"

_ROLE_SYNONYMS = {
    "execution": _ROLE_EXECUTION,
    "executor": _ROLE_EXECUTION,
    "task_execution": _ROLE_EXECUTION,
    "authoring": _ROLE_AUTHORING,
    "author": _ROLE_AUTHORING,
    "workflow_authoring": _ROLE_AUTHORING,
    "workflow_creation": _ROLE_AUTHORING,
    "creation": _ROLE_AUTHORING,
    "maintenance": _ROLE_MAINTENANCE,
    "maintainer": _ROLE_MAINTENANCE,
    "repair": _ROLE_MAINTENANCE,
    "testing": _ROLE_MAINTENANCE,
    "analysis": _ROLE_MAINTENANCE,
    "meta": _ROLE_MAINTENANCE,
}


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _normalise_role(value: Any) -> str | None:
    clean = _safe_text(value).lower().replace("-", "_")
    if not clean:
        return None
    return _ROLE_SYNONYMS.get(clean)


def _normalise_profile(profile: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(profile, Mapping):
        return None, "none"

    role = _normalise_role(profile.get("role") or profile.get("workflow_role"))
    if role is None:
        return None, "invalid"

    authoring_intent_required = _coerce_bool(
        profile.get("authoring_intent_required")
    )
    prefer_existing_capability = _coerce_bool(
        profile.get("prefer_existing_capability")
    )
    if authoring_intent_required is None:
        authoring_intent_required = role == _ROLE_AUTHORING
    if prefer_existing_capability is None:
        prefer_existing_capability = role == _ROLE_AUTHORING

    return (
        {
            "role": role,
            "authoring_intent_required": bool(authoring_intent_required),
            "prefer_existing_capability": bool(prefer_existing_capability),
        },
        "routing_profile",
    )


def _resolve_candidate_role(candidate: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    profile, profile_source = _normalise_profile(candidate.get("routing_profile"))
    if profile is not None:
        return (
            str(profile["role"]),
            profile_source,
            {
                "authoring_intent_required": bool(
                    profile.get("authoring_intent_required")
                ),
                "prefer_existing_capability": bool(
                    profile.get("prefer_existing_capability")
                ),
            },
        )
    return (
        _ROLE_UNKNOWN,
        "none",
        {
            "authoring_intent_required": False,
            "prefer_existing_capability": False,
        },
    )


@dataclass(frozen=True)
class WorkflowOverrideCandidateAssessment:
    workflow_id: str
    name: str
    role: str
    role_source: str
    launchable: bool
    semantic_fit_score: float
    discovery_score: float
    lexical_score: float
    role_adjustment: float
    override_score: float
    suitable: bool
    suitability_reason: str
    launch_input_resolution_status: str | None = None
    pre_action_reason_code: str | None = None
    policy_flags: Mapping[str, Any] | None = None
    lexical_signals: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowOverrideDecision:
    context: str
    chosen_workflow_id: str | None
    outcome: str
    reason_code: str
    explicit_authoring_request: bool
    explicit_execution_request: bool
    workflow_query_intent: bool
    candidate_assessments: tuple[WorkflowOverrideCandidateAssessment, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidate_assessments"] = [
            assessment.to_dict() for assessment in self.candidate_assessments
        ]
        return payload


def choose_custom_workflow_override_candidate(
    *,
    turn_text: str,
    context: str,
    candidates: Sequence[Mapping[str, Any]],
    launchability_probes: Mapping[str, Mapping[str, Any]],
) -> WorkflowOverrideDecision:
    """Return a launchable replacement candidate without prompt-semantic vetoes."""

    del turn_text
    candidate_assessments: list[WorkflowOverrideCandidateAssessment] = []

    for candidate in candidates:
        workflow_id = _safe_text(candidate.get("concept_id") or candidate.get("workflow_id"))
        if not workflow_id:
            continue

        probe = launchability_probes.get(workflow_id) or {}
        launchable = bool(probe.get("launchable"))
        launch_resolution = probe.get("launch_input_resolution")
        launch_status = (
            _safe_text(launch_resolution.get("status"))
            if isinstance(launch_resolution, Mapping)
            else ""
        )
        pre_action = probe.get("pre_action_validation")
        pre_action_reason = (
            _safe_text(pre_action.get("reason_code"))
            if isinstance(pre_action, Mapping)
            else ""
        )

        name = _safe_text(candidate.get("name")) or workflow_id
        role, role_source, policy_flags = _resolve_candidate_role(candidate)
        discovery_score = max(
            _coerce_float(candidate.get("confidence_score"), default=0.0),
            _coerce_float(candidate.get("relevance_score"), default=0.0),
        )
        semantic_fit_score = round(discovery_score, 3)
        lexical_score = 0.0
        role_adjustment = 0.0
        suitability_reason = "suitable"
        suitable = True

        if not launchable:
            suitable = False
            suitability_reason = "not_launchable"
        elif (
            role == _ROLE_AUTHORING
            and bool(policy_flags.get("authoring_intent_required"))
        ):
            suitable = False
            suitability_reason = "authoring_intent_required_by_workflow_profile"

        candidate_assessments.append(
            WorkflowOverrideCandidateAssessment(
                workflow_id=workflow_id,
                name=name,
                role=role,
                role_source=role_source,
                launchable=launchable,
                semantic_fit_score=semantic_fit_score,
                discovery_score=semantic_fit_score,
                lexical_score=lexical_score,
                role_adjustment=role_adjustment,
                override_score=semantic_fit_score,
                suitable=suitable,
                suitability_reason=suitability_reason,
                launch_input_resolution_status=launch_status or None,
                pre_action_reason_code=pre_action_reason or None,
                policy_flags=dict(policy_flags),
                lexical_signals={},
            )
        )

    candidate_assessments.sort(
        key=lambda item: (
            not item.suitable,
            -item.override_score,
            -item.discovery_score,
            item.workflow_id,
        )
    )

    chosen = next((item for item in candidate_assessments if item.suitable), None)
    if chosen is not None:
        return WorkflowOverrideDecision(
            context=context,
            chosen_workflow_id=chosen.workflow_id,
            outcome="promote",
            reason_code="launchable_custom_workflow_found",
            explicit_authoring_request=False,
            explicit_execution_request=False,
            workflow_query_intent=False,
            candidate_assessments=tuple(candidate_assessments),
        )

    reason_code = "no_suitable_custom_workflow"
    if not candidate_assessments:
        reason_code = "no_custom_workflow_candidates"
    elif not any(item.launchable for item in candidate_assessments):
        reason_code = "no_launchable_custom_workflow"
    elif any(
        item.suitability_reason == "authoring_intent_required_by_workflow_profile"
        for item in candidate_assessments
    ):
        reason_code = "authoring_workflow_profile_requires_explicit_authoring_context"
    return WorkflowOverrideDecision(
        context=context,
        chosen_workflow_id=None,
        outcome="decline",
        reason_code=reason_code,
        explicit_authoring_request=False,
        explicit_execution_request=False,
        workflow_query_intent=False,
        candidate_assessments=tuple(candidate_assessments),
    )


__all__ = [
    "WorkflowOverrideCandidateAssessment",
    "WorkflowOverrideDecision",
    "choose_custom_workflow_override_candidate",
]
