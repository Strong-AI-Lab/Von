"""Write-tool policy helpers.

This module centralises write-policy classification so routing, execution, and
telemetry all reason about the same mutation semantics.

Minimal imposition here means:
- low-risk additive Vontology writes default-allow unless explicitly denied;
- non-destructive edits to existing state require a clear request;
- destructive writes require explicit confirmation or a preserved continuation;
- external-system writes remain stricter than Vontology-governed writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence


MUTATION_AUTHORITY_LEVEL_READ_ONLY = "read_only"
MUTATION_AUTHORITY_LEVEL_ADDITIVE_VONTOLOGY = "additive_vontology"
MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE = (
    "mutative_vontology_non_destructive"
)
MUTATION_AUTHORITY_LEVEL_DESTRUCTIVE_VONTOLOGY_WITH_CONFIRMATION = (
    "destructive_vontology_with_confirmation"
)
MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED = "external_system_guarded"

WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION = (
    "workflow_step_mutation_authority.v1"
)

MUTATION_GUARDRAIL_DECISION_ALLOWED = "allowed"
MUTATION_GUARDRAIL_DECISION_BLOCKED = "blocked"
MUTATION_GUARDRAIL_DECISION_APPROVAL_REQUIRED = "approval_required"
MUTATION_GUARDRAIL_DECISION_DEFERRED = "deferred"

REASON_NO_REQUESTED_WRITE_TOOLS = "no_requested_write_tools"
REASON_MUTATION_AUTHORITY_DEFAULT = "mutation_authority_default"
REASON_INSUFFICIENT_MUTATION_AUTHORITY = "insufficient_mutation_authority"
REASON_WORKFLOW_MUTATION_AUTHORITY_INVALID = "workflow_mutation_authority_invalid"

WRITE_RISK_ADDITIVE_LOW_RISK = "additive_low_risk"
WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE = "mutative_non_destructive"
WRITE_RISK_DESTRUCTIVE = "destructive"
WRITE_RISK_EXTERNAL_NON_VONTOLOGY = "external_non_vontology"

REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK = "default_allow_additive_low_risk"
REASON_EXPLICIT_NON_DESTRUCTIVE_MUTATION_REQUEST = (
    "explicit_non_destructive_mutation_request"
)
REASON_RECENT_NON_DESTRUCTIVE_MUTATION_REQUEST = (
    "recent_non_destructive_mutation_request"
)
REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED = (
    "mutative_non_destructive_request_required"
)
REASON_EXPLICIT_DESTRUCTIVE_CONFIRMATION = "explicit_destructive_confirmation"
REASON_RECENT_DESTRUCTIVE_CONFIRMATION = "recent_destructive_confirmation"
REASON_DESTRUCTIVE_CONFIRMATION_REQUIRED = "destructive_confirmation_required"
REASON_EXPLICIT_EXTERNAL_WRITE_REQUEST = "explicit_external_write_request"
REASON_RECENT_EXTERNAL_WRITE_REQUEST = "recent_external_write_request"
REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST = (
    "external_write_requires_explicit_request"
)
REASON_EXPLICIT_WRITE_DENIAL_DETECTED = "explicit_write_denial_detected"

_MUTATION_AUTHORITY_LEVEL_ORDER: tuple[str, ...] = (
    MUTATION_AUTHORITY_LEVEL_READ_ONLY,
    MUTATION_AUTHORITY_LEVEL_ADDITIVE_VONTOLOGY,
    MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE,
    MUTATION_AUTHORITY_LEVEL_DESTRUCTIVE_VONTOLOGY_WITH_CONFIRMATION,
    MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED,
)
_MUTATION_AUTHORITY_LEVEL_RANKS: dict[str, int] = {
    level: index for index, level in enumerate(_MUTATION_AUTHORITY_LEVEL_ORDER)
}

_EXTERNAL_WRITE_PREFIXES: tuple[str, ...] = (
    "jira_",
    "github_",
    "gmail_",
)

_ADDITIVE_LOW_RISK_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_concepts",
        "add_relationship",
        "add_names",
        "upsert_text_relation",
        "upsert_singleton_text_relation",
        "download_paper",
        "finalise_cached_paper",
        "import_url_file_copy",
        "create_task",
        "assign_task",
        "workflow_create_instance",
        "workflow_bind_event",
        "workflow_create_schedule",
        "workflow_trigger_schedule",
    }
)

_MUTATIVE_NON_DESTRUCTIVE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "update_concept",
        "update_text_relation",
        "update_task_status",
        "undo_relationship_removal",
        "workflow_retry_instance",
        "workflow_set_event_binding_enabled",
        "workflow_set_schedule_enabled",
    }
)

_DESTRUCTIVE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "delete_concept",
        "delete_text_relation",
        "remove_relationship",
        "remove_relationships_bulk",
        "merge_concepts",
        "workflow_cancel_instance",
        "workflow_delete_event_binding",
        "workflow_delete_schedule",
    }
)

_CONFIRMATION_PATTERN = re.compile(
    r"\b("
    r"confirm(?:ed|ation)?|approved?|i approve|go ahead|do it|yes\b|proceed|"
    r"permission granted|you may proceed|continue with|carry on"
    r")\b",
    flags=re.IGNORECASE,
)

_NON_DESTRUCTIVE_MUTATION_PATTERN = re.compile(
    r"\b("
    r"update|edit|change|rename|set|adjust|revise|modify|"
    r"assign|mark|move|reword|rewrite|replace"
    r")\b",
    flags=re.IGNORECASE,
)

_DESTRUCTIVE_MUTATION_PATTERN = re.compile(
    r"\b("
    r"delete|remove|unlink|detach|drop|purge|erase|destroy|merge"
    r")\b",
    flags=re.IGNORECASE,
)

_EXTERNAL_WRITE_PATTERN = re.compile(
    r"\b("
    r"create|update|edit|change|delete|remove|comment|attach|transition|"
    r"open|file|submit|merge|label|assign"
    r")\b",
    flags=re.IGNORECASE,
)

_NON_DESTRUCTIVE_CONTEXT_TERMS: tuple[str, ...] = (
    "#v#",
    "vontology",
    "ontology",
    "concept",
    "relationship",
    "predicate",
    "text relation",
    "note",
    "description",
    "metadata",
    "task",
    "workflow",
    "schedule",
    "binding",
)

_EXTERNAL_CONTEXT_TERMS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "jira_": ("jira", "issue", "ticket", "atlassian"),
    "github_": ("github", "pull request", "pr", "issue", "review", "repository"),
    "gmail_": ("gmail", "email", "mail", "label", "message"),
}

_ADDITIVE_MUTATION_PATTERN = re.compile(
    r"\b("
    r"create|add|insert|upsert|link|attach|store|save|download|finalise|"
    r"finalize|represent|materialise|materialize|capture|record"
    r")\b",
    flags=re.IGNORECASE,
)

_ARXIV_ID_OR_URL_PATTERN = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/(?:(?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?)"
    r"|(?:\barxiv:\s*(?:(?:[a-z\-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?\b)"
    r"|(?:\b\d{4}\.\d{4,5}(?:v\d+)?\b)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class WriteToolDecision:
    tool_name: str
    risk_class: str
    required_mutation_authority: str
    effective_mutation_authority: str
    authority_sources: Mapping[str, str]
    allowed: bool
    outcome: str
    decision_basis: str
    requires_confirmation: bool = False
    blocked_reason: str | None = None
    authority_block_source: str | None = None
    continuation_context_used: bool = False


@dataclass(frozen=True)
class WriteToolPolicyDecision:
    allowed_tools: frozenset[str]
    reason: str
    decision_basis: str
    outcome: str
    effective_mutation_authority: str
    authority_sources: Mapping[str, str]
    user_denial_detected: bool
    tool_decisions: tuple[WriteToolDecision, ...]

    def decision_for_tool(self, tool_name: str) -> WriteToolDecision | None:
        if not isinstance(tool_name, str):
            return None
        lowered = tool_name.strip().lower()
        for decision in self.tool_decisions:
            if decision.tool_name.lower() == lowered:
                return decision
        return None


def normalise_mutation_authority_level(
    value: Any,
    *,
    default: str = MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED,
) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned in _MUTATION_AUTHORITY_LEVEL_RANKS:
        return cleaned
    return default


def mutation_authority_level_rank(level: Any) -> int:
    return int(
        _MUTATION_AUTHORITY_LEVEL_RANKS.get(
            normalise_mutation_authority_level(level),
            _MUTATION_AUTHORITY_LEVEL_RANKS[
                MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED
            ],
        )
    )


def intersect_mutation_authority_levels(
    *levels: Any,
    default: str = MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED,
) -> str:
    resolved = [normalise_mutation_authority_level(item, default=default) for item in levels]
    if not resolved:
        return normalise_mutation_authority_level(default)
    return min(resolved, key=mutation_authority_level_rank)


def required_mutation_authority_level_for_risk(risk_class: str) -> str:
    if risk_class == WRITE_RISK_ADDITIVE_LOW_RISK:
        return MUTATION_AUTHORITY_LEVEL_ADDITIVE_VONTOLOGY
    if risk_class == WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE:
        return MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE
    if risk_class == WRITE_RISK_DESTRUCTIVE:
        return MUTATION_AUTHORITY_LEVEL_DESTRUCTIVE_VONTOLOGY_WITH_CONFIRMATION
    return MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED


def normalise_workflow_step_mutation_authority_spec(
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        maximum_level = normalise_mutation_authority_level(value, default="")
        if maximum_level:
            return {
                "schema_version": WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION,
                "maximum_level": maximum_level,
            }
        return None
    if not isinstance(value, Mapping):
        return None

    schema_version = str(value.get("schema_version") or "").strip()
    if schema_version and schema_version != WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION:
        return None

    maximum_level_raw = (
        value.get("maximum_level")
        or value.get("max_level")
        or value.get("level")
        or value.get("mutation_authority_level")
    )
    maximum_level = normalise_mutation_authority_level(maximum_level_raw, default="")
    if not maximum_level:
        return None

    normalised = {
        "schema_version": WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION,
        "maximum_level": maximum_level,
    }
    reason_code = str(value.get("reason_code") or "").strip()
    if reason_code:
        normalised["reason_code"] = reason_code
    return normalised


def classify_write_tool_risk(tool_name: str) -> str:
    """Return the central write-risk class for a tool name."""

    if not isinstance(tool_name, str):
        return WRITE_RISK_EXTERNAL_NON_VONTOLOGY

    lowered = tool_name.strip().lower()
    if not lowered:
        return WRITE_RISK_EXTERNAL_NON_VONTOLOGY

    if any(lowered.startswith(prefix) for prefix in _EXTERNAL_WRITE_PREFIXES):
        return WRITE_RISK_EXTERNAL_NON_VONTOLOGY
    if lowered in _DESTRUCTIVE_WRITE_TOOLS:
        return WRITE_RISK_DESTRUCTIVE
    if lowered in _MUTATIVE_NON_DESTRUCTIVE_WRITE_TOOLS:
        return WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE
    if lowered in _ADDITIVE_LOW_RISK_WRITE_TOOLS:
        return WRITE_RISK_ADDITIVE_LOW_RISK

    if lowered.startswith(("delete_", "remove_", "merge_")):
        return WRITE_RISK_DESTRUCTIVE
    if lowered.startswith(("update_", "rename_", "assign_", "set_")):
        return WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE
    if lowered.startswith(
        ("create_", "add_", "upsert_", "download_", "finalise_", "finalize_")
    ):
        return WRITE_RISK_ADDITIVE_LOW_RISK

    return WRITE_RISK_EXTERNAL_NON_VONTOLOGY


def tool_requires_confirmation(tool_name: str) -> bool:
    return classify_write_tool_risk(tool_name) == WRITE_RISK_DESTRUCTIVE


def tool_requires_explicit_request(tool_name: str) -> bool:
    return classify_write_tool_risk(tool_name) in {
        WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE,
        WRITE_RISK_EXTERNAL_NON_VONTOLOGY,
    }


def is_high_impact_vontology_write_tool(tool_name: str) -> bool:
    """Compatibility shim for legacy "high-impact" routing code.

    "High-impact" now means mutating existing state or removing it. Additive
    writes no longer count as high-impact by default.
    """

    return classify_write_tool_risk(tool_name) in {
        WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE,
        WRITE_RISK_DESTRUCTIVE,
    }


def compute_allowed_write_tools(
    *,
    prompt: str,
    requested_tools: list[str],
    recent_user_prompts: list[str] | None = None,
    user_mutation_authority: str | None = None,
    workflow_mutation_authority: Mapping[str, Any] | str | None = None,
    global_mutation_authority: str | None = None,
    environment_mutation_authority: str | None = None,
) -> WriteToolPolicyDecision:
    """Compute which write tools are allowed for this user prompt."""

    requested = _normalise_tool_names(requested_tools)
    recent_prompts = _clean_prompt_list(recent_user_prompts)
    workflow_mutation_authority_spec = normalise_workflow_step_mutation_authority_spec(
        workflow_mutation_authority
    )
    workflow_authority_invalid = (
        workflow_mutation_authority is not None
        and workflow_mutation_authority_spec is None
    )
    authority_sources = {
        "user": normalise_mutation_authority_level(user_mutation_authority),
        "workflow": (
            str(workflow_mutation_authority_spec.get("maximum_level") or "").strip()
            if isinstance(workflow_mutation_authority_spec, Mapping)
            else (
                MUTATION_AUTHORITY_LEVEL_READ_ONLY
                if workflow_authority_invalid
                else MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED
            )
        ),
        "global": normalise_mutation_authority_level(global_mutation_authority),
        "environment": normalise_mutation_authority_level(
            environment_mutation_authority
        ),
    }
    effective_mutation_authority = intersect_mutation_authority_levels(
        authority_sources["user"],
        authority_sources["workflow"],
        authority_sources["global"],
        authority_sources["environment"],
    )
    if not requested:
        return WriteToolPolicyDecision(
            allowed_tools=frozenset(),
            reason=REASON_NO_REQUESTED_WRITE_TOOLS,
            decision_basis=REASON_NO_REQUESTED_WRITE_TOOLS,
            outcome=MUTATION_GUARDRAIL_DECISION_ALLOWED,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            user_denial_detected=False,
            tool_decisions=(),
        )

    if prompt_explicitly_denies_write(prompt):
        tool_decisions = tuple(
            WriteToolDecision(
                tool_name=tool_name,
                risk_class=classify_write_tool_risk(tool_name),
                required_mutation_authority=required_mutation_authority_level_for_risk(
                    classify_write_tool_risk(tool_name)
                ),
                effective_mutation_authority=effective_mutation_authority,
                authority_sources=authority_sources,
                allowed=False,
                outcome=MUTATION_GUARDRAIL_DECISION_BLOCKED,
                decision_basis=REASON_EXPLICIT_WRITE_DENIAL_DETECTED,
                requires_confirmation=False,
                blocked_reason=REASON_EXPLICIT_WRITE_DENIAL_DETECTED,
            )
            for tool_name in requested
        )
        return WriteToolPolicyDecision(
            allowed_tools=frozenset(),
            reason=REASON_EXPLICIT_WRITE_DENIAL_DETECTED,
            decision_basis=REASON_EXPLICIT_WRITE_DENIAL_DETECTED,
            outcome=MUTATION_GUARDRAIL_DECISION_BLOCKED,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            user_denial_detected=True,
            tool_decisions=tool_decisions,
        )

    decisions: list[WriteToolDecision] = []
    allowed_tools: set[str] = set()
    overall_reason = ""

    for tool_name in requested:
        decision = _decide_single_tool(
            tool_name=tool_name,
            prompt=prompt,
            recent_user_prompts=recent_prompts,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            workflow_authority_invalid=workflow_authority_invalid,
        )
        decisions.append(decision)
        if decision.allowed:
            allowed_tools.add(tool_name)
        if (
            not overall_reason
            or decision.outcome != MUTATION_GUARDRAIL_DECISION_ALLOWED
        ):
            overall_reason = decision.blocked_reason or decision.decision_basis

    if not overall_reason and decisions:
        overall_reason = decisions[0].decision_basis

    overall_decision = min(
        decisions,
        key=lambda item: {
            MUTATION_GUARDRAIL_DECISION_BLOCKED: 0,
            MUTATION_GUARDRAIL_DECISION_APPROVAL_REQUIRED: 1,
            MUTATION_GUARDRAIL_DECISION_DEFERRED: 2,
            MUTATION_GUARDRAIL_DECISION_ALLOWED: 3,
        }.get(item.outcome, 99),
    )

    return WriteToolPolicyDecision(
        allowed_tools=frozenset(allowed_tools),
        reason=overall_reason or REASON_MUTATION_AUTHORITY_DEFAULT,
        decision_basis=overall_reason or REASON_MUTATION_AUTHORITY_DEFAULT,
        outcome=overall_decision.outcome,
        effective_mutation_authority=effective_mutation_authority,
        authority_sources=authority_sources,
        user_denial_detected=False,
        tool_decisions=tuple(decisions),
    )


def build_mutation_guardrail_events(
    *,
    policy_decision: WriteToolPolicyDecision,
    guardrail_surface: str,
    stage: str | None,
    workflow_id: str | None = None,
    workflow_step_id: str | None = None,
    action_id: str | None = None,
    conversation_session_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for decision in policy_decision.tool_decisions:
        event: dict[str, Any] = {
            "type": "mutation_guardrail",
            "guardrail_surface": str(guardrail_surface or "").strip() or "unknown",
            "stage": str(stage or "").strip() or None,
            "tool_name": decision.tool_name,
            "risk_class": decision.risk_class,
            "required_mutation_authority": decision.required_mutation_authority,
            "effective_mutation_authority": decision.effective_mutation_authority,
            "authority_sources": dict(decision.authority_sources),
            "decision": decision.outcome,
            "decision_basis": decision.decision_basis,
            "blocked_reason": decision.blocked_reason,
            "authority_block_source": decision.authority_block_source,
            "requires_confirmation": decision.requires_confirmation,
            "continuation_context_used": decision.continuation_context_used,
            "user_denial_detected": policy_decision.user_denial_detected,
        }
        if workflow_id:
            event["workflow_id"] = workflow_id
        if workflow_step_id:
            event["workflow_step_id"] = workflow_step_id
        if action_id:
            event["action_id"] = action_id
        if conversation_session_id:
            event["conversation_session_id"] = conversation_session_id
        if turn_id:
            event["turn_id"] = turn_id
        events.append(event)
    return tuple(events)


def write_policy_reason_is_session_memory_eligible(reason: str | None) -> bool:
    """Return whether a policy result should be persisted for continuation."""

    return str(reason or "").strip() in {
        REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK,
        REASON_EXPLICIT_NON_DESTRUCTIVE_MUTATION_REQUEST,
        REASON_RECENT_NON_DESTRUCTIVE_MUTATION_REQUEST,
        REASON_DESTRUCTIVE_CONFIRMATION_REQUIRED,
        REASON_EXPLICIT_DESTRUCTIVE_CONFIRMATION,
        REASON_RECENT_DESTRUCTIVE_CONFIRMATION,
        REASON_EXPLICIT_EXTERNAL_WRITE_REQUEST,
        REASON_RECENT_EXTERNAL_WRITE_REQUEST,
    }


def prompt_has_low_risk_additive_write_evidence(prompt: str) -> bool:
    """Return whether the prompt gives affirmative evidence for additive writes.

    This routing helper is intentionally narrower than the allow-policy itself:
    additive tools may be allowed if requested, but we should only force the
    tool route when the prompt itself supplies concrete evidence of an additive
    mutation task.
    """

    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text or prompt_explicitly_denies_write(text):
        return False

    lowered = text.lower()
    if _ARXIV_ID_OR_URL_PATTERN.search(text):
        return True

    additive_context_terms = (
        "#v#",
        "vontology",
        "ontology",
        "concept",
        "relationship",
        "predicate",
        "text relation",
        "note",
        "description",
        "metadata",
        "task",
        "workflow",
        "schedule",
        "binding",
    )
    if any(token in lowered for token in additive_context_terms) and _ADDITIVE_MUTATION_PATTERN.search(
        text
    ):
        return True

    return False


def prompt_grants_high_impact_kb_write_approval(
    *,
    prompt: str,
    recent_user_prompts: list[str] | None = None,
) -> bool:
    """Compatibility shim for historic review-gate code paths.

    High-impact approval now maps to explicit confirmation of a destructive
    mutation in the current or immediately preceding preserved context.
    """

    return prompt_grants_destructive_write_confirmation(
        prompt=prompt,
        recent_user_prompts=recent_user_prompts,
    )


def prompt_grants_destructive_write_confirmation(
    *,
    prompt: str,
    recent_user_prompts: list[str] | None = None,
) -> bool:
    """Return True when the user explicitly confirms a destructive mutation."""

    prompt_text = prompt.strip() if isinstance(prompt, str) else ""
    recent_prompts = _clean_prompt_list(recent_user_prompts)

    prompt_confirms = bool(prompt_text and _CONFIRMATION_PATTERN.search(prompt_text))
    prompt_is_destructive = bool(
        prompt_text and _DESTRUCTIVE_MUTATION_PATTERN.search(prompt_text)
    )
    if prompt_confirms and prompt_is_destructive:
        return True

    if not prompt_confirms:
        return False

    return any(_DESTRUCTIVE_MUTATION_PATTERN.search(text) for text in recent_prompts)


def prompt_explicitly_denies_write(prompt: str) -> bool:
    """Return True when the user explicitly forbids write-side effects."""

    if not isinstance(prompt, str):
        return False

    lowered = prompt.lower()
    verbs = (
        "create",
        "add",
        "insert",
        "upsert",
        "update",
        "edit",
        "change",
        "delete",
        "remove",
        "rename",
        "merge",
        "link",
        "unlink",
        "set",
        "download",
        "fetch",
        "save",
        "store",
        "cache",
        "archive",
        "persist",
        "upload",
        "finalise",
        "finalize",
        "comment",
        "attach",
        "transition",
        "label",
        "assign",
    )

    return any(
        re.search(
            rf"\b(?:do not|don't|dont|never)\s+{re.escape(verb)}\b",
            lowered,
        )
        for verb in verbs
    )


def _decide_single_tool(
    *,
    tool_name: str,
    prompt: str,
    recent_user_prompts: Sequence[str],
    effective_mutation_authority: str,
    authority_sources: Mapping[str, str],
    workflow_authority_invalid: bool,
) -> WriteToolDecision:
    risk_class = classify_write_tool_risk(tool_name)
    required_mutation_authority = required_mutation_authority_level_for_risk(risk_class)
    authority_block_source = _resolve_authority_block_source(
        required_mutation_authority=required_mutation_authority,
        authority_sources=authority_sources,
    )
    if workflow_authority_invalid:
        authority_block_source = "workflow"

    if (
        mutation_authority_level_rank(effective_mutation_authority)
        < mutation_authority_level_rank(required_mutation_authority)
    ):
        blocked_reason = (
            REASON_WORKFLOW_MUTATION_AUTHORITY_INVALID
            if workflow_authority_invalid
            else REASON_INSUFFICIENT_MUTATION_AUTHORITY
        )
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            required_mutation_authority=required_mutation_authority,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            allowed=False,
            outcome=MUTATION_GUARDRAIL_DECISION_BLOCKED,
            decision_basis=blocked_reason,
            blocked_reason=blocked_reason,
            authority_block_source=authority_block_source,
        )

    if risk_class == WRITE_RISK_ADDITIVE_LOW_RISK:
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            required_mutation_authority=required_mutation_authority,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            allowed=True,
            outcome=MUTATION_GUARDRAIL_DECISION_ALLOWED,
            decision_basis=REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK,
        )

    if risk_class == WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE:
        explicit = _prompt_requests_non_destructive_mutation(prompt)
        recent = any(
            _prompt_requests_non_destructive_mutation(text)
            for text in recent_user_prompts
        )
        if explicit or recent:
            reason = (
                REASON_EXPLICIT_NON_DESTRUCTIVE_MUTATION_REQUEST
                if explicit
                else REASON_RECENT_NON_DESTRUCTIVE_MUTATION_REQUEST
            )
            return WriteToolDecision(
                tool_name=tool_name,
                risk_class=risk_class,
                required_mutation_authority=required_mutation_authority,
                effective_mutation_authority=effective_mutation_authority,
                authority_sources=authority_sources,
                allowed=True,
                outcome=MUTATION_GUARDRAIL_DECISION_ALLOWED,
                decision_basis=reason,
                continuation_context_used=recent and not explicit,
            )
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            required_mutation_authority=required_mutation_authority,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            allowed=False,
            outcome=MUTATION_GUARDRAIL_DECISION_DEFERRED,
            decision_basis=REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED,
            blocked_reason=REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED,
        )

    if risk_class == WRITE_RISK_DESTRUCTIVE:
        explicit_confirmation = prompt_grants_destructive_write_confirmation(
            prompt=prompt,
            recent_user_prompts=[],
        )
        recent_confirmation = (
            not explicit_confirmation
            and prompt_grants_destructive_write_confirmation(
                prompt=prompt,
                recent_user_prompts=list(recent_user_prompts),
            )
        )
        if explicit_confirmation or recent_confirmation:
            reason = (
                REASON_EXPLICIT_DESTRUCTIVE_CONFIRMATION
                if explicit_confirmation
                else REASON_RECENT_DESTRUCTIVE_CONFIRMATION
            )
            return WriteToolDecision(
                tool_name=tool_name,
                risk_class=risk_class,
                required_mutation_authority=required_mutation_authority,
                effective_mutation_authority=effective_mutation_authority,
                authority_sources=authority_sources,
                allowed=True,
                outcome=MUTATION_GUARDRAIL_DECISION_ALLOWED,
                decision_basis=reason,
                requires_confirmation=True,
                continuation_context_used=recent_confirmation,
            )
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            required_mutation_authority=required_mutation_authority,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            allowed=False,
            outcome=MUTATION_GUARDRAIL_DECISION_APPROVAL_REQUIRED,
            decision_basis=REASON_DESTRUCTIVE_CONFIRMATION_REQUIRED,
            requires_confirmation=True,
            blocked_reason=REASON_DESTRUCTIVE_CONFIRMATION_REQUIRED,
        )

    explicit_external = _prompt_requests_external_write(
        prompt=prompt,
        tool_name=tool_name,
    )
    recent_external = any(
        _prompt_requests_external_write(prompt=text, tool_name=tool_name)
        for text in recent_user_prompts
    )
    if explicit_external or recent_external:
        reason = (
            REASON_EXPLICIT_EXTERNAL_WRITE_REQUEST
            if explicit_external
            else REASON_RECENT_EXTERNAL_WRITE_REQUEST
        )
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            required_mutation_authority=required_mutation_authority,
            effective_mutation_authority=effective_mutation_authority,
            authority_sources=authority_sources,
            allowed=True,
            outcome=MUTATION_GUARDRAIL_DECISION_ALLOWED,
            decision_basis=reason,
            continuation_context_used=recent_external and not explicit_external,
        )

    return WriteToolDecision(
        tool_name=tool_name,
        risk_class=risk_class,
        required_mutation_authority=required_mutation_authority,
        effective_mutation_authority=effective_mutation_authority,
        authority_sources=authority_sources,
        allowed=False,
        outcome=MUTATION_GUARDRAIL_DECISION_DEFERRED,
        decision_basis=REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST,
        blocked_reason=REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST,
    )


def _resolve_authority_block_source(
    *,
    required_mutation_authority: str,
    authority_sources: Mapping[str, str],
) -> str | None:
    required_rank = mutation_authority_level_rank(required_mutation_authority)
    for source_name in ("workflow", "user", "global", "environment"):
        level = authority_sources.get(source_name)
        if mutation_authority_level_rank(level) < required_rank:
            return source_name
    return None


def _prompt_requests_non_destructive_mutation(prompt: str) -> bool:
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text:
        return False
    lowered = text.lower()
    if not any(token in lowered for token in _NON_DESTRUCTIVE_CONTEXT_TERMS):
        return False
    return bool(_NON_DESTRUCTIVE_MUTATION_PATTERN.search(text))


def _prompt_requests_external_write(*, prompt: str, tool_name: str) -> bool:
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text:
        return False

    lowered_tool = str(tool_name or "").strip().lower()
    context_terms: Sequence[str] = ()
    for prefix, terms in _EXTERNAL_CONTEXT_TERMS_BY_PREFIX.items():
        if lowered_tool.startswith(prefix):
            context_terms = terms
            break
    if not context_terms:
        return False

    lowered = text.lower()
    if not any(token in lowered for token in context_terms):
        return False
    return bool(_EXTERNAL_WRITE_PATTERN.search(text))


def _clean_prompt_list(values: Sequence[str] | None) -> list[str]:
    return [
        str(item).strip()
        for item in (values or [])
        if isinstance(item, str) and str(item).strip()
    ]


def _normalise_tool_names(values: Sequence[str] | None) -> list[str]:
    tools: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        tool_name = raw.strip()
        lowered = tool_name.lower()
        if not tool_name or lowered in seen:
            continue
        seen.add(lowered)
        tools.append(tool_name)
    return tools
