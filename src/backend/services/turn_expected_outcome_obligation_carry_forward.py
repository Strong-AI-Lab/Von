"""Support surface for prior-turn expected-outcome obligation carry-forward.

A follow-up turn may legitimately reuse *referents* from a prior turn (resolved
concept IDs, source URIs, candidate targets, uncertainty notes) without inheriting
the prior turn's *operational obligations* (its ingestion / read-back / mutation
required tools and completion-gate expectations). Von previously conflated the two:
because a prior turn's resolved target concept IDs were carried into the current
turn's expected-outcome contract, the contract's ``conditional_required_tools``
"activated" (they only activate once a symbolic target is resolved), and a simple
"do you have a concept for X" lookup inherited the whole prior ingestion/read-back
tool obligation family and answered as if verification was still the task.

The represented policy for *whether* prior obligations remain relevant lives in the
context-adjudication / expected-outcome prompt guidance. This module is support-only:
it enforces the shape the represented policy asks for, keeps referents while
suppressing stale obligations, and preserves the decision as telemetry so replay
diagnosis can see exactly what was carried and what was suppressed and why.

The invariant enforced here:

    Current user intent owns the new expected outcome. Prior turns may contribute
    referents, candidate IDs, and uncertainty notes, but prior completion
    obligations carry forward only when the current prompt is adjudicated as an
    explicit continuation / verification / resume of the prior operation, or when
    the current turn itself resolved the target through its own execution.

This is domain-agnostic: it never inspects a URL, arXiv id, mail id, or Jira key.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .required_tool_identity_service import canonical_required_tool_key

TURN_OBLIGATION_CARRY_FORWARD_SCHEMA_VERSION = (
    "turn_expected_outcome_obligation_carry_forward.v1"
)

# Represented context-adjudication decision fields that can authorise or forbid
# carrying prior-turn obligations into the current turn. The represented prompt
# owns the policy; support code only reads the recorded directive.
_CARRY_FORWARD_DECISION_KEYS: tuple[str, ...] = (
    "prior_obligation_carry_forward",
    "obligation_carry_forward",
    "expected_outcome_obligation_scope",
)
_EXPECTED_OUTCOME_SCOPE_KEYS: tuple[str, ...] = (
    "expected_outcome_scope",
    "turn_context_handoff_expected_outcome_scope",
)

_CARRY_TOKENS = frozenset(
    {
        "carry",
        "carry_forward",
        "carried_forward",
        "retain",
        "keep",
        "continue",
        "continuation",
        "resume",
        "verify",
        "inherit",
    }
)
_SUPPRESS_TOKENS = frozenset(
    {
        "suppress",
        "suppressed",
        "drop",
        "reset",
        "omit",
        "current_request_only",
        "no_prior_context",
        "fresh",
    }
)

_CREATE_IF_ABSENT_TOOL_KEYS = frozenset({"create_concepts"})


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _dedupe_strings(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        cleaned = _clean_text(item)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(cleaned)
    return ordered


def obligation_carry_forward_permission(
    adjudication_decision: Mapping[str, Any] | None,
) -> bool | None:
    """Read the represented obligation carry-forward directive.

    Returns ``True`` when represented policy explicitly permits carrying prior
    obligations, ``False`` when it explicitly suppresses them, and ``None`` when
    there is no represented directive (the caller then applies the conservative
    structural default).
    """

    if not isinstance(adjudication_decision, Mapping):
        return None
    for key in _CARRY_FORWARD_DECISION_KEYS:
        text = _clean_text(adjudication_decision.get(key))
        if text is None:
            continue
        lowered = text.lower()
        if lowered in _CARRY_TOKENS:
            return True
        if lowered in _SUPPRESS_TOKENS:
            return False
    # An explicit "current request only" expected-outcome scope means the current
    # intent owns the outcome and no prior obligation should carry.
    for key in _EXPECTED_OUTCOME_SCOPE_KEYS:
        text = _clean_text(adjudication_decision.get(key))
        if text and text.lower() in _SUPPRESS_TOKENS:
            return False
    return None


def _symbolic_target_present(contract: Any) -> bool:
    if getattr(contract, "target_concept_ids", None):
        return True
    if getattr(contract, "target_type_ids", None):
        return True
    for target_contract in getattr(contract, "target_contracts", ()) or ():
        resolver = getattr(target_contract, "is_symbolically_resolved", None)
        if callable(resolver) and resolver():
            return True
    return False


def _candidate_targets(contract: Any) -> list[str]:
    candidates: list[str] = []
    for target_contract in getattr(contract, "target_contracts", ()) or ():
        candidates.extend(getattr(target_contract, "candidate_concept_ids", ()) or ())
    return _dedupe_strings(candidates)


def _resolved_entity_target_present(contract: Any) -> bool:
    """Return whether the agreement already resolves its focal entity.

    A resolved *type* can be the grounded parent for a later create operation;
    it must not be confused with an already-existing focal entity.
    """

    for target_contract in getattr(contract, "target_contracts", ()) or ():
        binding_kind = _clean_text(getattr(target_contract, "binding_kind", None))
        if binding_kind != "entity":
            continue
        resolver = getattr(target_contract, "is_symbolically_resolved", None)
        if callable(resolver) and resolver():
            return True
    return False


def _unresolved_entity_target_present(contract: Any) -> bool:
    """Return whether any focal entity agreement still needs materialisation."""

    for target_contract in getattr(contract, "target_contracts", ()) or ():
        binding_kind = _clean_text(getattr(target_contract, "binding_kind", None))
        if binding_kind != "entity":
            continue
        resolver = getattr(target_contract, "is_symbolically_resolved", None)
        if not callable(resolver) or not resolver():
            return True
    return False


def _invoked_tool_keys(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> set[str]:
    if not isinstance(tool_invocations, Sequence) or isinstance(
        tool_invocations, (str, bytes, bytearray)
    ):
        return set()
    keys: set[str] = set()
    for invocation in tool_invocations:
        if not isinstance(invocation, Mapping):
            continue
        name = (
            _clean_text(invocation.get("tool_name"))
            or _clean_text(invocation.get("tool"))
            or _clean_text(invocation.get("name"))
            or _clean_text(invocation.get("method"))
        )
        if name:
            keys.add(canonical_required_tool_key(name))
    return keys


def target_resolved_by_current_turn(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Did the current turn do its own work that could resolve a target?

    A turn that invoked no tools cannot have resolved a symbolic target through
    its own execution; any resolved symbolic target it carries must therefore be
    a referent inherited from prior conversation context. This is the structural
    signal that distinguishes a legitimate in-turn retrieve-then-act flow (which
    invokes retrieval tools before its conditional obligation activates) from a
    bare follow-up that merely reuses prior referents.
    """

    if not isinstance(tool_invocations, Sequence) or isinstance(
        tool_invocations, (str, bytes, bytearray)
    ):
        return False
    for invocation in tool_invocations:
        if not isinstance(invocation, Mapping):
            continue
        name = (
            _clean_text(invocation.get("tool_name"))
            or _clean_text(invocation.get("tool"))
            or _clean_text(invocation.get("name"))
            or _clean_text(invocation.get("method"))
        )
        if name:
            return True
    return False


