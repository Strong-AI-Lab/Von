"""Per-turn decision-attribution projection (JVNAUTOSCI-2499).

This module is a telemetry support surface. It projects already-recorded turn
telemetry into one uniform decision-attribution shape so that routing, model
selection, dispatch, recovery, and acceptance decisions can be attributed to
represented authority (Vontology concepts, workflows, prompts, represented
policy) or to Python fallback code, per turn and in aggregate.

It makes no decisions of its own and adds no policy: it classifies the
provenance markers other components already emit, most importantly the
``annotate_python_decision_event`` envelope
(``decision_authority_origin == "python"``, ``decision_class``,
``decision_source``, ``changed_outcome``) from
``python_decision_authority_service`` and the model-policy resolution
telemetry (``policy_source``, ``graph_completeness``) from the orchestrator.

The aggregate ``architecture_integrity_score`` is the fraction of attributable
decisions whose authority was represented rather than Python fallback. Values
this projection cannot classify are reported as ``unknown`` rather than
guessed.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION = "turn_decision_attribution.v1"

DECISION_KINDS = (
    "discovery",
    "selection",
    "dispatch",
    "model_choice",
    "recovery",
    "acceptance",
)

AUTHORITY_REPRESENTED = "represented"
AUTHORITY_PYTHON_FALLBACK = "python_fallback"
AUTHORITY_SETTINGS_DEFAULT = "settings_default"
AUTHORITY_UNKNOWN = "unknown"
AUTHORITY_ABSENT = "absent"

# Stage names emitted by annotate_python_decision_event callers, mapped to the
# decision kind they belong to. This is provenance classification of Von's own
# telemetry enums, not behaviour policy.
_STAGE_TO_DECISION_KIND = {
    "workflow_discovery": "discovery",
    "selector_preparation": "selection",
    "selector_decision": "selection",
    "workflow_dispatch": "dispatch",
    "tool_recovery": "recovery",
    "turn_recovery": "recovery",
    "completion_gate": "acceptance",
}

# Known Python-authored selection provenance markers (see JVNAUTOSCI-2352
# staleness review and JVNAUTOSCI-2406 for the durable first-match residual).
_PYTHON_SELECTION_SOURCES = frozenset({"durable_discovery_fallback"})
_PYTHON_SELECTION_RATIONALES = frozenset({"first_routing_match"})

# Known Python recovery rationale markers (JVNAUTOSCI-2406 evidence and the
# orchestrator workflow-execute recovery path tracked under JVNAUTOSCI-2365).
_PYTHON_RECOVERY_MARKERS = frozenset(
    {
        "single_specialised_candidate_recovery_from_selector_fallback",
        "single_specialised_candidate_recovery_from_selector_prompt_unavailable",
        "workflow_execute_single_candidate_recovery",
    }
)

_DISCOVERY_NO_MATCH_ORIGINS = frozenset(
    {
        "durable_action_no_match_fallback",
        "durable_action_skipped_no_query",
    }
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _python_event_summary(entry: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "decision_class": _safe_str(entry.get("decision_class")),
        "decision_source": _safe_str(entry.get("decision_source")),
        "component": _safe_str(entry.get("component")),
        "function": _safe_str(entry.get("function")),
        "changed_outcome": bool(entry.get("changed_outcome")),
    }
    reason_code = _safe_str(entry.get("reason_code"))
    if reason_code:
        summary["reason_code"] = reason_code
    if entry.get("possible_inappropriate_python_code_use"):
        summary["possible_inappropriate_python_code_use"] = True
    return summary


def _collect_python_events_by_kind(
    aux_entries: Sequence[Mapping[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {kind: [] for kind in DECISION_KINDS}
    for entry in aux_entries or ():
        if not isinstance(entry, Mapping):
            continue
        if _safe_str(entry.get("decision_authority_origin")) != "python":
            continue
        stage = _safe_str(entry.get("stage"))
        kind = _STAGE_TO_DECISION_KIND.get(stage or "")
        reason_code = _safe_str(entry.get("reason_code")) or ""
        if kind is None and reason_code in _PYTHON_RECOVERY_MARKERS:
            kind = "recovery"
        if kind is None:
            continue
        events[kind].append(_python_event_summary(entry))
    return events


def _decision(
    kind: str,
    authority: str,
    *,
    authority_surface: str | None = None,
    concept_ids: Sequence[str] | None = None,
    evidence: Mapping[str, Any] | None = None,
    python_events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "decision_kind": kind,
        "authority": authority,
    }
    if authority_surface:
        record["authority_surface"] = authority_surface
    cleaned_concepts = [
        concept for concept in (concept_ids or ()) if _safe_str(concept)
    ]
    if cleaned_concepts:
        record["concept_ids"] = cleaned_concepts
    if evidence:
        record["evidence"] = {
            str(key): value for key, value in evidence.items() if value is not None
        }
    if python_events:
        record["python_decision_events"] = list(python_events)
    return record


def _attribute_discovery(
    diagnostics: Mapping[str, Any],
    python_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    discovery = _safe_mapping(diagnostics.get("workflow_discovery"))
    if discovery is None:
        return _decision("discovery", AUTHORITY_ABSENT, python_events=python_events)
    origin = _safe_str(discovery.get("discovery_payload_origin"))
    candidate_count = discovery.get("candidate_count")
    if not isinstance(candidate_count, int):
        candidates = discovery.get("candidates")
        candidate_count = len(candidates) if isinstance(candidates, list) else None
    evidence = {
        "discovery_payload_origin": origin,
        "query_source": _safe_str(discovery.get("query_source")),
        "candidate_count": candidate_count,
        "match_absence_reason": _safe_str(discovery.get("match_absence_reason")),
    }
    if (origin in _DISCOVERY_NO_MATCH_ORIGINS) or candidate_count == 0:
        return _decision(
            "discovery",
            AUTHORITY_ABSENT,
            evidence=evidence,
            python_events=python_events,
        )
    if any(event.get("changed_outcome") for event in python_events):
        return _decision(
            "discovery",
            AUTHORITY_PYTHON_FALLBACK,
            evidence=evidence,
            python_events=python_events,
        )
    return _decision(
        "discovery",
        AUTHORITY_REPRESENTED,
        authority_surface="workflow_discovery_service",
        evidence=evidence,
        python_events=python_events,
    )


def _routing_mapping(diagnostics: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read the routing section from either the assembled-diagnostics shape
    (``workflow_routing``) or the embedded chat-history debug shape
    (``workflow_routing_diagnostics``)."""

    return (
        _safe_mapping(diagnostics.get("workflow_routing"))
        or _safe_mapping(diagnostics.get("workflow_routing_diagnostics"))
        or {}
    )


