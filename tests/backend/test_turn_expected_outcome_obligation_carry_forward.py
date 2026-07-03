"""Regression tests for prior-turn expected-outcome obligation carry-forward.

JVNAUTOSCI-2563: a follow-up turn that merely shares an entity/source/topic with
a prior turn must reuse the prior referents without inheriting the prior turn's
ingestion/read-back/mutation obligations. Prior obligations carry forward only on
an explicit continuation (represented policy) or when the current turn resolved
the target through its own execution.

These tests pin the merge boundary itself so future changes cannot silently
reintroduce obligation leakage through conversation context. They cover a
domain-varied family: arXiv paper existence, Gmail message lookup, Jira issue
lookup, and generic Vontology concept lookup.
"""

from src.backend.workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
)
from src.backend.services.turn_expected_outcome_obligation_carry_forward import (
    adjudicate_conditional_required_tool_activation,
    obligation_carry_forward_permission,
    target_resolved_by_current_turn,
)
from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
)


def _contract(conditional_tools, *, target_concept_ids=(), target_type_ids=()):
    return TurnExpectedOutcomeContract(
        conditional_required_tools=tuple(conditional_tools),
        target_concept_ids=tuple(target_concept_ids),
        target_type_ids=tuple(target_type_ids),
    )


# --- permission derivation from represented adjudication -------------------


def test_permission_carry_directive_permits() -> None:
    assert obligation_carry_forward_permission(
        {"prior_obligation_carry_forward": "carry"}
    ) is True


def test_permission_suppress_directive_forbids() -> None:
    assert obligation_carry_forward_permission(
        {"prior_obligation_carry_forward": "suppress"}
    ) is False


def test_permission_current_request_only_scope_forbids() -> None:
    assert obligation_carry_forward_permission(
        {"expected_outcome_scope": "current_request_only"}
    ) is False


def test_permission_absent_directive_returns_none() -> None:
    assert obligation_carry_forward_permission({"mode": "referent_resolution_summary"}) is None
    assert obligation_carry_forward_permission(None) is None


def test_target_resolved_by_current_turn_signal() -> None:
    assert target_resolved_by_current_turn([]) is False
    assert target_resolved_by_current_turn(None) is False
    assert target_resolved_by_current_turn([{"tool_name": "gmail_list_messages"}]) is True
    assert target_resolved_by_current_turn([{"tool": "jira_search"}]) is True
    assert target_resolved_by_current_turn([{"unrelated": "x"}]) is False


# --- the motivating leak: arXiv concept-existence follow-up ----------------


