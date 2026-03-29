"""Routing-time policy for promoting discovered custom workflows.

The selector/discovery stack already finds relevant workflows, but routing-time
override decisions still need a deterministic policy for cases where a
discovered custom workflow might replace a direct-response or tool-pipeline
path. This module keeps that policy centralised so semantic-fit, launchability,
and authoring/meta-role checks evolve together.
"""

from __future__ import annotations

import re
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

_TOKEN_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "show",
        "tell",
        "that",
        "the",
        "there",
        "this",
        "to",
        "use",
        "what",
        "whether",
        "which",
        "workflow",
        "workflows",
    }
)

_AUTHORING_TEXT_HINTS = (
    "workflow creation",
    "create workflow",
    "creates workflow",
    "authoring",
    "author workflow",
    "design structure",
    "verify discoverability",
    "publish workflow",
    "draft workflow",
)
_AUTHORING_ACTION_HINTS = (
    "workflow_authoring.",
    "create_workflow",
    "publish_workflow",
    "verify_discoverability",
)
_MAINTENANCE_TEXT_HINTS = (
    "maintenance",
    "introspection",
    "recovery",
    "repair",
    "regression",
    "benchmark",
    "testing workflow",
    "diagnose",
    "migration",
)
_MAINTENANCE_ACTION_HINTS = (
    "repair",
    "recover",
    "diagnose",
    "test",
    "benchmark",
    "migration",
)

_EXPLICIT_AUTHORING_PATTERNS = (
    re.compile(
        r"\b(create|build|generate|author|design|draft|publish|make)\b"
        r"[\s\w-]{0,48}\bworkflow\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bworkflow\b[\s\w-]{0,32}\b(create|build|generate|author|design)\b",
        re.IGNORECASE,
    ),
)
_EXPLICIT_EXECUTION_PATTERNS = (
    re.compile(
        r"\b(run|execute|launch)\b[\s\w-]{0,64}\bworkflow\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(run|execute)\b[\s\w-]{0,64}\b(test|verification|benchmark)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(test|verify|benchmark|validate)\b[\s\w-]{0,64}\b("
        r"workflow|ingestion|representation|invitation|fixture|experiment|"
        r"regression|cleanup|provenance"
        r")\b",
        re.IGNORECASE,
    ),
)
_WORKFLOW_QUERY_PATTERNS = (
    re.compile(
        r"\b(is|are|does|do|what|which|whether|can|could|should|would)\b"
        r"[\s\w-]{0,48}\bworkflow\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bworkflow\b[\s\w-]{0,24}\b(exist|exists|available|availability|have)\b",
        re.IGNORECASE,
    ),
)


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
        "profile",
    )


def _filtered_tokens(value: Any, *, limit: int = 32) -> tuple[str, ...]:
    text = _safe_text(value).lower()
    if not text:
        return ()

    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", text):
        if len(token) < 3 or token in _TOKEN_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tuple(tokens)


def _query_phrases(value: Any, *, limit: int = 12) -> tuple[str, ...]:
    tokens = list(_filtered_tokens(value, limit=18))
    if len(tokens) < 2:
        return ()

    phrases: list[str] = []
    seen: set[str] = set()
    for width in (3, 2):
        if len(tokens) < width:
            continue
        for index in range(len(tokens) - width + 1):
            phrase = " ".join(tokens[index : index + width])
            if phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
            if len(phrases) >= limit:
                return tuple(phrases)
    return tuple(phrases)


def _compute_lexical_fit(
    *,
    turn_text: str,
    label_text: str,
    description_text: str,
) -> tuple[float, dict[str, Any]]:
    query_tokens = set(_filtered_tokens(turn_text, limit=24))
    if not query_tokens:
        return 0.0, {"label_overlap": (), "description_overlap": (), "phrase_hits": ()}

    label_tokens = set(_filtered_tokens(label_text, limit=18))
    description_tokens = set(_filtered_tokens(description_text, limit=36))
    label_overlap = tuple(sorted(query_tokens & label_tokens))
    description_overlap = tuple(
        sorted((query_tokens & description_tokens) - set(label_overlap))
    )

    normalised_candidate_text = " ".join(
        part for part in (_safe_text(label_text), _safe_text(description_text)) if part
    ).lower()
    phrase_hits = tuple(
        phrase for phrase in _query_phrases(turn_text) if phrase in normalised_candidate_text
    )

    score = min(
        1.0,
        len(label_overlap) * 0.22
        + len(description_overlap) * 0.08
        + len(phrase_hits) * 0.26,
    )
    return (
        round(score, 3),
        {
            "label_overlap": label_overlap,
            "description_overlap": description_overlap,
            "phrase_hits": phrase_hits,
        },
    )


def _turn_authoring_intent(turn_text: str) -> tuple[bool, bool]:
    text = _safe_text(turn_text)
    if not text:
        return False, False

    explicit_authoring = any(pattern.search(text) for pattern in _EXPLICIT_AUTHORING_PATTERNS)
    workflow_query = any(pattern.search(text) for pattern in _WORKFLOW_QUERY_PATTERNS)
    return explicit_authoring, workflow_query


