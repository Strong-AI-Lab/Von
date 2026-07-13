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
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.workflows.mcp_tool_bridge import (
    apply_runtime_defaults_to_mcp_payload,
)
from src.backend.workflows.terminal_success_contracts import (
    evaluate_workflow_terminal_success_contract,
)
from src.backend.workflows.terminal_outcome_receipts import (
    apply_terminal_outcome_receipt_to_completion_gate,
    validate_terminal_outcome_receipt,
)
from src.backend.services.turn_expected_outcome_obligation_carry_forward import (
    adjudicate_conditional_required_tool_activation,
)
from src.backend.services.rag_service import build_rag_retrieval_state
from src.backend.services.required_tool_obligation_service import (
    build_required_tool_obligation_ledger,
)
from src.backend.services import tool_target_contract_validation as target_validation
from src.backend.services.tool_metadata_service import (
    ToolRequiredObligationMetadata,
)
from src.backend.services.operational_learning_release_service import (
    project_learning_release_recovery_affordances,
)
from src.backend.services.operational_learning_release_vontology_service import (
    LearningReleaseStateConflictError,
    LearningReleasePersistenceError,
    _apply_and_readback_release_activation,
)
from src.backend.workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
)


# --- retrieval barriers remain inspectable and recoverable ------------------


def test_recovery_schema_hygiene_preserves_valid_verification_read_opportunity() -> (
    None
):
    payload = {
        "query": "synthetic target",
        "contract_advisory": "exact",
    }

    bindings = apply_runtime_defaults_to_mcp_payload(
        payload,
        tool_name="synthetic_verification_lookup",
        input_schema=Schema(
            required={"query": str},
            optional={"namespace": (str, type(None))},
            allow_unknown=True,
        ),
        user_namespace="#V#tester@test_org",
        default_gmail_profile=None,
        strip_unknown_fields=True,
    )

    assert payload == {
        "query": "synthetic target",
        "namespace": "#V#tester@test_org",
    }
    assert {
        (entry.get("field"), entry.get("source")) for entry in bindings
    } >= {
        ("namespace", "user_namespace"),
        ("contract_advisory", "removed_for_strict_tool_schema"),
    }


def test_retrieval_incompatibility_is_not_collapsed_into_authoritative_empty() -> None:
    blocked = build_rag_retrieval_state(
        "embedding_signature_mismatch",
        cause="synthetic_signature_mismatch",
    )
    empty = build_rag_retrieval_state(
        "valid_empty",
        cause="synthetic_query_completed",
    )

    assert blocked["usable"] is False
    assert blocked["authoritative_empty"] is False
    assert blocked["rebuild_required"] is True
    assert blocked["recovery_affordances"] == [
        {"action_type": "rebuild_namespace_index"}
    ]

    assert empty["usable"] is True
    assert empty["authoritative_empty"] is True
    assert empty["rebuild_required"] is False


# --- terminal receipts retain represented recovery opportunities ------------


def test_equivalent_execution_surface_is_not_erased_by_gateway_name_mismatch() -> (
    None
):
    action_id = "synthetic_family.verify_effect"
    ledger = build_required_tool_obligation_ledger(
        required_tools=[action_id],
        allowed_tools=[action_id],
        method_catalogue={},
        observed_equivalent_successful_executions=[
            {
                "action_id": action_id,
                "workflow_id": "#V#synthetic_workflow",
                "status": "success",
            }
        ],
    )

    obligation = ledger["obligations"][0]
    assert obligation["available_on_gateway"] is False
    assert obligation["available_on_any_surface"] is True
    assert obligation["availability_surfaces"] == [
        "observed_invocation",
        "workflow_action",
    ]
    assert obligation["satisfied"] is True
    assert obligation["blocking_reason"] == ""


def test_grounded_read_evidence_preserves_target_inspection_opportunity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        target_validation,
        "get_tool_required_obligation_metadata",
        lambda _tool_name: ToolRequiredObligationMetadata(
            operation_class="verification_read",
            target_argument_names=("target_id",),
        ),
    )

    result = target_validation.validate_tool_target_contract(
        tool_name="synthetic.verify_target",
        payload={"target_id": "#V#synthetic_grounded_target"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "entity",
                    "text": "the target named in the request",
                    "resolution_status": "unresolved",
                }
            ]
        },
        prior_tool_invocations=[
            {
                "tool": "synthetic.resolve_target",
                "status": "ok",
                "effective_payload": {
                    "result": {
                        "resolved_concept_id": "#V#synthetic_grounded_target"
                    }
                },
            }
        ],
    )

    assert result.ok is True
    assert result.resolution_evidence[0]["resolution_scope"] == (
        "verification_read_only"
    )
    assert result.resolution_evidence[0]["evidence"] == [
        {
            "concept_id": "#V#synthetic_grounded_target",
            "tool": "synthetic.resolve_target",
            "result_path": "effective_payload.result.resolved_concept_id",
        }
    ]


