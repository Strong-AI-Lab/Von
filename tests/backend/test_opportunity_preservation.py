"""Opportunity-preservation regression matrix (JVNAUTOSCI-2558, parent 2553).

The parent epic exists because Von support layers kept converting recoverable
states into Python-driven irrecoverable failures, removing opportunities for a
reasonably intelligent model to recover, inspect, retry, or return a useful
partial result. These tests pin the *support-layer invariants* that keep those
opportunities open, so a future patch cannot quietly restore a barrier.

Every assertion here is domain-agnostic on purpose. The barrier classes are
exercised through the generic support functions with synthetic, non-domain
workflow/tool identifiers. Domain success policy (arXiv optional-PDF, mail,
Jira, etc.) lives in represented workflow/prompt/Vontology artefacts, never in
these tests or the support code they exercise.

Barrier classes (from JVNAUTOSCI-2553):
  - schema tolerance at tool gateways (2554)
  - timeout / partial-success separation and visibility (2555)
  - completion accounting / workflow-owned obligations (2556) and prior-turn
    obligation carry-forward suppression (2563)
  - degraded / optional-branch terminal success preserved by represented
    terminal-success contracts (2557)
"""

from src.backend.integrations.internal_mcp.catalogue import (
    _bool_input_normalisation_record,
    _coerce_bool_input,
)
from src.backend.workflows.terminal_success_contracts import (
    evaluate_workflow_terminal_success_contract,
)
from src.backend.services.turn_expected_outcome_obligation_carry_forward import (
    adjudicate_conditional_required_tool_activation,
)
from src.backend.workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
)


# --- schema tolerance: a represented workflow is not denied a fair run for a
#     model-natural boolean-like input (JVNAUTOSCI-2554) -----------------------


def test_boolean_like_inputs_do_not_deny_a_fair_run() -> None:
    # Model-natural string/int spellings coerce instead of raising a schema error.
    for truthy in ("true", "True", "1", "yes", "on", 1, True):
        assert _coerce_bool_input(truthy, default=False) is True
    for falsy in ("false", "0", "no", "off", 0, False):
        assert _coerce_bool_input(falsy, default=True) is False


def test_unrecognised_input_falls_back_to_safe_default_not_error() -> None:
    # An unrecognised value uses the default rather than becoming a terminal
    # schema failure that blocks the workflow before it runs.
    assert _coerce_bool_input("terminal", default=False) is False
    assert _coerce_bool_input(object(), default=True) is True


def test_boolean_coercion_is_telemetry_visible() -> None:
    # The coercion decision is recorded so a reviewer/model can see what happened.
    record = _bool_input_normalisation_record("true", normalised=True, default=False)
    assert record["supplied"] is True
    assert record["recognised"] is True
    assert record["coerced"] is True
    assert record["used_default"] is False

    unrecognised = _bool_input_normalisation_record(
        "terminal", normalised=False, default=False
    )
    assert unrecognised["recognised"] is False
    assert unrecognised["used_default"] is True


# --- degraded / optional-branch terminal success is preserved by the
#     represented terminal-success contract (JVNAUTOSCI-2557, generic) --------

_DEGRADED_SUCCESS_CONTRACT = {
    "schema_version": "workflow_terminal_success_contract.v1",
    "success_statuses": ["completed"],
    "required_summary_fields": [
        "workflow_id",
        "terminal_status",
        "final_state",
        "completed",
    ],
    "require_terminal_status": True,
    "require_final_state": True,
    "require_completed_true": True,
    # The optional-branch policy: reaching a terminal completed state is success
    # even if the outer completion gate could not mark itself "safe" (e.g. an
    # optional enrichment action failed). This is the arXiv optional-PDF policy
    # expressed generically.
    "require_completion_gate_safe_to_claim_completion": False,
}


def _degraded_summary():
    return {
        "workflow_id": "#V#synthetic_representation_workflow",
        "terminal_status": "completed",
        "final_state": "#V#workflow_step_synthetic_representation_workflow_completed",
        "completed": True,
        # An optional enrichment action failed, so the gate is not "safe".
        "completion_gate_safe_to_claim_completion": False,
    }


def test_optional_branch_reaches_terminal_success_despite_unsafe_gate() -> None:
    evaluation = evaluate_workflow_terminal_success_contract(
        contract=_DEGRADED_SUCCESS_CONTRACT,
        execution_summary=_degraded_summary(),
    )
    assert evaluation is not None
    assert evaluation["success"] is True
    assert evaluation["failure_codes"] == []


def test_contract_that_requires_safe_gate_fails_closed_not_falsely_succeeds() -> None:
    # The converse invariant: when a workflow's own contract *does* require the
    # safe gate, an unsafe gate fails closed rather than being papered over.
    strict_contract = {
        **_DEGRADED_SUCCESS_CONTRACT,
        "require_completion_gate_safe_to_claim_completion": True,
    }
    evaluation = evaluate_workflow_terminal_success_contract(
        contract=strict_contract,
        execution_summary=_degraded_summary(),
    )
    assert evaluation is not None
    assert evaluation["success"] is False
    assert "contracted_workflow_completion_gate_not_safe" in evaluation["failure_codes"]


def test_non_terminal_status_is_not_blessed_as_success() -> None:
    # Opportunity preservation must not become "bless any failure": a workflow
    # that did not reach a success status still fails.
    summary = {**_degraded_summary(), "terminal_status": "error"}
    evaluation = evaluate_workflow_terminal_success_contract(
        contract=_DEGRADED_SUCCESS_CONTRACT,
        execution_summary=summary,
    )
    assert evaluation is not None
    assert evaluation["success"] is False


# --- completion accounting: a bare follow-up keeps its recovery affordance
#     (a narrow answer) instead of inheriting stale prior obligations, and the
#     suppression stays telemetry-visible (JVNAUTOSCI-2556 / 2563) ------------


def test_stale_prior_obligations_do_not_remove_a_narrow_answer_opportunity() -> None:
    contract = TurnExpectedOutcomeContract(
        conditional_required_tools=("some_prior_mutation_tool", "some_prior_readback_tool"),
        target_concept_ids=("#V#carried_referent_concept",),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    # No stale obligation blocks the narrow current-intent answer...
    assert activated == []
    # ...the referent is still available for the current answer...
    assert projection["carried_forward_referents"]["target_concept_ids"] == [
        "#V#carried_referent_concept"
    ]
    # ...and the suppression is visible in telemetry, not silent.
    assert projection["suppressed_prior_obligations"] == [
        "some_prior_mutation_tool",
        "some_prior_readback_tool",
    ]
    assert projection["suppression_reason"]