def _turn_explicit_execution_request(turn_text: str) -> bool:
    text = _safe_text(turn_text)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _EXPLICIT_EXECUTION_PATTERNS)


def _context_requires_explicit_execution_request(context: str) -> bool:
    lowered = _safe_text(context).lower()
    return lowered == "selected_custom_workflow_launchability_replacement"


def _infer_role(candidate: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
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

    text_corpus = " ".join(
        part
        for part in (
            _safe_text(candidate.get("name")),
            _safe_text(candidate.get("description")),
            _safe_text(candidate.get("workflow_purpose")),
        )
        if part
    ).lower()
    action_corpus = " ".join(
        _safe_text(item).lower()
        for item in (candidate.get("workflow_action_ids") or ())
        if _safe_text(item)
    )

    if any(hint in text_corpus for hint in _AUTHORING_TEXT_HINTS) or any(
        hint in action_corpus for hint in _AUTHORING_ACTION_HINTS
    ):
        return (
            _ROLE_AUTHORING,
            "heuristic",
            {
                "authoring_intent_required": True,
                "prefer_existing_capability": True,
            },
        )
    if any(hint in text_corpus for hint in _MAINTENANCE_TEXT_HINTS) or any(
        hint in action_corpus for hint in _MAINTENANCE_ACTION_HINTS
    ):
        return (
            _ROLE_MAINTENANCE,
            "heuristic",
            {
                "authoring_intent_required": False,
                "prefer_existing_capability": False,
            },
        )
    return (
        _ROLE_EXECUTION,
        "default",
        {
            "authoring_intent_required": False,
            "prefer_existing_capability": False,
        },
    )


def _context_semantic_threshold(context: str) -> float:
    lowered = _safe_text(context).lower()
    if lowered == "selected_custom_workflow_launchability_replacement":
        return 0.24
    return 0.34


def _context_composite_threshold(context: str) -> float:
    lowered = _safe_text(context).lower()
    if lowered == "selected_custom_workflow_launchability_replacement":
        return 0.26
    return 0.36


def _context_discovery_score_floor(context: str) -> float:
    lowered = _safe_text(context).lower()
    if lowered == "selected_custom_workflow_launchability_replacement":
        return 0.32
    if lowered == "mutative_intent_direct_response_override":
        return 0.38
    return 0.0


def _context_requires_lexical_grounding(context: str) -> bool:
    lowered = _safe_text(context).lower()
    return lowered == "mutative_intent_direct_response_override"


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
    """Return the best discovered custom workflow for override, if any.

    The returned decision can also explicitly decline promotion when the best
    launchable candidates are authoring/meta workflows without matching user
    intent, or when semantic fit is too weak to justify replacing the preserved
    routing path.
    """

    explicit_authoring_request, workflow_query_intent = _turn_authoring_intent(turn_text)
    explicit_execution_request = _turn_explicit_execution_request(turn_text)
    semantic_threshold = _context_semantic_threshold(context)
    composite_threshold = _context_composite_threshold(context)
    discovery_score_floor = _context_discovery_score_floor(context)
    explicit_execution_request_required = _context_requires_explicit_execution_request(
        context
    )
    requires_lexical_grounding = _context_requires_lexical_grounding(context)

    raw_assessments: list[dict[str, Any]] = []
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
        description = _safe_text(candidate.get("description"))
        workflow_purpose = _safe_text(candidate.get("workflow_purpose"))
        role, role_source, policy_flags = _infer_role(candidate)

        lexical_score, lexical_signals = _compute_lexical_fit(
            turn_text=turn_text,
            label_text=name,
            description_text=description or workflow_purpose,
        )
        discovery_score = max(
            _coerce_float(candidate.get("confidence_score"), default=0.0),
            _coerce_float(candidate.get("relevance_score"), default=0.0),
        )
        if (
            discovery_score <= 0.0
            and lexical_score <= 0.0
            and discovery_score_floor > 0.0
            and role != _ROLE_AUTHORING
        ):
            discovery_score = discovery_score_floor
        semantic_fit_score = round(
            max(
                lexical_score,
                discovery_score,
                min(1.0, discovery_score * 0.62 + lexical_score * 0.58),
            ),
            3,
        )

        role_adjustment = 0.0
        if role == _ROLE_AUTHORING:
            role_adjustment = 0.08 if explicit_authoring_request else -0.28
        elif role == _ROLE_EXECUTION:
            role_adjustment = 0.08
        elif role == _ROLE_MAINTENANCE and workflow_query_intent:
            role_adjustment = -0.04

        override_score = round(semantic_fit_score + role_adjustment, 3)
        raw_assessments.append(
            {
                "workflow_id": workflow_id,
                "name": name,
                "role": role,
                "role_source": role_source,
                "policy_flags": dict(policy_flags),
                "launchable": launchable,
                "launch_input_resolution_status": launch_status or None,
                "pre_action_reason_code": pre_action_reason or None,
                "semantic_fit_score": semantic_fit_score,
                "discovery_score": round(discovery_score, 3),
                "lexical_score": lexical_score,
                "lexical_signals": lexical_signals,
                "role_adjustment": role_adjustment,
                "override_score": override_score,
                "suitable": False,
                "suitability_reason": "",
            }
        )

    best_non_authoring_score = max(
        (
            float(item["semantic_fit_score"])
            for item in raw_assessments
            if item["launchable"] and item["role"] != _ROLE_AUTHORING
        ),
        default=0.0,
    )

    candidate_assessments: list[WorkflowOverrideCandidateAssessment] = []
    for item in raw_assessments:
        suitability_reason = "suitable"
        suitable = True
        if not item["launchable"]:
            suitable = False
            suitability_reason = "not_launchable"
        elif float(item["semantic_fit_score"]) < semantic_threshold:
            suitable = False
            suitability_reason = "semantic_fit_below_threshold"
        elif (
            requires_lexical_grounding
            and item["role"] == _ROLE_EXECUTION
            and not explicit_execution_request
            and float(item["lexical_score"]) <= 0.0
        ):
            suitable = False
            suitability_reason = "lexical_grounding_missing"
        elif (
            item["role"] == _ROLE_AUTHORING
            and bool(item["policy_flags"].get("authoring_intent_required"))
            and not explicit_authoring_request
        ):
            suitable = False
            if best_non_authoring_score >= semantic_threshold:
                suitability_reason = "existing_capability_preferred"
            elif workflow_query_intent:
                suitability_reason = "authoring_declined_for_workflow_query"
            else:
                suitability_reason = "authoring_requires_explicit_request"
        elif (
            item["role"] == _ROLE_MAINTENANCE
            and explicit_execution_request_required
            and not explicit_execution_request
        ):
            suitable = False
            if workflow_query_intent:
                suitability_reason = "maintenance_declined_for_workflow_query"
            else:
                suitability_reason = "maintenance_requires_explicit_request"
        elif float(item["override_score"]) < composite_threshold:
            suitable = False
            suitability_reason = "override_score_below_threshold"

        candidate_assessments.append(
            WorkflowOverrideCandidateAssessment(
                workflow_id=str(item["workflow_id"]),
                name=str(item["name"]),
                role=str(item["role"]),
                role_source=str(item["role_source"]),
                launchable=bool(item["launchable"]),
                semantic_fit_score=float(item["semantic_fit_score"]),
                discovery_score=float(item["discovery_score"]),
                lexical_score=float(item["lexical_score"]),
                role_adjustment=float(item["role_adjustment"]),
                override_score=float(item["override_score"]),
                suitable=bool(suitable),
                suitability_reason=suitability_reason,
                launch_input_resolution_status=item["launch_input_resolution_status"],
                pre_action_reason_code=item["pre_action_reason_code"],
                policy_flags=dict(item["policy_flags"]),
                lexical_signals=dict(item["lexical_signals"]),
            )
        )

    candidate_assessments.sort(
        key=lambda item: (
            not item.suitable,
            -item.override_score,
            -item.semantic_fit_score,
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
            reason_code="suitable_custom_workflow_found",
            explicit_authoring_request=explicit_authoring_request,
            explicit_execution_request=explicit_execution_request,
            workflow_query_intent=workflow_query_intent,
            candidate_assessments=tuple(candidate_assessments),
        )

    reason_code = "no_suitable_custom_workflow"
    if not candidate_assessments:
        reason_code = "no_custom_workflow_candidates"
    elif not any(item.launchable for item in candidate_assessments):
        reason_code = "no_launchable_custom_workflow"
    elif any(
        item.suitability_reason == "existing_capability_preferred"
        for item in candidate_assessments
    ):
        reason_code = "existing_capability_preferred"
    elif any(
        item.suitability_reason
        in {
            "authoring_declined_for_workflow_query",
            "authoring_requires_explicit_request",
        }
        for item in candidate_assessments
    ):
        reason_code = "authoring_override_declined"
    elif any(
        item.suitability_reason
        in {
            "maintenance_declined_for_workflow_query",
            "maintenance_requires_explicit_request",
        }
        for item in candidate_assessments
    ):
        reason_code = "maintenance_override_declined"
    elif any(
        item.suitability_reason == "lexical_grounding_missing"
        for item in candidate_assessments
    ):
        reason_code = "lexical_grounding_missing"
    elif any(
        item.suitability_reason == "semantic_fit_below_threshold"
        for item in candidate_assessments
    ):
        reason_code = "semantic_fit_insufficient"

    return WorkflowOverrideDecision(
        context=context,
        chosen_workflow_id=None,
        outcome="decline",
        reason_code=reason_code,
        explicit_authoring_request=explicit_authoring_request,
        explicit_execution_request=explicit_execution_request,
        workflow_query_intent=workflow_query_intent,
        candidate_assessments=tuple(candidate_assessments),
    )


__all__ = [
    "WorkflowOverrideCandidateAssessment",
    "WorkflowOverrideDecision",
    "choose_custom_workflow_override_candidate",
]