def test_rejected_learning_release_preserves_active_pointer_and_recovery_options() -> (
    None
):
    active = {
        "release_id": "release-active",
        "release_sha256": "a" * 64,
        "candidate_id": "candidate-active",
    }

    affordances = project_learning_release_recovery_affordances(
        action="reject",
        prior_active=active,
        resulting_active=active,
        resulting_previous=None,
    )

    assert {item["action_type"] for item in affordances} == {
        "inspect_release_evidence",
        "retest_candidate",
        "revise_candidate",
        "retain_active_release",
    }
    retained = next(
        item for item in affordances if item["action_type"] == "retain_active_release"
    )
    assert retained["release"] == active


def test_learning_release_optimistic_conflict_preserves_inspect_and_retry_paths() -> (
    None
):
    conflict = LearningReleaseStateConflictError(
        expected_version=3,
        expected_state_sha256="a" * 64,
        current_version=4,
        current_state_sha256="b" * 64,
        state_concept_id="#V#operational_learning_release_state_synthetic",
    ).to_dict()

    assert conflict["details"]["current_version"] == 4
    assert conflict["details"]["current_state_sha256"] == "b" * 64
    assert {
        item["action_type"] for item in conflict["recovery_affordances"]
    } == {"read_latest_state", "retry_with_latest_version"}


def test_missing_release_activation_adapter_retains_active_release_opportunity() -> (
    None
):
    try:
        _apply_and_readback_release_activation(
            {
                "candidate_id": "candidate-synthetic",
                "affected_artifact": "#V#synthetic_artifact",
                "release_sha256": "a" * 64,
            }
        )
    except LearningReleasePersistenceError as exc:
        projection = exc.to_dict()
    else:  # pragma: no cover - the default adapter must fail closed
        raise AssertionError("missing activation adapter unexpectedly succeeded")

    assert projection["details"]["decision_state"] == (
        "promotion_approved_not_activated"
    )
    assert {
        item["action_type"] for item in projection["recovery_affordances"]
    } == {"register_canonical_release_activation_adapter", "retain_active_release"}


# --- terminal receipts retain represented recovery opportunities ------------


def test_terminal_receipt_preserves_recovery_affordances_and_committed_effects() -> (
    None
):
    authored_receipt = {
        "schema_version": "terminal_outcome_receipt.v1",
        "profile_concept_id": "#V#terminal_outcome_receipt",
        "outcome": "verified_partial",
        "cause_code": "remaining_obligation_unverified",
        "causal_stage": "verification",
        "summary": "One effect is established and one remains unverified.",
        "evidence_refs": [{"kind": "observation", "ref": "observation-1"}],
        "committed_effects": [{"effect_id": "effect-1", "status": "satisfied"}],
        "remaining_obligations": [{"effect_id": "effect-2", "status": "unverified"}],
        "retryability": "now",
        "recovery_affordances": [
            {"action_type": "inspect", "target_ref": "observation-1"},
            {"action_type": "retry", "target_ref": "effect-2"},
            {"action_type": "narrower_answer", "target_ref": "effect-1"},
        ],
        "learning_candidate": None,
        "redaction_status": "contains_no_sensitive_values",
        "provenance": {"decision_source": "represented_llm"},
    }

    receipt, validation = validate_terminal_outcome_receipt(
        authored_receipt,
        completion_gate={"safe_to_claim_completion": True},
        required=True,
    )
    gate = apply_terminal_outcome_receipt_to_completion_gate(
        {"safe_to_claim_completion": True, "requires_follow_up": False},
        receipt=receipt,
        validation=validation,
        authoritative_receipt_required=True,
    )

    assert validation["valid"] is True
    assert gate["decision"] == "verified_partial"
    persisted = gate["evidence_payload"]["terminal_outcome_receipt"]
    assert persisted["committed_effects"] == [
        {"effect_id": "effect-1", "status": "satisfied"}
    ]
    assert [item["action_type"] for item in persisted["recovery_affordances"]] == [
        "inspect",
        "retry",
        "narrower_answer",
    ]


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
        conditional_required_tools=(
            "some_prior_mutation_tool",
            "some_prior_readback_tool",
        ),
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


def test_verified_existing_entity_does_not_force_duplicate_creation() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "conditional_required_tools": ["create_concepts", "fetch_concept"],
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "entity",
                    "concept_ids": ["#V#synthetic_existing_entity"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                }
            ],
        }
    )

    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[{"tool": "resolve_concept_by_name", "status": "ok"}],
        obligation_carry_forward_permitted=None,
    )

    assert activated == ["fetch_concept"]
    assert projection["conditional_tools_satisfied_without_execution"] == [
        "create_concepts"
    ]
    assert projection["conditional_satisfaction_reason"] == (
        "resolved_existing_entity_satisfies_create_if_absent_branch"
    )