def _attribute_selection(
    diagnostics: Mapping[str, Any],
    python_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    routing = _routing_mapping(diagnostics)
    selection = _safe_mapping(diagnostics.get("workflow_selection")) or {}
    selected_workflow_id = _safe_str(
        routing.get("selected_workflow_id")
        or selection.get("selected_workflow_id")
        or diagnostics.get("selected_workflow_id")
    )
    selector_source = _safe_str(
        routing.get("selector_source") or selection.get("selector_source")
    )
    selection_rationale = _safe_str(
        routing.get("selection_rationale") or selection.get("selection_rationale")
    )
    evidence = {
        "selector_source": selector_source,
        "selection_rationale": selection_rationale,
        "selected_workflow_id": selected_workflow_id,
    }
    if selected_workflow_id is None:
        return _decision(
            "selection",
            AUTHORITY_ABSENT,
            evidence=evidence,
            python_events=python_events,
        )
    if (
        (selector_source in _PYTHON_SELECTION_SOURCES)
        or (selection_rationale in _PYTHON_SELECTION_RATIONALES)
        or (selection_rationale in _PYTHON_RECOVERY_MARKERS)
        or any(event.get("changed_outcome") for event in python_events)
    ):
        return _decision(
            "selection",
            AUTHORITY_PYTHON_FALLBACK,
            evidence=evidence,
            python_events=python_events,
        )
    if selector_source:
        return _decision(
            "selection",
            AUTHORITY_REPRESENTED,
            authority_surface="workflow_selector",
            concept_ids=[selected_workflow_id],
            evidence=evidence,
            python_events=python_events,
        )
    return _decision(
        "selection",
        AUTHORITY_UNKNOWN,
        evidence=evidence,
        python_events=python_events,
    )


def _attribute_dispatch(
    diagnostics: Mapping[str, Any],
    python_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    # Dispatch preflight decisions resolved from the represented dispatch
    # policy (JVNAUTOSCI-2365) self-identify via decision_source.
    represented_policy_events = [
        event
        for event in python_events
        if _safe_str(event.get("decision_source")) == "represented_dispatch_policy"
    ]
    if represented_policy_events and len(represented_policy_events) == len(
        [
            e
            for e in python_events
            if _safe_str(e.get("decision_class"))
            == "workflow_dispatch_turn_contract_check"
        ]
    ):
        other_overrides = [
            event
            for event in python_events
            if event not in represented_policy_events and event.get("changed_outcome")
        ]
        if not other_overrides:
            return _decision(
                "dispatch",
                AUTHORITY_REPRESENTED,
                authority_surface="turn_contract_dispatch_policy",
                python_events=python_events,
            )
    override_events = [
        event
        for event in python_events
        if event.get("changed_outcome")
        or _safe_str(event.get("reason_code"))
        not in (
            None,
            "no_contract_requirements",
            "single_surface_contract",
            "no_external_surface_requirement",
            "selected_workflow_satisfies_contract",
            "non_custom_route_selected",
        )
    ]
    if override_events:
        return _decision(
            "dispatch",
            AUTHORITY_PYTHON_FALLBACK,
            evidence={"override_event_count": len(override_events)},
            python_events=python_events,
        )
    if python_events:
        # The Python preflight ran but left the represented selection standing.
        return _decision(
            "dispatch",
            AUTHORITY_REPRESENTED,
            authority_surface="turn_expected_outcome_contract",
            python_events=python_events,
        )
    return _decision("dispatch", AUTHORITY_UNKNOWN)


def _find_model_policy_telemetry(
    diagnostics: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    direct = _safe_mapping(diagnostics.get("workflow_model_policy"))
    if direct is not None:
        return direct
    for container_key in ("llm_debug", "selected_workflow_trace"):
        container = _safe_mapping(diagnostics.get(container_key))
        if container is None:
            continue
        found = _safe_mapping(container.get("workflow_model_policy"))
        if found is not None:
            return found
        metadata = _safe_mapping(container.get("metadata"))
        if metadata is not None:
            found = _safe_mapping(metadata.get("workflow_model_policy"))
            if found is not None:
                return found
    return None


def _attribute_model_choice(
    diagnostics: Mapping[str, Any],
    python_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    telemetry = _find_model_policy_telemetry(diagnostics)
    if telemetry is None:
        return _decision("model_choice", AUTHORITY_UNKNOWN, python_events=python_events)
    policy_source = _safe_str(telemetry.get("policy_source"))
    policy_id = _safe_str(telemetry.get("policy_id"))
    evidence = {
        "policy_source": policy_source,
        "graph_completeness": _safe_str(telemetry.get("graph_completeness")),
        "policy_id": policy_id,
    }
    if policy_source == "graph":
        return _decision(
            "model_choice",
            AUTHORITY_REPRESENTED,
            authority_surface="vontology_model_policy_graph",
            concept_ids=[policy_id] if policy_id else None,
            evidence=evidence,
            python_events=python_events,
        )
    if policy_source == "json":
        return _decision(
            "model_choice",
            AUTHORITY_REPRESENTED,
            authority_surface="vontology_model_policy_json_text_relation",
            concept_ids=[policy_id] if policy_id else None,
            evidence=evidence,
            python_events=python_events,
        )
    if policy_source == "disabled":
        # Model choice follows the user/org-selected active LLM settings.
        return _decision(
            "model_choice",
            AUTHORITY_SETTINGS_DEFAULT,
            evidence=evidence,
            python_events=python_events,
        )
    return _decision(
        "model_choice",
        AUTHORITY_PYTHON_FALLBACK,
        evidence=evidence,
        python_events=python_events,
    )


def _attribute_recovery(
    diagnostics: Mapping[str, Any],
    python_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    routing = _routing_mapping(diagnostics)
    selection_rationale = _safe_str(routing.get("selection_rationale"))
    marker_hit = selection_rationale in _PYTHON_RECOVERY_MARKERS
    if python_events or marker_hit:
        evidence = {"selection_rationale": selection_rationale} if marker_hit else None
        return _decision(
            "recovery",
            AUTHORITY_PYTHON_FALLBACK,
            evidence=evidence,
            python_events=python_events,
        )
    return _decision("recovery", AUTHORITY_ABSENT)


def _completion_gate_from_container(
    container: Mapping[str, Any] | None,
    *,
    source_prefix: str,
) -> tuple[Mapping[str, Any] | None, str | None]:
    if container is None:
        return None, None
    turn_record = _safe_mapping(container.get("turn_execution_record"))
    if turn_record is not None:
        gate = _safe_mapping(turn_record.get("completion_gate"))
        if gate is not None:
            return gate, f"{source_prefix}turn_execution_record.completion_gate"
    gate = _safe_mapping(container.get("completion_gate_verdict"))
    if gate is not None:
        return gate, f"{source_prefix}completion_gate_verdict"
    gate = _safe_mapping(container.get("completion_gate"))
    if gate is not None:
        return gate, f"{source_prefix}completion_gate"
    return None, None


def _find_final_completion_gate(
    diagnostics: Mapping[str, Any],
) -> tuple[
    Mapping[str, Any] | None,
    str | None,
    Mapping[str, Any] | None,
]:
    """Return the strongest recorded final-gate projection.

    A persisted Turn Execution Record is canonical.  The explicit verdict
    projection is next because it is assembled after iterative completion-gate
    work; the raw ``completion_gate`` mapping can describe an earlier retry.
    """

    direct_record = _safe_mapping(diagnostics.get("turn_execution_record"))
    if direct_record is not None:
        gate = _safe_mapping(direct_record.get("completion_gate"))
        if gate is not None:
            return gate, "turn_execution_record.completion_gate", direct_record

    llm_debug = _safe_mapping(diagnostics.get("llm_debug"))
    debug_record = (
        _safe_mapping(llm_debug.get("turn_execution_record"))
        if llm_debug is not None
        else None
    )
    if debug_record is not None:
        gate = _safe_mapping(debug_record.get("completion_gate"))
        if gate is not None:
            return (
                gate,
                "llm_debug.turn_execution_record.completion_gate",
                debug_record,
            )

    direct_verdict = _safe_mapping(diagnostics.get("completion_gate_verdict"))
    if direct_verdict is not None:
        return direct_verdict, "completion_gate_verdict", diagnostics
    if llm_debug is not None:
        debug_verdict = _safe_mapping(llm_debug.get("completion_gate_verdict"))
        if debug_verdict is not None:
            return debug_verdict, "llm_debug.completion_gate_verdict", llm_debug

    gate, source = _completion_gate_from_container(diagnostics, source_prefix="")
    if gate is not None:
        return gate, source, diagnostics
    gate, source = _completion_gate_from_container(
        llm_debug,
        source_prefix="llm_debug.",
    )
    return gate, source, llm_debug


def _receipt_from_container(
    container: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    if container is None:
        return None, None
    receipt = _safe_mapping(container.get("terminal_outcome_receipt"))
    validation = _safe_mapping(container.get("terminal_outcome_receipt_validation"))
    if receipt is not None:
        return receipt, validation
    evidence = _safe_mapping(container.get("evidence_payload"))
    if evidence is None:
        return None, validation
    return (
        _safe_mapping(evidence.get("terminal_outcome_receipt")),
        _safe_mapping(evidence.get("terminal_outcome_receipt_validation"))
        or validation,
    )


def _find_terminal_outcome_receipt(
    gate: Mapping[str, Any],
    gate_container: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Read receipt evidence only from the selected gate's priority container."""

    receipt, validation = _receipt_from_container(gate)
    if receipt is not None:
        return receipt, validation
    return _receipt_from_container(gate_container)


def _represented_terminal_success(
    *,
    gate: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    validation: Mapping[str, Any] | None,
) -> bool:
    """Check provenance and final safety facts without judging task semantics."""

    if receipt is None or validation is None:
        return False
    provenance = _safe_mapping(receipt.get("provenance")) or {}
    gate_decision = _safe_str(
        gate.get("decision")
        or gate.get("status")
        or gate.get("verdict")
        or gate.get("state")
    )
    return bool(
        gate_decision
        in {"complete", "completed", "pass", "passed", "success", "succeeded"}
        and gate.get("safe_to_claim_completion") is True
        and gate.get("requires_follow_up") is not True
        and validation.get("present") is True
        and validation.get("valid") is True
        and _safe_str(validation.get("decision_authority")) == "represented_llm"
        and _safe_str(receipt.get("outcome")) == "verified_success"
        and _safe_str(provenance.get("decision_source")) == "represented_llm"
    )


def _attribute_acceptance(
    diagnostics: Mapping[str, Any],
    python_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    gate, gate_source, gate_container = _find_final_completion_gate(diagnostics)
    if gate is None:
        return _decision("acceptance", AUTHORITY_ABSENT, python_events=python_events)
    verdict_source = _safe_str(
        gate.get("verdict_source") or gate.get("source") or gate.get("gate_source")
    )
    evidence: dict[str, Any] = {
        "verdict_source": verdict_source,
        "status": _safe_str(
            gate.get("status")
            or gate.get("state")
            or gate.get("verdict")
            or gate.get("decision")
        ),
        "completion_gate_source": gate_source,
    }
    changed_python_events = [
        event for event in python_events if event.get("changed_outcome")
    ]
    receipt, receipt_validation = _find_terminal_outcome_receipt(
        gate,
        gate_container,
    )
    if _represented_terminal_success(
        gate=gate,
        receipt=receipt,
        validation=receipt_validation,
    ):
        assert receipt is not None
        assert receipt_validation is not None
        provenance = _safe_mapping(receipt.get("provenance")) or {}
        authority_surface = (
            _safe_str(provenance.get("workflow_id"))
            or _safe_str(provenance.get("prompt_concept_id"))
            or "terminal_outcome_receipt"
        )
        evidence.update(
            {
                "terminal_outcome": _safe_str(receipt.get("outcome")),
                "terminal_outcome_decision_authority": _safe_str(
                    receipt_validation.get("decision_authority")
                ),
                "superseded_python_guardrail_event_count": len(changed_python_events),
            }
        )
        return _decision(
            "acceptance",
            AUTHORITY_REPRESENTED,
            authority_surface=authority_surface,
            concept_ids=[
                concept_id
                for concept_id in (
                    _safe_str(provenance.get("workflow_id")),
                    _safe_str(provenance.get("prompt_concept_id")),
                    _safe_str(receipt.get("profile_concept_id")),
                )
                if concept_id
            ],
            evidence=evidence,
            python_events=python_events,
        )
    if changed_python_events:
        evidence["python_changed_outcome_event_count"] = len(changed_python_events)
        return _decision(
            "acceptance",
            AUTHORITY_PYTHON_FALLBACK,
            evidence=evidence,
            python_events=python_events,
        )
    if verdict_source and ("critic" in verdict_source or "contract" in verdict_source):
        return _decision(
            "acceptance",
            AUTHORITY_REPRESENTED,
            authority_surface=verdict_source,
            evidence=evidence,
            python_events=python_events,
        )
    return _decision(
        "acceptance",
        AUTHORITY_UNKNOWN,
        evidence=evidence,
        python_events=python_events,
    )


def build_turn_decision_attribution(
    *,
    diagnostics: Mapping[str, Any] | None,
    aux_entries: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project recorded turn telemetry into per-decision authority attribution."""

    diagnostics_mapping = _safe_mapping(diagnostics) or {}
    events_by_kind = _collect_python_events_by_kind(aux_entries)

    decisions = [
        _attribute_discovery(diagnostics_mapping, events_by_kind["discovery"]),
        _attribute_selection(diagnostics_mapping, events_by_kind["selection"]),
        _attribute_dispatch(diagnostics_mapping, events_by_kind["dispatch"]),
        _attribute_model_choice(diagnostics_mapping, events_by_kind["model_choice"]),
        _attribute_recovery(diagnostics_mapping, events_by_kind["recovery"]),
        _attribute_acceptance(diagnostics_mapping, events_by_kind["acceptance"]),
    ]

    breakdown = {
        decision["decision_kind"]: decision["authority"] for decision in decisions
    }
    represented_count = sum(
        1 for decision in decisions if decision["authority"] == AUTHORITY_REPRESENTED
    )
    python_fallback_count = sum(
        1
        for decision in decisions
        if decision["authority"] == AUTHORITY_PYTHON_FALLBACK
    )
    unknown_count = sum(
        1 for decision in decisions if decision["authority"] == AUTHORITY_UNKNOWN
    )
    attributable = represented_count + python_fallback_count
    score = (represented_count / attributable) if attributable else None

    return {
        "schema_version": TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION,
        "decisions": decisions,
        "summary": {
            "decision_kind_breakdown": breakdown,
            "represented_count": represented_count,
            "python_fallback_count": python_fallback_count,
            "unknown_count": unknown_count,
            "attributable_decision_count": attributable,
            "architecture_integrity_score": score,
        },
    }


TURN_DECISION_ATTRIBUTION_AGGREGATE_SCHEMA_VERSION = (
    "turn_decision_attribution_aggregate.v1"
)


def aggregate_turn_decision_attributions(
    attributions: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Aggregate per-turn attribution payloads across a run set.

    Reports the mean architecture-integrity score over turns that had
    attributable decisions, per-kind authority counts, and a histogram of the
    Python-fallback signatures observed (decision kind plus the strongest
    available provenance marker), so trends can be tied back to specific
    code seams.
    """

    turn_count = 0
    scored_turn_count = 0
    score_total = 0.0
    kind_authority_counts: dict[str, dict[str, int]] = {
        kind: {} for kind in DECISION_KINDS
    }
    fallback_signatures: dict[str, int] = {}

    for attribution in attributions or ():
        if not isinstance(attribution, Mapping):
            continue
        turn_count += 1
        summary = _safe_mapping(attribution.get("summary")) or {}
        score = summary.get("architecture_integrity_score")
        if isinstance(score, (int, float)):
            scored_turn_count += 1
            score_total += float(score)
        for decision in attribution.get("decisions") or ():
            if not isinstance(decision, Mapping):
                continue
            kind = _safe_str(decision.get("decision_kind"))
            authority = _safe_str(decision.get("authority"))
            if not kind or not authority or kind not in kind_authority_counts:
                continue
            counts = kind_authority_counts[kind]
            counts[authority] = counts.get(authority, 0) + 1
            if authority != AUTHORITY_PYTHON_FALLBACK:
                continue
            marker = None
            events = decision.get("python_decision_events")
            if isinstance(events, list) and events:
                first = _safe_mapping(events[0]) or {}
                marker = _safe_str(first.get("function")) or _safe_str(
                    first.get("decision_class")
                )
            if marker is None:
                evidence = _safe_mapping(decision.get("evidence")) or {}
                marker = (
                    _safe_str(evidence.get("selector_source"))
                    or _safe_str(evidence.get("selection_rationale"))
                    or _safe_str(evidence.get("policy_source"))
                    or "unattributed"
                )
            signature = f"{kind}:{marker}"
            fallback_signatures[signature] = fallback_signatures.get(signature, 0) + 1

    return {
        "schema_version": TURN_DECISION_ATTRIBUTION_AGGREGATE_SCHEMA_VERSION,
        "turn_count": turn_count,
        "scored_turn_count": scored_turn_count,
        "mean_architecture_integrity_score": (
            (score_total / scored_turn_count) if scored_turn_count else None
        ),
        "decision_kind_authority_counts": kind_authority_counts,
        "python_fallback_signatures": dict(
            sorted(
                fallback_signatures.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ),
    }


__all__ = [
    "AUTHORITY_ABSENT",
    "AUTHORITY_PYTHON_FALLBACK",
    "AUTHORITY_REPRESENTED",
    "AUTHORITY_SETTINGS_DEFAULT",
    "AUTHORITY_UNKNOWN",
    "DECISION_KINDS",
    "TURN_DECISION_ATTRIBUTION_AGGREGATE_SCHEMA_VERSION",
    "TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION",
    "aggregate_turn_decision_attributions",
    "build_turn_decision_attribution",
]