def test_arxiv_existence_followup_suppresses_prior_ingestion_obligations() -> None:
    # Prior ingestion/read-back tools carried into a bare "do you have a concept
    # for X" follow-up; prior paper + file-copy concept IDs carried as referents;
    # current turn ran no tools and there is no continuation directive.
    contract = _contract(
        (
            "scholarly_paper.verify_representation",
            "vontology:fetch_concept",
            "vontology:read_file_copy",
            "vontology:rag_get_item",
        ),
        target_concept_ids=(
            "#V#genotex_an_llm_agent_benchmark",
            "#V#arxiv_pdf_file_46934c89",
        ),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == [
        "scholarly_paper.verify_representation",
        "vontology:fetch_concept",
        "vontology:read_file_copy",
        "vontology:rag_get_item",
    ]
    assert projection["suppression_reason"]
    # Referents are preserved for downstream resolution.
    assert projection["carried_forward_referents"]["target_concept_ids"] == [
        "#V#genotex_an_llm_agent_benchmark",
        "#V#arxiv_pdf_file_46934c89",
    ]


def test_gmail_lookup_followup_suppresses_prior_mail_action_obligations() -> None:
    contract = _contract(
        ("gmail_send_message", "gmail_modify_labels"),
        target_concept_ids=("#V#gmail_message_thread_a1b2",),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == [
        "gmail_send_message",
        "gmail_modify_labels",
    ]


def test_jira_lookup_followup_suppresses_prior_write_obligations() -> None:
    contract = _contract(
        ("jira_transition", "jira_add_comment"),
        target_concept_ids=("#V#jira_issue_jvnautosci_150",),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == [
        "jira_transition",
        "jira_add_comment",
    ]


def test_generic_concept_lookup_followup_suppresses_prior_mutation_obligations() -> None:
    contract = _contract(
        ("upsert_singleton_text_relation", "add_relationship"),
        target_concept_ids=("#V#some_represented_concept",),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == [
        "upsert_singleton_text_relation",
        "add_relationship",
    ]


# --- legitimate cases must still activate ----------------------------------


def test_explicit_continuation_directive_carries_obligations() -> None:
    contract = _contract(
        ("scholarly_paper.verify_representation", "vontology:fetch_concept"),
        target_concept_ids=("#V#genotex_paper",),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=True,
    )
    assert activated == [
        "scholarly_paper.verify_representation",
        "vontology:fetch_concept",
    ]
    assert projection["carried_forward_obligations"] == activated
    assert projection["suppressed_prior_obligations"] == []


def test_in_turn_retrieve_then_act_activates_conditional_tool() -> None:
    # The current turn ran its own retrieval tool that resolved the target, so the
    # downstream conditional obligation legitimately activates.
    contract = _contract(("workflow_execute",), target_concept_ids=("#V#paper_x",))
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[{"tool_name": "gmail_list_messages", "status": "success"}],
        obligation_carry_forward_permitted=None,
    )
    assert activated == ["workflow_execute"]
    assert projection["target_resolved_by_current_turn"] is True


def test_unresolved_target_leaves_conditional_tools_pending() -> None:
    # No symbolic target resolved yet: conditional tools stay dormant regardless
    # of carry-forward, and are surfaced as pending rather than suppressed.
    contract = _contract(("workflow_execute",))
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == []
    assert projection["pending_conditional_required_tools"] == ["workflow_execute"]


def test_no_conditional_tools_is_noop() -> None:
    contract = _contract((), target_concept_ids=("#V#paper_x",))
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[],
        obligation_carry_forward_permitted=None,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == []
    assert projection["carried_forward_obligations"] == []


def test_represented_suppress_directive_is_authoritative() -> None:
    # An explicit represented suppress directive keeps prior obligations out even
    # when the current turn ran a tool: represented policy is authoritative and
    # takes precedence over the structural fallback signal.
    contract = _contract(
        ("scholarly_paper.verify_representation",),
        target_concept_ids=("#V#genotex_paper",),
    )
    activated, projection = adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=[{"tool_name": "fetch_concept"}],
        obligation_carry_forward_permitted=False,
    )
    assert activated == []
    assert projection["suppressed_prior_obligations"] == [
        "scholarly_paper.verify_representation"
    ]
    assert (
        projection["suppression_reason"]
        == "represented_context_adjudication_suppressed_prior_obligation_carry_forward"
    )


# --- integration through the real turn-record build path -------------------

_PRIOR_INGESTION_TOOLS = [
    "scholarly_paper.verify_representation",
    "vontology:fetch_concept",
    "vontology:read_file_copy",
    "vontology:rag_get_item",
]


def _build_bare_lookup_followup_record():
    # Reproduces the JVNAUTOSCI-2563 motivating follow-up: a bare concept-existence
    # lookup whose expected-outcome contract carried the prior turn's ingestion/
    # read-back conditional tools plus the prior resolved concept IDs as referents,
    # with zero tools invoked and no continuation directive.
    return build_turn_execution_record(
        request_id="req-2563-bare-lookup",
        session_id="session-2563",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Do you have a concept for https://arxiv.org/abs/2406.15341",
        response_text="I could not verify whether that concept exists yet.",
        interaction_timestamp_utc="2026-07-03T16:41:16Z",
        workflow_routing={
            "workflow_id": "#V#chat_narration_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        turn_expected_outcome_contract={
            "expected_outcome_summary": "Answer whether a concept exists for the source.",
            "conditional_required_tools": list(_PRIOR_INGESTION_TOOLS),
            "target_concept_ids": [
                "#V#genotex_an_llm_agent_benchmark",
                "#V#arxiv_pdf_file_46934c89",
            ],
        },
    )


def test_record_build_suppresses_carried_prior_obligations() -> None:
    record = _build_bare_lookup_followup_record()
    summary = record["execution"]["summary"]

    projection = summary["expected_outcome_obligation_carry_forward"]
    assert projection["suppressed_prior_obligations"] == _PRIOR_INGESTION_TOOLS
    assert projection["suppression_reason"]
    assert summary["suppressed_prior_obligation_tools"] == _PRIOR_INGESTION_TOOLS
    # Prior referents are preserved for downstream resolution.
    assert projection["carried_forward_referents"]["target_concept_ids"] == [
        "#V#genotex_an_llm_agent_benchmark",
        "#V#arxiv_pdf_file_46934c89",
    ]


def test_record_build_keeps_stale_tools_out_of_required_obligations() -> None:
    record = _build_bare_lookup_followup_record()
    summary = record["execution"]["summary"]

    obligations = summary.get("required_tool_obligations") or {}
    required_tools = set(obligations.get("unsatisfied_required_tools") or [])
    # None of the prior ingestion/read-back tools should have become an obligation.
    assert required_tools.isdisjoint(set(_PRIOR_INGESTION_TOOLS))
