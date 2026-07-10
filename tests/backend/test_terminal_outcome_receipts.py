import pytest

from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
)
from src.backend.workflows.terminal_outcome_receipts import (
    apply_terminal_outcome_receipt_to_completion_gate,
    build_terminal_outcome_receipt_projection,
    terminal_outcome_receipt_projection_from_record,
    validate_terminal_outcome_receipt,
)


def _receipt(**overrides):
    receipt = {
        "schema_version": "terminal_outcome_receipt.v1",
        "profile_concept_id": "#V#terminal_outcome_receipt",
        "outcome": "recoverable_failure",
        "cause_code": "represented_capability_temporarily_unavailable",
        "causal_stage": "invocation",
        "summary": "The attempted action did not establish its expected effect.",
        "evidence_refs": [{"kind": "invocation", "ref": "call-1"}],
        "committed_effects": [{"effect_id": "effect-1", "status": "satisfied"}],
        "remaining_obligations": [{"effect_id": "effect-2", "status": "not_satisfied"}],
        "retryability": "now",
        "recovery_affordances": [
            {"action_type": "inspect", "target_ref": "call-1"},
            {"action_type": "alternate_tool", "target_ref": "#V#tool_alternative"},
        ],
        "learning_candidate": None,
        "redaction_status": "contains_no_sensitive_values",
        "provenance": {
            "decision_source": "represented_llm",
            "workflow_id": "#V#synthetic_workflow",
            "prompt_concept_id": "#V#prompt_turn_execution_postcondition_critic",
        },
    }
    receipt.update(overrides)
    return receipt


def test_validates_represented_llm_receipt_without_semantic_reclassification() -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        _receipt(),
        completion_gate={"safe_to_claim_completion": False},
        required=True,
    )

    assert validation["valid"] is True
    assert validation["decision_authority"] == "represented_llm"
    assert receipt is not None
    assert receipt["outcome"] == "recoverable_failure"
    assert [item["action_type"] for item in receipt["recovery_affordances"]] == [
        "inspect",
        "alternate_tool",
    ]


def test_redacts_sensitive_values_in_receipt_projection() -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        _receipt(
            evidence_refs=[
                {
                    "kind": "invocation",
                    "ref": "call-1",
                    "access_token": "must-not-survive",
                }
            ]
        ),
        required=True,
    )

    assert validation["valid"] is True
    assert validation["redacted_field_count"] == 1
    assert receipt is not None
    assert receipt["redaction_status"] == "redacted"
    assert receipt["evidence_refs"][0]["access_token"] == "[redacted]"


def test_hard_gate_can_veto_but_not_invent_verified_success() -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        _receipt(
            outcome="verified_success",
            cause_code=None,
            causal_stage="not_applicable",
            retryability="not_applicable",
            recovery_affordances=[],
        ),
        completion_gate={"safe_to_claim_completion": False},
        required=True,
    )

    assert receipt is None
    assert validation["valid"] is False
    assert (
        "terminal_outcome_receipt_verified_success_conflicts_with_gate"
        in validation["errors"]
    )


def test_projects_authored_non_success_while_preserving_recovery_evidence() -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        _receipt(),
        completion_gate={"safe_to_claim_completion": True},
        required=True,
    )
    projected = apply_terminal_outcome_receipt_to_completion_gate(
        {"safe_to_claim_completion": True, "requires_follow_up": False},
        receipt=receipt,
        validation=validation,
        authoritative_receipt_required=True,
    )

    assert projected["decision"] == "recoverable_failure"
    assert projected["safe_to_claim_completion"] is False
    assert projected["requires_follow_up"] is True
    persisted = projected["evidence_payload"]["terminal_outcome_receipt"]
    assert persisted["committed_effects"] == [
        {"effect_id": "effect-1", "status": "satisfied"}
    ]
    assert len(persisted["recovery_affordances"]) == 2


def test_preserves_represented_workflow_default_provenance() -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        _receipt(
            provenance={"decision_source": "represented_workflow_default"},
        ),
        completion_gate={"safe_to_claim_completion": True},
        required=True,
    )
    projected = apply_terminal_outcome_receipt_to_completion_gate(
        {"safe_to_claim_completion": True, "requires_follow_up": False},
        receipt=receipt,
        validation=validation,
        authoritative_receipt_required=True,
    )

    assert projected["terminal_outcome_receipt_source"] == (
        "represented_workflow_default"
    )