def adjudicate_conditional_required_tool_activation(
    *,
    contract: Any,
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    obligation_carry_forward_permitted: bool | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Decide which conditional required tools activate for the current turn.

    Returns ``(activated_conditional_required_tools, projection)`` where the
    projection classifies prior context into carry-forward buckets and records
    any suppression so telemetry/replay can see it.
    """

    conditional_tools = _dedupe_strings(
        getattr(contract, "conditional_required_tools", ()) or ()
    )
    resolved_by_current_turn = target_resolved_by_current_turn(tool_invocations)
    symbolic_target = _symbolic_target_present(contract)
    resolved_entity_target = _resolved_entity_target_present(contract)
    unresolved_entity_target = _unresolved_entity_target_present(contract)
    invoked_tool_keys = _invoked_tool_keys(tool_invocations)
    satisfied_without_execution: list[str] = []
    if resolved_entity_target and not unresolved_entity_target:
        remaining_tools: list[str] = []
        for tool_name in conditional_tools:
            tool_key = canonical_required_tool_key(tool_name)
            if (
                tool_key in _CREATE_IF_ABSENT_TOOL_KEYS
                and tool_key not in invoked_tool_keys
            ):
                satisfied_without_execution.append(tool_name)
                continue
            remaining_tools.append(tool_name)
        conditional_tools = remaining_tools

    projection: dict[str, Any] = {
        "schema_version": TURN_OBLIGATION_CARRY_FORWARD_SCHEMA_VERSION,
        "carried_forward_referents": {
            "target_concept_ids": _dedupe_strings(
                getattr(contract, "target_concept_ids", ()) or ()
            ),
            "target_type_ids": _dedupe_strings(
                getattr(contract, "target_type_ids", ()) or ()
            ),
            "workflow_concept_ids": _dedupe_strings(
                getattr(contract, "workflow_concept_ids", ()) or ()
            ),
        },
        "carried_forward_candidate_targets": _candidate_targets(contract),
        "carried_forward_obligations": [],
        "suppressed_prior_obligations": [],
        "suppression_reason": None,
        "obligation_carry_forward_permitted": obligation_carry_forward_permitted,
        "target_resolved_by_current_turn": resolved_by_current_turn,
        "symbolic_target_present": symbolic_target,
        "resolved_entity_target_present": resolved_entity_target,
        "unresolved_entity_target_present": unresolved_entity_target,
        "conditional_tools_satisfied_without_execution": (
            satisfied_without_execution
        ),
    }

    if satisfied_without_execution:
        projection["conditional_satisfaction_reason"] = (
            "resolved_existing_entity_satisfies_create_if_absent_branch"
        )

    if not conditional_tools:
        return [], projection

    if not symbolic_target:
        # The downstream target is not resolved yet, so the conditional obligation
        # is dormant regardless of carry-forward. Preserve the tools as pending
        # so telemetry can show they were considered but not activated.
        projection["pending_conditional_required_tools"] = list(conditional_tools)
        return [], projection

    # Represented context-adjudication policy is authoritative when present.
    if obligation_carry_forward_permitted is False:
        projection["suppressed_prior_obligations"] = list(conditional_tools)
        projection["suppression_reason"] = (
            "represented_context_adjudication_suppressed_prior_obligation_carry_forward"
        )
        return [], projection
    if obligation_carry_forward_permitted is True:
        projection["carried_forward_obligations"] = list(conditional_tools)
        return list(conditional_tools), projection

    # No represented directive: fall back to the structural signal. A symbolic
    # target that the current turn did not resolve through its own execution can
    # only be an inherited prior referent, so its prior obligations must not
    # activate without an explicit continuation.
    if resolved_by_current_turn:
        projection["carried_forward_obligations"] = list(conditional_tools)
        return list(conditional_tools), projection

    projection["suppressed_prior_obligations"] = list(conditional_tools)
    projection["suppression_reason"] = (
        "conditional_required_tools_activated_only_by_carried_forward_prior_"
        "referents_without_explicit_continuation"
    )
    return [], projection
