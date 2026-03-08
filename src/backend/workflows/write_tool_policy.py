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
from typing import Sequence

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
    allowed: bool
    decision_basis: str
    requires_confirmation: bool = False
    blocked_reason: str | None = None


@dataclass(frozen=True)
class WriteToolPolicyDecision:
    allowed_tools: frozenset[str]
    reason: str
    decision_basis: str
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
) -> WriteToolPolicyDecision:
    """Compute which write tools are allowed for this user prompt."""

    requested = _normalise_tool_names(requested_tools)
    recent_prompts = _clean_prompt_list(recent_user_prompts)
    if not requested:
        return WriteToolPolicyDecision(
            allowed_tools=frozenset(),
            reason="no_requested_write_tools",
            decision_basis="no_requested_write_tools",
            user_denial_detected=False,
            tool_decisions=(),
        )

    if prompt_explicitly_denies_write(prompt):
        tool_decisions = tuple(
            WriteToolDecision(
                tool_name=tool_name,
                risk_class=classify_write_tool_risk(tool_name),
                allowed=False,
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
        )
        decisions.append(decision)
        if decision.allowed:
            allowed_tools.add(tool_name)
        if not overall_reason or not decision.allowed:
            overall_reason = decision.blocked_reason or decision.decision_basis

    if not overall_reason and decisions:
        overall_reason = decisions[0].decision_basis

    return WriteToolPolicyDecision(
        allowed_tools=frozenset(allowed_tools),
        reason=overall_reason or "write_policy_evaluated",
        decision_basis=overall_reason or "write_policy_evaluated",
        user_denial_detected=False,
        tool_decisions=tuple(decisions),
    )


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
) -> WriteToolDecision:
    risk_class = classify_write_tool_risk(tool_name)

    if risk_class == WRITE_RISK_ADDITIVE_LOW_RISK:
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            allowed=True,
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
                allowed=True,
                decision_basis=reason,
            )
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            allowed=False,
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
                allowed=True,
                decision_basis=reason,
                requires_confirmation=True,
            )
        return WriteToolDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            allowed=False,
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
            allowed=True,
            decision_basis=reason,
        )

    return WriteToolDecision(
        tool_name=tool_name,
        risk_class=risk_class,
        allowed=False,
        decision_basis=REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST,
        blocked_reason=REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST,
    )


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