def test_required_invalid_receipt_fails_closed_with_typed_diagnostics() -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        None,
        completion_gate={"safe_to_claim_completion": True},
        required=True,
    )
    projected = apply_terminal_outcome_receipt_to_completion_gate(
        {"safe_to_claim_completion": True, "requires_follow_up": False},
        receipt=receipt,
        validation=validation,
        authoritative_receipt_required=True,
    )

    assert projected["safe_to_claim_completion"] is False
    assert projected["requires_follow_up"] is True
    assert projected["terminal_outcome_receipt_source"] == "missing_or_invalid"
    assert "terminal_outcome_receipt_missing" in projected["blocking_failure_codes"]


def test_turn_execution_record_persists_the_critic_authored_receipt() -> None:
    authored_receipt = _receipt()
    record = build_turn_execution_record(
        request_id="req-terminal-receipt",
        session_id="session-terminal-receipt",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Attempt the represented task.",
        response_text="A candidate response.",
        interaction_timestamp_utc="2026-07-11T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#synthetic_workflow",
            "verdict": "represented_workflow_selected",
            "source": "selector",
        },
        critic_verdict={
            "verdict": "follow_up_required",
            "terminal_outcome_receipt": authored_receipt,
        },
    )

    assert record["terminal_outcome_receipt"] == authored_receipt
    assert record["terminal_outcome_receipt_validation"]["valid"] is True
    assert record["completion_gate"]["decision"] == "recoverable_failure"
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    ledger_projection = record["execution"]["tool_observation_ledger"][
        "terminal_outcome_receipt_projection"
    ]
    assert ledger_projection["available"] is True
    assert ledger_projection["outcome"] == "recoverable_failure"
    assert ledger_projection["source"] == "tool_observation_ledger"


@pytest.mark.parametrize(
    ("outcome", "causal_stage", "cause_code", "retryability", "affordance"),
    [
        ("verified_success", "not_applicable", None, "not_applicable", None),
        ("verified_partial", "persistence", "partial_effects", "now", "resume"),
        ("input_required", "planning", "required_input_missing", "after_input", "elicit_input"),
        (
            "externally_blocked",
            "invocation",
            "authorisation_denied",
            "after_external_change",
            "inspect",
        ),
        ("recoverable_failure", "retrieval", "empty_result", "now", "alternate_tool"),
        (
            "terminal_failure",
            "verification",
            "forbidden_target",
            "not_safely_recoverable",
            "escalate",
        ),
        ("cancelled", "execution", "execution_interrupted", "now", "resume"),
        ("inconclusive", "unknown", "causal_evidence_missing", "now", "inspect"),
    ],
)
def test_accepts_the_full_represented_terminal_outcome_family(
    outcome: str,
    causal_stage: str,
    cause_code: str | None,
    retryability: str,
    affordance: str | None,
) -> None:
    receipt, validation = validate_terminal_outcome_receipt(
        _receipt(
            outcome=outcome,
            causal_stage=causal_stage,
            cause_code=cause_code,
            retryability=retryability,
            recovery_affordances=(
                [{"action_type": affordance}] if affordance else []
            ),
        ),
        completion_gate={"safe_to_claim_completion": True},
        required=True,
    )

    assert validation["valid"] is True
    assert receipt is not None
    assert receipt["outcome"] == outcome
    assert receipt["causal_stage"] == causal_stage
    assert receipt["retryability"] == retryability


def test_projection_preserves_partial_effects_and_idempotent_reuse_evidence() -> None:
    projection = build_terminal_outcome_receipt_projection(
        _receipt(
            outcome="verified_partial",
            cause_code="remaining_readback_pending",
            causal_stage="verification",
            committed_effects=[
                {
                    "effect_id": "effect-reuse",
                    "effect_type": "idempotent_reuse",
                    "authoritative_postcondition_rechecked": True,
                    "status": "satisfied",
                }
            ],
            remaining_obligations=[
                {"effect_id": "effect-readback", "status": "not_satisfied"}
            ],
            retryability="now",
            recovery_affordances=[{"action_type": "resume"}],
        ),
        source="test.partial_idempotent_reuse",
    )

    assert projection["available"] is True
    assert projection["outcome"] == "verified_partial"
    assert projection["committed_effects"][0]["effect_type"] == "idempotent_reuse"
    assert projection["committed_effects"][0][
        "authoritative_postcondition_rechecked"
    ] is True
    assert projection["remaining_obligations"] == [
        {"effect_id": "effect-readback", "status": "not_satisfied"}
    ]


def test_record_projection_falls_back_to_completion_gate_evidence() -> None:
    projection = terminal_outcome_receipt_projection_from_record(
        {
            "completion_gate": {
                "evidence_payload": {
                    "terminal_outcome_receipt": _receipt(),
                }
            }
        },
        source="test.legacy_record",
    )

    assert projection["available"] is True
    assert projection["outcome"] == "recoverable_failure"
    assert projection["source"] == "test.legacy_record"
