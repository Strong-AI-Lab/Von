from __future__ import annotations

import json
import time
from collections.abc import Mapping
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import (
    InternalMCPTransport,
    internal_mcp_cancellation_requested,
)
from src.backend.languagemodels.llm_interface import ModelExecutionEligibilityError
from src.backend.languagemodels.structured_tool_calling.types import (
    LLMContinuation,
    LLMResponse,
    StructuredToolContextLimitError,
    StructuredToolProtocolError,
    StructuredToolTransportError,
    ToolCall,
    ToolCallError,
    ToolResult,
)
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from src.backend.services.adaptive_turn_service import (
    _CONVERSATION_OBSERVATIONS_MAX_BYTES,
    _CONVERSATION_OBSERVATIONS_MAX_ITEMS,
    _CONVERSATION_SITUATION_MAX_CHARS,
    _bound_tool_results_for_model,
    _bounded_conversation_observation_projection,
    _build_effect_outcome_report,
    _canonical_create_readback_matches_invocation,
    _canonical_effect_readback_receipt,
    _canonical_relation_readback_matches_invocation,
    _canonical_scoped_assertion_readback_matches_invocation,
    _canonically_verified_material_effect_ids,
    _capability_catalogue,
    _cited_ontology_mutation_claim_conflicts,
    _compact_context_after_limit,
    _compact_evidence_envelope,
    _compact_evidence_index,
    _effect_id,
    _effect_postcondition_identity,
    _effect_requires_turn_finality,
    _effect_result_target_ids,
    _effect_subject_authorised,
    _effect_subject_authority_denial,
    _extract_conversation_situation_sidecar,
    _final_synthesis_context,
    _group_effect_outcome_facts,
    _json_bytes,
    _ordinary_effect_argument_denial,
    _reconcile_effect_attempts_by_postcondition,
    _scope_message,
    _trusted_tool_payload,
    build_effect_outcome_narration_context,
    build_effect_outcome_spoken_fallback,
    build_effect_outcome_spoken_text,
    execute_adaptive_turn,
    ordinary_turn_capability_delegation,
)
from src.backend.services.turn_evidence_store import TrustedTurnScope


@pytest.fixture(autouse=True)
def _acknowledge_effect_observation_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import turn_execution_record_service

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        lambda **kwargs: {
            "updated": True,
            "duplicate": False,
            "phase": kwargs.get("phase"),
            "stored_phase": dict(kwargs.get("observation") or {}),
        },
    )


class _SequenceClient:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        next_response = self.responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return next_response


def test_effect_outcome_narration_context_is_safe_and_finality_preserving() -> None:
    context = build_effect_outcome_narration_context(
        terminal_status="effect_partially_completed",
        facts=(
            {
                "tool": "Entity Representation Workflow",
                "effect_status": "succeeded",
                "reconciliation_status": "canonically_verified",
                "target_ids": ("#V#university_of_waikato",),
            },
            {
                "tool": "Create Concepts",
                "effect_status": "indeterminate",
                "outcome_resolved": True,
                "current_outcome_status": "target_observed",
                "target_ids": (
                    "#V#university_of_waikato",
                    "not-a-safe-target-label",
                ),
                "error": "raw failure details must not be narrated",
                "effect_id": "11111111-2222-3333-4444-555555555555",
            },
        ),
        canonical_scopes=({"mode": "user", "concept_id": "#V#person"},),
    )

    assert context["turn_outcome"] == "partially completed"
    assert context["scope"] == (
        "at least one checked result is in the current user's personal scope; "
        "organisation publication was not established"
    )
    assert context["operation_outcomes"] == [
        {
            "operation": "Create Concepts",
            "original_status": "indeterminate",
            "targets": ["University of Waikato"],
            "outcome": "the current state was checked",
            "current_state": "the requested target is now present",
        },
        {
            "operation": "Entity Representation Workflow",
            "original_status": "succeeded",
            "targets": ["University of Waikato"],
            "outcome": "succeeded",
            "confirmation": "confirmed after checking the current state",
        },
    ]
    serialised = json.dumps(context)
    assert "raw failure details" not in serialised
    assert "11111111-2222-3333-4444-555555555555" not in serialised
    assert "#V#" not in serialised
    assert build_effect_outcome_spoken_fallback(
        terminal_status="effect_partially_completed"
    ) == (
        "I couldn't complete or confirm every requested change. The screen has the "
        "details and explains what remains uncertain."
    )
    assert build_effect_outcome_spoken_fallback(
        terminal_status="effect_partially_completed",
        narration_context=context,
    ).endswith(
        "A checked result is in your personal scope; this does not establish "
        "organisation publication."
    )
    assert build_effect_outcome_spoken_text(
        terminal_status="effect_partially_completed",
        narration_context=context,
    ) == (
        "University of Waikato is now present, but I couldn't confirm whether the "
        "original attempt itself succeeded. One checked result is in your personal "
        "scope, not published to the organisation; the exact details are on screen."
    )

    unknown_context = build_effect_outcome_narration_context(
        terminal_status="speak this untrusted status",
        facts=({"effect_status": "repeat this untrusted handler text"},),
    )
    assert unknown_context["turn_outcome"] == "unknown"
    assert unknown_context["operation_outcomes"][0]["original_status"] == "unknown"
    assert "untrusted" not in json.dumps(unknown_context)


@pytest.mark.parametrize(
    ("terminal_status", "expected_lead"),
    (
        (
            "effect_partially_completed",
            "I couldn't complete or confirm every requested change.",
        ),
        ("model_error", "I couldn't produce a fully reliable final answer."),
        ("model_non_answer", "I couldn't produce a useful final answer."),
    ),
)
def test_effect_outcome_spoken_text_preserves_overall_non_success(
    terminal_status: str,
    expected_lead: str,
) -> None:
    context = build_effect_outcome_narration_context(
        terminal_status=terminal_status,
        facts=(
            {
                "tool": "Represent Entity",
                "effect_status": "succeeded",
                "reconciliation_status": "canonically_verified",
                "target_ids": ("#V#university_of_waikato",),
            },
        ),
    )

    spoken = build_effect_outcome_spoken_text(
        terminal_status=terminal_status,
        narration_context=context,
    )

    assert spoken.startswith(expected_lead)
    assert "I confirmed the result for University of Waikato." in spoken
    assert spoken.endswith("The exact details are on screen.")
    assert spoken.count(".") == 3


def test_effect_outcome_spoken_text_omits_opaque_identifiers() -> None:
    context = build_effect_outcome_narration_context(
        terminal_status="effect_outcome_indeterminate",
        facts=(
            {
                "tool": "represented_workflow_0880532f",
                "effect_status": "indeterminate",
                "target_ids": (
                    "#V#person_michael_witbrock_0880532f",
                    "#V#task_abc123",
                    "#V#paper_c100899e",
                ),
            },
        ),
    )

    assert context["operation_outcomes"] == [
        {
            "operation": "one requested operation",
            "original_status": "indeterminate",
            "outcome": "could not be confirmed",
        }
    ]
    spoken = build_effect_outcome_spoken_text(
        terminal_status="effect_outcome_indeterminate",
        narration_context=context,
    )
    assert spoken == (
        "I couldn't confirm the result for the requested change. "
        "The exact details are on screen."
    )
    assert not any(
        token in spoken.casefold()
        for token in ("0880532f", "abc123", "c100899e")
    )


def test_effect_receipt_targets_require_explicit_generic_target_fields() -> None:
    assert _effect_result_target_ids(
        {
            "created_concept_ids": ["#V#created_a", "#V#created_b"],
            "result": {
                "concept_id": "#V#nested_result",
                "type_concept_id": "#V#context_type",
                "candidate_concept_ids": ["#V#candidate"],
                "missing_concept_ids": ["#V#missing"],
                "arguments": {
                    "invented_concept_id": "#V#echoed_argument_claim",
                },
            },
        }
    ) == [
        "#V#created_a",
        "#V#created_b",
        "#V#nested_result",
    ]


def test_embedded_gmail_readback_is_canonical_without_private_fields() -> None:
    receipt = _canonical_effect_readback_receipt(
        {
            "canonical_readback": {
                "status": "verified",
                "verified": True,
                "message_id": "gmail-message-1",
                "thread_id": "gmail-thread-1",
                "sender": "Von AI Agent <agent@example.test>",
                "to": ["recipient@example.test"],
                "subject": "Private subject",
                "body_included": False,
                "disclosure_verified": True,
                "delivery_fingerprint_verified": True,
            }
        }
    )

    assert receipt == {
        "status": "verified",
        "verified": True,
        "body_included": False,
        "message_id": "gmail-message-1",
        "thread_id": "gmail-thread-1",
        "disclosure_verified": True,
        "delivery_fingerprint_verified": True,
    }
    assert _effect_result_target_ids(
        {
            "message_id": "gmail-message-1",
            "thread_id": "gmail-thread-1",
            "delivery_fingerprint": "delivery-1",
        }
    ) == ["gmail-message-1", "gmail-thread-1", "delivery-1"]

    material, verified = _canonically_verified_material_effect_ids(
        [
            {
                "effect_id": "effect-1",
                "status": "ok",
                "result_target_ids": ["gmail-message-1"],
            }
        ],
        {
            "effect-1": {
                "effect_status": "succeeded",
                "changed": True,
                "turn_finality_required": True,
                "canonical_readback": receipt,
            }
        },
    )
    assert material == {"effect-1"}
    assert verified == {"effect-1"}


def test_embedded_ontology_relation_readback_verifies_the_exact_effect() -> None:
    readback = _canonical_effect_readback_receipt(
        {
            "canonical_read_back": {
                "source_id": "#V#student_a",
                "source_exists": True,
                "predicate": "is_an_instance_of",
                "target": "#V#student",
                "relationship_present": True,
                "inverse_predicate": "has_instance",
                "inverse_relationship_present": True,
                "publication_context": {"kind": "global", "concept_id": None},
            }
        }
    )

    invocation = {
        "effect_id": "effect-ontology-1",
        "status": "ok",
        "execution_method": "add_relationship",
        "effective_arguments": {
            "source_id": "#V#student_a",
            "predicate": "#V#is_an_instance_of",
            "target": "#V#student",
        },
    }
    material, verified = _canonically_verified_material_effect_ids(
        [invocation],
        {
            "effect-ontology-1": {
                "effect_status": "succeeded",
                "changed": True,
                "turn_finality_required": True,
                "canonical_readback": readback,
            }
        },
    )

    assert readback is not None
    assert readback["relationship_present"] is True
    assert readback["inverse_relationship_present"] is True
    assert material == {"effect-ontology-1"}
    assert verified == {"effect-ontology-1"}
    missing_source_readback = dict(readback)
    missing_source_readback.pop("source_exists")
    assert not _canonical_relation_readback_matches_invocation(
        invocation,
        missing_source_readback,
    )
    wrong_case_readback = dict(readback)
    wrong_case_readback["source_id"] = "#V#Student_A"
    assert not _canonical_relation_readback_matches_invocation(
        invocation,
        wrong_case_readback,
    )


def test_source_owned_relationship_readback_does_not_invent_inverse_requirement() -> None:
    invocation = {
        "effect_id": "effect-source-owned",
        "execution_method": "add_relationship",
        "effective_arguments": {
            "source_id": "#V#student_a",
            "predicate": "is_an_instance_of",
            "target": "#V#alumni",
        },
    }
    readback = _canonical_effect_readback_receipt(
        {
            "canonical_read_back": {
                "source_id": "#V#student_a",
                "source_exists": True,
                "predicate": "is_an_instance_of",
                "target": "#V#alumni",
                "relationship_present": True,
                "inverse_predicate": "has_instance",
                "inverse_relationship_present": None,
                "inverse_relationship_required": False,
            }
        }
    )

    assert _canonical_relation_readback_matches_invocation(invocation, readback)
    absent_forward = dict(readback or {})
    absent_forward["relationship_present"] = False
    assert not _canonical_relation_readback_matches_invocation(
        invocation,
        absent_forward,
    )
    wrong_target = dict(readback or {})
    wrong_target["target"] = "#V#different_collection"
    assert not _canonical_relation_readback_matches_invocation(
        invocation,
        wrong_target,
    )


def test_governed_create_projection_verifies_only_one_exact_existing_concept() -> None:
    invocation = {
        "effect_id": "effect-create",
        "execution_method": "create_concepts",
        "effective_arguments": {
            "parent_id": "#V#first_order_collection",
            "concepts": [
                {
                    "concept_id": "#V#sail_phd_alumni",
                    "name": "SAIL PhD Alumni",
                    "kind": "type",
                }
            ],
            "scope_mode": "organisation_general",
        },
    }
    readback = _canonical_effect_readback_receipt(
        {
            "canonical_read_back": {
                "concepts": [
                    {
                        "concept_id": "#V#sail_phd_alumni",
                        "exists": True,
                        "publication_context": {
                            "kind": "organisation",
                            "concept_id": "#V#strong_ai_lab",
                        },
                        "text_relations": [
                            {"text": "must not be copied into the turn receipt"}
                        ],
                    }
                ]
            }
        }
    )

    assert readback == {
        "concept_count": 1,
        "concepts": [
            {"concept_id": "#V#sail_phd_alumni", "exists": True}
        ],
    }
    assert _canonical_create_readback_matches_invocation(invocation, readback)
    wrong_id = {
        **(readback or {}),
        "concepts": [{"concept_id": "#V#other", "exists": True}],
    }
    assert not _canonical_create_readback_matches_invocation(invocation, wrong_id)
    missing = {
        **(readback or {}),
        "concepts": [{"concept_id": "#V#sail_phd_alumni", "exists": False}],
    }
    assert not _canonical_create_readback_matches_invocation(invocation, missing)
    multiple = {
        "concept_count": 2,
        "concepts": [
            {"concept_id": "#V#sail_phd_alumni", "exists": True},
            {"concept_id": "#V#other", "exists": True},
        ],
    }
    assert not _canonical_create_readback_matches_invocation(invocation, multiple)


def test_exact_later_create_reconciles_bad_parent_attempt_without_erasing_it() -> None:
    failed_invocation = {
        "effect_id": "effect-bad-parent",
        "execution_method": "create_concepts",
        "effective_arguments": {
            "parent_id": "#V#thing",
            "concepts": [
                {"concept_id": "#V#sail_phd_alumni", "name": "SAIL PhD Alumni"}
            ],
        },
    }
    successful_invocation = {
        "effect_id": "effect-good-parent",
        "execution_method": "create_concepts",
        "effective_arguments": {
            "parent_id": "#V#first_order_collection",
            "concepts": [
                {"concept_id": "#V#sail_phd_alumni", "name": "SAIL PhD Alumni"}
            ],
        },
    }
    snapshot = {
        "effect-bad-parent": {
            "effect_status": "failed",
            "changed": False,
            "turn_finality_required": True,
        },
        "effect-good-parent": {
            "effect_status": "succeeded",
            "changed": True,
            "turn_finality_required": True,
            "canonical_readback": {
                "concept_count": 1,
                "concepts": [
                    {"concept_id": "#V#sail_phd_alumni", "exists": True}
                ],
            },
        },
    }

    reconciled = _reconcile_effect_attempts_by_postcondition(
        tool_invocations=[failed_invocation, successful_invocation],
        effect_snapshot=snapshot,
    )

    assert reconciled["effect-bad-parent"]["recovered_by_effect_id"] == (
        "effect-good-parent"
    )
    assert reconciled["effect-bad-parent"]["recovery_status"] == "succeeded"
    assert reconciled["effect-bad-parent"]["reconciliation_basis"] == (
        "later_exact_postcondition_success"
    )
    assert snapshot["effect-bad-parent"].get("recovered_by_effect_id") is None
    assert _effect_postcondition_identity(failed_invocation) == (
        _effect_postcondition_identity(successful_invocation)
    )


def test_prior_exact_create_success_satisfies_redundant_later_failure() -> None:
    effective_arguments = {
        "concepts": [{"concept_id": "#V#sail_phd_alumni"}],
    }
    invocations = [
        {
            "effect_id": "effect-created",
            "execution_method": "create_concepts",
            "effective_arguments": effective_arguments,
        },
        {
            "effect_id": "effect-redundant-conflict",
            "execution_method": "create_concepts",
            "effective_arguments": effective_arguments,
        },
    ]
    snapshot = {
        "effect-created": {
            "effect_status": "succeeded",
            "changed": True,
            "canonical_readback": {
                "concept_count": 1,
                "concepts": [
                    {"concept_id": "#V#sail_phd_alumni", "exists": True}
                ],
            },
        },
        "effect-redundant-conflict": {
            "effect_status": "not_started",
            "changed": False,
            "error_code": "ontology_create_concept_id_conflict",
        },
    }

    reconciled = _reconcile_effect_attempts_by_postcondition(
        tool_invocations=invocations,
        effect_snapshot=snapshot,
    )

    redundant = reconciled["effect-redundant-conflict"]
    assert redundant["recovered_by_effect_id"] == "effect-created"
    assert redundant["recovery_status"] == "succeeded"
    assert redundant["reconciliation_basis"] == (
        "prior_exact_postcondition_success"
    )


def test_outcome_facts_group_retries_by_exact_relationship_postcondition() -> None:
    relationship_identity = {
        "kind": "canonical_relationship",
        "source_id": "#V#aaron_keesing",
        "predicate": "is_an_instance_of",
        "target": "#V#sail_phd_alumni",
        "relationship_present": True,
    }
    facts = [
        {
            "effect_id": f"effect-aaron-{index}",
            "tool": "add_relationship",
            "effect_status": "not_started",
            "changed": False,
            "error_code": "explicit_scope_adoption_required",
            "postcondition_identity": relationship_identity,
        }
        for index in range(4)
    ]
    facts.append(
        {
            "effect_id": "effect-beryl",
            "tool": "add_relationship",
            "effect_status": "not_started",
            "changed": False,
            "error_code": "organisation_ontology_admin_authority_required",
            "postcondition_identity": {
                **relationship_identity,
                "source_id": "#V#beryl_qi",
            },
        }
    )

    grouped = _group_effect_outcome_facts(facts)

    assert len(grouped) == 2
    assert grouped[0]["attempt_count"] == 4
    assert grouped[0]["attempt_status_counts"] == {"not_started": 4}
    assert grouped[0]["attempt_effect_ids"] == [
        "effect-aaron-0",
        "effect-aaron-1",
        "effect-aaron-2",
        "effect-aaron-3",
    ]
    assert grouped[1]["attempt_count"] == 1


def test_embedded_scoped_assertion_readback_verifies_the_exact_effect() -> None:
    canonical_record = {
        "assertion_id": "ska_research_description",
        "subject_concept_id": "#V#student_a",
        "predicate": "#V#has_research_description",
        "object_kind": "text",
        "object_text": {
            "text": "Studies robust multimodal learning.",
            "language": "en-NZ",
        },
        "scope": {
            "mode": "organisation",
            "user_concept_id": "#V#person",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#person@org",
            "audience_keys": ["org:#V#org"],
        },
        "provenance": {
            "asserted_by_user_concept_id": "#V#person",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#person@org",
        },
        "canonical_publication": False,
        "status": "asserted",
    }
    readback = _canonical_effect_readback_receipt(
        {"canonical_read_back": canonical_record}
    )
    invocation = {
        "effect_id": "effect-scoped-assertion-1",
        "status": "ok",
        "execution_method": "upsert_scoped_assertion",
        "effective_arguments": {
            "subject_concept_id": "#V#student_a",
            "predicate": "#V#has_research_description",
            "target_text": "Studies robust multimodal learning.",
            "language": "en-NZ",
            "scope_mode": "organisation",
            "acting_user_concept_id": "#V#person",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#person@org",
            "canonical_publication": False,
        },
    }
    material, verified = _canonically_verified_material_effect_ids(
        [invocation],
        {
            "effect-scoped-assertion-1": {
                "effect_status": "succeeded",
                "changed": True,
                "turn_finality_required": True,
                "canonical_readback": readback,
            }
        },
    )

    assert readback is not None
    assert "object_text" not in readback
    assert _canonical_scoped_assertion_readback_matches_invocation(
        invocation,
        readback,
    )
    assert material == {"effect-scoped-assertion-1"}
    assert verified == {"effect-scoped-assertion-1"}
    wrong_scope_readback = dict(readback)
    wrong_scope_readback["scope_mode"] = "user"
    assert not _canonical_scoped_assertion_readback_matches_invocation(
        invocation,
        wrong_scope_readback,
    )
    wrong_object_readback = dict(readback)
    wrong_object_readback["object_text_identity_sha256"] = "wrong"
    assert not _canonical_scoped_assertion_readback_matches_invocation(
        invocation,
        wrong_object_readback,
    )
    wrong_actor_readback = dict(readback)
    wrong_actor_readback["trusted_scope_identity_sha256"] = "wrong"
    assert not _canonical_scoped_assertion_readback_matches_invocation(
        invocation,
        wrong_actor_readback,
    )
    mismatched_namespace_record = json.loads(json.dumps(canonical_record))
    mismatched_namespace_record["scope"]["namespace"] = "#V#other@org"
    mismatched_namespace_readback = _canonical_effect_readback_receipt(
        {"canonical_read_back": mismatched_namespace_record}
    )
    assert mismatched_namespace_readback is not None
    assert "trusted_scope_identity_sha256" not in mismatched_namespace_readback
    wrong_audience_record = json.loads(json.dumps(canonical_record))
    wrong_audience_record["scope"]["audience_keys"] = ["org:#V#other"]
    wrong_audience_readback = _canonical_effect_readback_receipt(
        {"canonical_read_back": wrong_audience_record}
    )
    assert wrong_audience_readback is not None
    assert "trusted_scope_identity_sha256" not in wrong_audience_readback
    assert not _canonical_scoped_assertion_readback_matches_invocation(
        invocation,
        wrong_audience_readback,
    )
    extra_audience_record = json.loads(json.dumps(canonical_record))
    extra_audience_record["scope"]["audience_keys"] = [
        "org:#V#org",
        "user:#V#person",
    ]
    extra_audience_readback = _canonical_effect_readback_receipt(
        {"canonical_read_back": extra_audience_record}
    )
    assert extra_audience_readback is not None
    assert "trusted_scope_identity_sha256" not in extra_audience_readback
    assert not _canonical_scoped_assertion_readback_matches_invocation(
        invocation,
        extra_audience_readback,
    )


def test_cited_ontology_conflict_matches_live_evidence_identifier() -> None:
    conflicts = _cited_ontology_mutation_claim_conflicts(
        "| Gael Gendron | Added and verified | ev_live_failed_effect |",
        tool_invocations=[
            {
                "effect_id": "effect-live-failed",
                "execution_method": "add_relationship",
                "effective_arguments": {
                    "source_id": "#V#gael_gendron",
                    "predicate": "#V#is_an_instance_of",
                    "target": "#V#student",
                },
                "evidence": {"evidence_id": "ev_live_failed_effect"},
                "error_code": "ontology_mutation_target_not_accessible",
            }
        ],
        effect_snapshot={
            "effect-live-failed": {
                "effect_status": "not_started",
                "changed": False,
            }
        },
        canonically_verified_effect_ids=set(),
    )

    assert len(conflicts) == 1
    assert conflicts[0]["effect_id"] == "effect-live-failed"
    assert conflicts[0]["evidence_id"] == "ev_live_failed_effect"
    assert conflicts[0]["reason"] == "effect_not_succeeded"


def _gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="general_read",
            handler=handler,
            input_schema=Schema(
                optional={
                    "query": str,
                    "namespace": (str, type(None)),
                    "user_id": (str, type(None)),
                    "org_id": (str, type(None)),
                },
                allow_unknown=False,
                description="Read arbitrary general evidence.",
            ),
            category="read",
            ordinary_turn_public=True,
            description="Read arbitrary general evidence.",
        )
    )
    catalogue.register(
        MethodDefinition(
            name="general_write",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(allow_unknown=True),
            category="write",
            description="A write which must not be delegated.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def test_scope_message_contains_boundaries_and_preserves_request_scope() -> None:
    message = _scope_message(
        TrustedTurnScope(
            user_concept_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        ),
        delegated_count=12,
        final_synthesis=False,
    )

    assert "Authenticated actor: #V#person" in message
    assert "Effect boundary:" in message
    assert "least costly" not in message
    assert "representedness" not in message
    assert "requested outcome and effect cardinality" in message
    assert "smallest bounded candidate set" in message
    assert "must not create, update, or otherwise act on more" in message
    assert "Before another search, read, or hydration" in message
    assert "what unresolved material decision" in message
    assert "Stop retrieving once current evidence supports" in message
    assert "do not broaden retrieval merely to avoid asking" in message
    assert "hydrate candidates sequentially" in message
    assert "Parallel fan-out is appropriate only" in message
    assert "Preserve result-set continuity" in message
    assert "uninspected candidate handles" in message
    assert "A non-match among earlier items" in message
    assert "Replace the result only when you can identify" in message
    assert "reuse existing representation as create-if-absent" in message
    assert "inspect and reuse any exact existing candidate" in message
    assert "stable source or component identifiers" in message
    assert "partial neighbourhood cannot establish absence" in message
    assert "Create only after that bounded reuse check" in message
    assert "open-ended request such as 'I want to talk about X'" in message
    assert "get_concept_elicitation_opportunities" in message
    assert "giving the user's own questions priority" in message
    assert "preserve the exact text with provenance" in message
    assert "OUTCOME EXPLANATION SUPPORT" in message
    assert "which mechanism was actually invoked" in message
    assert "Discovery, selection, a workflow declaration" in message
    assert "Never say a workflow stopped, failed, or reached a stage" in message
    assert "not invoked; invoked and pending; invoked and failed" in message
    assert "every attempted tool call succeeded" in message
    assert "outcome_explanation_support" in message
    assert "its prompt body is not evidence" in message


def test_progressive_evidence_guidance_survives_post_read_continuation() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    call_id="call-read-once",
                    tool_name="turn_invoke_capability",
                    payload={"name": "general_read", "arguments": {}},
                )
            ],
        ),
        LLMResponse(text_response="The first bounded read was sufficient."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Find the relevant item.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-progressive-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The first bounded read was sufficient."
    assert len(client.calls) == 2
    second_system_message = client.calls[1]["system_message"]
    assert "what unresolved material decision" in second_system_message
    assert "Stop retrieving once current evidence supports" in second_system_message
    assert "hydrate candidates sequentially" in second_system_message
    assert "Preserve result-set continuity" in second_system_message
    assert "reuse existing representation as create-if-absent" in second_system_message
    assert "inspect and reuse any exact existing candidate" in second_system_message


def test_scope_message_carries_brief_approval_and_exact_recovery_state() -> None:
    situation = (
        "Proposal: represent the booked journey using source Gmail message "
        "19febb3feda7b024. Durable workflow instance: workflow-trip-123. "
        "Created concept: #V#trip_gmail_19febb3feda7b024. "
        "Unmet outcome: add the three component relations."
    )

    message = _scope_message(
        TrustedTurnScope(
            user_concept_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        ),
        delegated_count=12,
        final_synthesis=False,
        conversation_id="conversation-trip",
        conversation_situation=situation,
    )

    assert situation in message
    assert "Interpret a brief follow-up" in message
    assert "most recent sufficiently concrete proposal" in message
    assert "Do not make the user repeat internal identifiers" in message
    assert "carry it out rather than merely restating it" in message
    assert "complete purpose index" in message
    assert "typed recovery affordance" in message
    assert "same requested semantic object and effect cardinality" in message
    assert "normally use it in the same turn" in message
    assert "complete only unmet postconditions" in message
    assert "never repeat a confirmed effect" in message
    assert "core concept establishes only" in message
    assert "source-processing marker establishes only processing" in message
    assert "ancillary fields rejected by a core-create contract" in message
    assert "same-object scoped or standalone assertions" in message
    assert "do not call the richer representation complete" in message
    assert "entity_representation_coverage=core_only" in message
    assert "not terminal success" in message
    assert "preserve its exact grounded candidate identifiers" in message
    assert "actual workflow invocation status" in message
    assert "verified completed sub-effects" in message
    assert "remaining postconditions" in message
    assert "typed recovery affordance" in message
    assert "unresolved create-versus-reuse status" in message
    assert "Do not leave the only stable identity solely" in message
    assert "including when you made a proposal intended for later approval" in message


def _gmail_gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="gmail_list_messages",
            handler=handler,
            input_schema=Schema(
                required={"profile": str},
                optional={"query": str},
                aliases={
                    "profile_id": "profile",
                    "identity": "profile",
                    "user_id": "profile",
                },
                allow_unknown=False,
                description="List Gmail messages for a represented profile.",
            ),
            category="read",
            ordinary_turn_trusted_argument_bindings={
                "profile": "gmail_profile",
            },
            description="List Gmail messages for a represented profile.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _gmail_choice_gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="gmail_list_messages",
            handler=handler,
            input_schema=Schema(
                required={"profile": str},
                optional={"query": str, "scope": str},
                aliases={
                    "profile_id": "profile",
                    "identity": "profile",
                },
                allow_unknown=False,
                description="List Gmail messages for an authorised profile.",
            ),
            category="read",
            ordinary_turn_trusted_argument_choice_bindings={
                "profile": "gmail_profile",
            },
            description="List Gmail messages for an authorised profile.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _gmail_send_gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="gmail_send_message",
            handler=handler,
            input_schema=Schema(
                required={
                    "profile": str,
                    "to": (str, list),
                    "subject": str,
                    "body_text": str,
                    "allow_send": bool,
                },
                optional={
                    "namespace": (str, type(None)),
                    "acting_user_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "request_id": (str, type(None)),
                    "idempotency_scope": (str, type(None)),
                },
                aliases={
                    "profile_id": "profile",
                    "identity": "profile",
                    "allow_mutation": "allow_send",
                },
                allow_unknown=False,
                description="Send Gmail after verified explicit request evidence.",
            ),
            category="write",
            ordinary_turn_effect=True,
            ordinary_turn_trusted_argument_bindings={
                "acting_user_concept_id": "actor_user_concept_id",
                "organisation_concept_id": "actor_organisation_concept_id",
                "request_id": "turn_id",
                "idempotency_scope": "turn_id",
                "namespace": "turn_namespace",
            },
            ordinary_turn_trusted_argument_choice_bindings={
                "profile": "gmail_profile",
            },
            ordinary_turn_fixed_arguments={"allow_send": True},
            write_guardrail={"ordinary_turn_explicit_request": True},
            description="Send Gmail after verified explicit request evidence.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(write_timeout_sec=1.0),
        enabled=True,
    )


def _gmail_profile_choices(*, default: str | None) -> dict[str, Any]:
    return {
        "schema_version": "trusted_argument_choice.v1",
        "default_selector": default,
        "choices": [
            {
                "selector": "#V#gmail_profile_personal",
                "value": "personal-runtime",
                "source_family": "gmail",
                "resource_id": "#V#gmail_profile_personal",
                "runtime_alias": "personal-runtime",
                "display_label": "michael@example.test",
                "represented_identity_concept_ids": ["#V#michael"],
            },
            {
                "selector": "#V#gmail_profile_zhan",
                "value": "zhan-runtime",
                "source_family": "gmail",
                "resource_id": "#V#gmail_profile_zhan",
                "runtime_alias": "zhan-runtime",
                "display_label": "zhan@example.test",
                "represented_identity_concept_ids": ["#V#zhan"],
            },
        ],
    }


def _actor_alias_gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="shared_conversation_list_invites",
            handler=handler,
            input_schema=Schema(
                optional={
                    "invitee_user_id": (str, type(None)),
                    "user_concept_id": (str, type(None)),
                    "acting_user_concept_id": (str, type(None)),
                    "actor_user_id": (str, type(None)),
                    "on_behalf_of_user_concept_id": (str, type(None)),
                    "actor_concept_id": (str, type(None)),
                    "agent_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "org_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="List invites visible to the current actor.",
            ),
            category="read",
            description="List invites visible to the current actor.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _delegation_gateway() -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    policies = {
        "concept_exists": {},
        "gmail_list_messages": {
            "ordinary_turn_trusted_argument_bindings": {
                "profile": "gmail_profile",
            },
        },
        "gmail_list_profiles": {
            "ordinary_turn_excluded_reason": "deployment_account_enumeration",
        },
        "list_recent_screenshots": {
            "ordinary_turn_excluded_reason": "host_local_data",
        },
        "search_arxiv": {"ordinary_turn_public": True},
    }
    for name, policy in policies.items():
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(allow_unknown=True),
                category="read",
                description=f"Read through {name}.",
                **policy,
            )
        )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _effect_gateway(
    handler: Any,
    *,
    write_timeout_sec: float = 1.0,
    effect_output_schema: Schema | None = None,
    effect_admission_window_sec: float | None = None,
    include_scoped_assertion: bool = False,
    hard_timeout_enabled: bool = False,
    selectable_create_scope: bool = False,
) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="general_read",
            handler=lambda **kwargs: handler("general_read", kwargs),
            input_schema=Schema(allow_unknown=True),
            category="read",
        )
    )
    for name, subject_argument in (
        ("create_concepts", None),
        ("upsert_text_relation", "concept_id"),
        ("add_relationship", "source_id"),
        *((("upsert_scoped_assertion", None),) if include_scoped_assertion else ()),
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda _name=name, **kwargs: handler(_name, kwargs),
                input_schema=(
                    Schema(
                        optional={
                            "namespace": (str, type(None)),
                            "created_by_concept_id": (str, type(None)),
                            "organisation_concept_id": (str, type(None)),
                            "org_id": (str, type(None)),
                            "scope_mode": (str, type(None)),
                            "visibility_scope_mode": (str, type(None)),
                        },
                        enum_values={
                            "scope_mode": (
                                "user_only_default",
                                "organisation_general",
                                "global_general",
                                None,
                            )
                        },
                        allow_unknown=True,
                    )
                    if name == "create_concepts" and selectable_create_scope
                    else Schema(allow_unknown=True)
                ),
                output_schema=effect_output_schema,
                category="write",
                ordinary_turn_effect=True,
                hard_timeout_enabled=hard_timeout_enabled,
                effect_admission_window_sec=effect_admission_window_sec,
                ordinary_turn_mutation_subject_argument=subject_argument,
                ordinary_turn_trusted_argument_bindings=(
                    {
                        "namespace": "turn_namespace",
                        "created_by_concept_id": "actor_user_concept_id",
                    }
                    if name == "create_concepts"
                    else (
                        {"namespace": "turn_namespace"}
                        if name == "upsert_text_relation"
                        else (
                            {
                                "acting_user_concept_id": ("actor_user_concept_id"),
                                "organisation_concept_id": (
                                    "actor_organisation_concept_id"
                                ),
                                "namespace": "turn_namespace",
                            }
                            if name == "upsert_scoped_assertion"
                            else None
                        )
                    )
                ),
                ordinary_turn_fixed_arguments=(
                    {
                        "organisation_concept_id": None,
                        "org_id": None,
                        **(
                            {}
                            if selectable_create_scope
                            else {"scope_mode": "user_only_default"}
                        ),
                        "visibility_scope_mode": None,
                    }
                    if name == "create_concepts"
                    else (
                        {"provenance": None}
                        if name == "upsert_text_relation"
                        else (
                            {"canonical_publication": False}
                            if name == "upsert_scoped_assertion"
                            else None
                        )
                    )
                ),
            )
        )
    catalogue.register(
        MethodDefinition(
            name="other_write",
            handler=lambda **kwargs: handler("other_write", kwargs),
            input_schema=Schema(allow_unknown=True),
            category="write",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(
            read_timeout_sec=1.0,
            write_timeout_sec=write_timeout_sec,
        ),
        enabled=True,
    )


def _stub_same_turn_ontology_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Authorise governed fake effects whose tests target turn mechanics."""

    issued: list[dict[str, Any]] = []

    def issue(**kwargs: Any) -> dict[str, Any]:
        issued.append(dict(kwargs))
        return {"delegation_id": f"test-delegation-{kwargs['effect_id']}"}

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        issue,
    )
    return issued


def _stub_proven_scoped_assertion_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the exact internal marker after command-layer preconditions."""

    def issue(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["method_name"] == "add_relationship"
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "global_ontology_admin_authority_required",
            "error": "Global ontology publication authority is required.",
            "recovery_affordances": [
                {"action_type": "create_scoped_assertion"}
            ],
        }

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        issue,
    )


def _workflow_gateway(
    handler: Any,
    *,
    hard_timeout_enabled: bool = True,
    instance_handler: Any | None = None,
) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="general_read",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(allow_unknown=True),
            category="read",
            ordinary_turn_public=True,
        )
    )
    if instance_handler is not None:
        catalogue.register(
            MethodDefinition(
                name="workflow_get_instance",
                handler=instance_handler,
                input_schema=Schema(
                    required={"instance_id": str},
                    optional={
                        "await_terminal": bool,
                        "timeout_seconds": (int, float),
                        "poll_interval_seconds": (int, float),
                    },
                    allow_unknown=False,
                ),
                output_schema=Schema(
                    required={"success": bool},
                    allow_unknown=True,
                ),
                category="read",
            )
        )
    catalogue.register(
        MethodDefinition(
            name="workflow_execute",
            handler=handler,
            input_schema=Schema(
                required={"workflow_id": str},
                optional={
                    "user_id": str,
                    "org_id": (str, type(None)),
                    "namespace": str,
                    "inputs": dict,
                    "max_retries": int,
                    "await_terminal": bool,
                    "timeout_seconds": (int, float),
                    "poll_interval_seconds": (int, float),
                    "include_step_result_envelopes": bool,
                    "include_trace": bool,
                    "source_event_type": str,
                    "source_event_id": str,
                    "event_idempotency_key": str,
                },
                allow_unknown=False,
            ),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="write",
            timeout_sec=1.0,
            hard_timeout_enabled=hard_timeout_enabled,
            effect_admission_window_sec=0.01,
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(
            read_timeout_sec=1.0,
            write_timeout_sec=1.0,
        ),
        enabled=True,
    )


class _ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _DeadlineClient(_SequenceClient):
    def __init__(self, clock: _ManualClock, *responses: Any) -> None:
        super().__init__(*responses)
        self.clock = clock

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        response_number = len(self.calls)
        if response_number == 0:
            self.clock.now = 8.0
        return super().generate_with_tools(prompt, available_tools, **kwargs)


class _LateResponseClient(_SequenceClient):
    def __init__(
        self,
        clock: _ManualClock,
        late_at: float,
        *responses: Any,
    ) -> None:
        super().__init__(*responses)
        self.clock = clock
        self.late_at = late_at

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        self.clock.now = self.late_at
        return super().generate_with_tools(prompt, available_tools, **kwargs)


class _TimedSequenceClient(_SequenceClient):
    def __init__(
        self,
        clock: _ManualClock,
        *timed_responses: tuple[float, Any],
    ) -> None:
        super().__init__(*(item[1] for item in timed_responses))
        self.clock = clock
        self.response_times = [item[0] for item in timed_responses]

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        self.clock.now = self.response_times[len(self.calls)]
        return super().generate_with_tools(prompt, available_tools, **kwargs)


class _HydrationAdvisoryClient:
    def __init__(
        self,
        clock: _ManualClock,
        *,
        native_continuation: bool,
        research_advisory_at: float,
    ) -> None:
        self.clock = clock
        self.native_continuation = native_continuation
        self.research_advisory_at = research_advisory_at
        self.calls: list[dict[str, Any]] = []
        self.received_evidence_outputs: list[dict[str, Any]] = []

    def _continuation(self, response_id: str) -> LLMContinuation | None:
        if not self.native_continuation:
            return None
        return LLMContinuation(
            provider="test",
            api_surface="responses",
            model="test-model",
            response_id=response_id,
        )

    def _latest_tool_output(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.native_continuation:
            tool_results = kwargs.get("tool_results")
            assert isinstance(tool_results, list)
            assert len(tool_results) == 1
            assert isinstance(tool_results[0], ToolResult)
            assert isinstance(tool_results[0].output, dict)
            return dict(tool_results[0].output)

        context = kwargs.get("context")
        assert isinstance(context, list)
        tool_messages = [
            item
            for item in context
            if isinstance(item, dict) and item.get("role") == "tool"
        ]
        assert tool_messages
        output = json.loads(tool_messages[-1]["content"])
        assert isinstance(output, dict)
        return output

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        call_number = len(self.calls)
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        if call_number == 0:
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="call-source-evidence",
                        payload={
                            "name": "general_read",
                            "arguments": {"query": "find the source"},
                        },
                    )
                ],
                continuation=self._continuation("response-source"),
            )
        if call_number == 1:
            envelope = self._latest_tool_output(kwargs)
            self.received_evidence_outputs.append(envelope)
            self.clock.now = self.research_advisory_at + 0.5
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_read_evidence",
                        call_id="call-hydrated-evidence",
                        payload={
                            "evidence_id": envelope["evidence_id"],
                            "json_pointer": "/record/z_summary",
                            "max_chars": 4_000,
                        },
                    )
                ],
                continuation=self._continuation("response-hydration"),
            )
        if call_number == 2:
            hydrated_slice = self._latest_tool_output(kwargs)
            self.received_evidence_outputs.append(hydrated_slice)
            return LLMResponse(
                text_response=(
                    "The final answer uses the explicitly hydrated evidence."
                )
            )
        raise AssertionError("unexpected extra model call")


class _RootAliasHydrationClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.hydrated_slice: dict[str, Any] | None = None

    @staticmethod
    def _latest_tool_output(kwargs: dict[str, Any]) -> dict[str, Any]:
        tool_results = kwargs.get("tool_results")
        assert isinstance(tool_results, list)
        assert len(tool_results) == 1
        assert isinstance(tool_results[0], ToolResult)
        assert isinstance(tool_results[0].output, dict)
        return dict(tool_results[0].output)

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        call_number = len(self.calls)
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        if call_number == 0:
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="root-alias-source",
                        payload={
                            "name": "general_read",
                            "arguments": {"query": "canonical record"},
                        },
                    )
                ],
                continuation=LLMContinuation(
                    provider="test",
                    api_surface="responses",
                    model="test-model",
                    response_id="root-source-response",
                ),
            )
        if call_number == 1:
            envelope = self._latest_tool_output(kwargs)
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_read_evidence",
                        call_id="root-alias-hydration",
                        payload={
                            "evidence_id": envelope["evidence_id"],
                            "json_pointer": "/",
                            "max_chars": 4_000,
                        },
                    )
                ],
                continuation=LLMContinuation(
                    provider="test",
                    api_surface="responses",
                    model="test-model",
                    response_id="root-hydration-response",
                ),
            )
        if call_number == 2:
            self.hydrated_slice = self._latest_tool_output(kwargs)
            return LLMResponse(text_response="The canonical identifier was retained.")
        raise AssertionError("unexpected model call")


class _FieldEqualsHydrationClient(_RootAliasHydrationClient):
    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        call_number = len(self.calls)
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        if call_number == 0:
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="field-source",
                        payload={
                            "name": "general_read",
                            "arguments": {"query": "list conversations"},
                        },
                    )
                ],
                continuation=LLMContinuation(
                    provider="test",
                    api_surface="responses",
                    model="test-model",
                    response_id="field-source-response",
                ),
            )
        if call_number == 1:
            envelope = self._latest_tool_output(kwargs)
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_read_evidence",
                        call_id="field-hydration",
                        payload={
                            "evidence_id": envelope["evidence_id"],
                            "field_equals": {"session_name": None},
                        },
                    )
                ],
                continuation=LLMContinuation(
                    provider="test",
                    api_surface="responses",
                    model="test-model",
                    response_id="field-hydration-response",
                ),
            )
        if call_number == 2:
            self.hydrated_slice = self._latest_tool_output(kwargs)
            return LLMResponse(text_response="The unnamed conversation was found.")
        raise AssertionError("unexpected model call")


def test_plain_answer_gets_trusted_scope_and_generic_read_doorway() -> None:
    client = _SequenceClient(LLMResponse(text_response="A useful answer."))
    gateway = _gateway(lambda **_kwargs: {"success": True})

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-1",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "A useful answer."
    assert result.duration_ms is not None
    assert result.duration_ms >= 0
    system_message = client.calls[0]["system_message"]
    assert "#V#person" in system_message
    assert "#V#org" in system_message
    assert "all other writes are unavailable" in system_message
    assert {tool.name for tool in client.calls[0]["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    catalogue_tool = next(
        tool
        for tool in client.calls[0]["available_tools"]
        if tool.name == "turn_capabilities"
    )
    assert "complete unranked compact purpose index" in catalogue_tool.description
    assert "represented workflows are peers" in catalogue_tool.description
    assert "representedness does not rank a plan" in catalogue_tool.description
    assert "smallest plan that can produce" in catalogue_tool.description
    assert "all alternatives being compared together" in catalogue_tool.description
    assert set(catalogue_tool.input_schema["properties"]) == {
        "query",
        "names",
        "offset",
        "limit",
    }


def test_model_eligibility_denial_is_returned_in_ordinary_language() -> None:
    denial = ModelExecutionEligibilityError(
        "OpenAI model 'gpt-5.6-terra' is not enabled for the current user or "
        "organisation.",
        provider="openai",
        model="gpt-5.6-terra",
    )
    client = _SequenceClient(denial)

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="gpt-5.6-terra",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="model-not-enabled",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_not_enabled"
    assert result.response_text == str(denial)
    assert len(client.calls) == 1
    assert result.llm_calls[0]["call_id"] == "model-not-enabled:llm:1"
    assert result.llm_calls[0]["requested_model"] == "gpt-5.6-terra"
    assert result.llm_calls[0]["selected_model"] == "gpt-5.6-terra"
    assert result.llm_calls[0]["effective_model"] is None
    assert result.llm_calls[0]["model_identity_source"] is None
    assert result.llm_calls[0]["provider_request_sent"] is False


def test_terminal_answer_can_update_a_revisable_conversation_situation() -> None:
    current_situation = (
        "We are deciding how to represent an evolving conversation situation. "
        "The storage choice is still open."
    )
    revised_situation = (
        "We are implementing a lightweight, inspectable text description of the "
        "conversation situation. It remains provisional and may contain "
        "sub-situations. The next step is to validate same-call continuity."
    )
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I have implemented the smallest same-call carrier seam.\n\n"
                "<von_conversation_situation>\n"
                f"{revised_situation}\n"
                "</von_conversation_situation>"
            )
        )
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Continue with the agreed first step.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-with-situation",
        conversation_id="conversation-123",
        conversation_situation=current_situation,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == (
        "I have implemented the smallest same-call carrier seam."
    )
    assert "<von_conversation_situation>" not in result.response_text
    assert revised_situation not in result.response_text
    assert result.conversation_situation == revised_situation

    system_message = client.calls[0]["system_message"]
    assert "conversation-123" in system_message
    assert current_situation in system_message
    assert "provisional, revisable theory" in system_message
    assert "one focused question" in system_message
    assert "unavailable information is distinct from performative permission" in (
        system_message
    )
    assert "<von_conversation_situation>" in system_message


def test_material_unknown_is_elicited_then_resolved_through_shared_situation() -> None:
    outstanding_situation = (
        "Objective: prepare the corpus comparison. "
        "Material unknown: which corpus the user intends. "
        "Outstanding question: Which corpus should I use? "
        "Independent work can continue on the comparison structure."
    )
    first_client = _SequenceClient(
        LLMResponse(
            text_response=(
                "Which corpus should I use?\n"
                "<von_conversation_situation>\n"
                f"{outstanding_situation}\n"
                "</von_conversation_situation>"
            )
        )
    )

    first_turn = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Prepare the comparison.",
        context=[],
        llm_client=first_client,
        model="test-model",
        conversation_id="conversation-elicitation",
        conversation_situation=(
            "Objective: prepare the corpus comparison. "
            "The intended corpus is not yet known."
        ),
        turn_id="turn-elicitation-question",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert first_turn.response_text == "Which corpus should I use?"
    assert first_turn.response_text.count("?") == 1
    assert "permission" not in first_turn.response_text.lower()
    assert first_turn.conversation_situation == outstanding_situation

    resolved_situation = (
        "Objective: compare the British National Corpus with the existing "
        "baseline. The user selected the British National Corpus, resolving "
        "the prior material unknown. Next step: compute the comparison."
    )
    second_client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I’ll use the British National Corpus and proceed with the "
                "comparison.\n"
                "<von_conversation_situation>\n"
                f"{resolved_situation}\n"
                "</von_conversation_situation>"
            )
        )
    )
    second_turn = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Use the British National Corpus.",
        context=[
            {"role": "assistant", "content": first_turn.response_text},
            {"role": "user", "content": "Use the British National Corpus."},
        ],
        llm_client=second_client,
        model="test-model",
        conversation_id="conversation-elicitation",
        conversation_situation=first_turn.conversation_situation,
        turn_id="turn-elicitation-answer",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert outstanding_situation in second_client.calls[0]["system_message"]
    assert second_turn.response_text == (
        "I’ll use the British National Corpus and proceed with the comparison."
    )
    assert "?" not in second_turn.response_text
    assert second_turn.conversation_situation == resolved_situation


def test_tool_resolvable_unknown_is_observed_without_questioning_the_user() -> None:
    invoked_queries: list[str] = []

    def _read(**kwargs: Any) -> dict[str, Any]:
        invoked_queries.append(str(kwargs.get("query")))
        return {"success": True, "current_corpus": "Lancaster-Oslo/Bergen"}

    revised_situation = (
        "Objective: continue the corpus comparison. The canonical configuration "
        "says the current corpus is Lancaster-Oslo/Bergen; no user answer was "
        "needed."
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-corpus",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current configured corpus"},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The configured corpus is Lancaster-Oslo/Bergen, so I can "
                "continue.\n"
                "<von_conversation_situation>\n"
                f"{revised_situation}\n"
                "</von_conversation_situation>"
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(_read),
        prompt="Continue with the configured corpus.",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-tool-resolvable",
        conversation_situation=(
            "Objective: continue the corpus comparison. "
            "The current configured corpus is unknown but available via tools."
        ),
        turn_id="turn-tool-resolvable",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked_queries == ["current configured corpus"]
    assert "?" not in result.response_text
    assert result.conversation_situation == revised_situation


def test_machine_observations_are_bounded_separate_and_not_redispatched() -> None:
    observations = [
        {
            "observation_id": observation_id,
            "effect_id": f"effect-{index}",
            "outcome": "succeeded",
            "canonical_terminal": True,
        }
        for index, observation_id in enumerate(
            [
                "omitted-old-a",
                "omitted-old-b",
                "retained-00",
                "retained-01",
                "retained-02",
                "retained-03",
                "retained-04",
                "retained-05",
                "retained-06",
                "retained-07",
            ]
        )
    ]
    projection = _bounded_conversation_observation_projection(observations)
    projection_with_prior_omissions = _bounded_conversation_observation_projection(
        observations,
        omitted_before=5,
    )

    assert projection is not None
    assert projection_with_prior_omissions is not None
    assert len(projection["observations"]) == _CONVERSATION_OBSERVATIONS_MAX_ITEMS
    assert projection["observations"] == observations[-8:]
    assert projection["omitted_count"] == 2
    assert projection_with_prior_omissions["omitted_count"] == 7
    assert (
        len(_json_bytes(projection["observations"]))
        <= _CONVERSATION_OBSERVATIONS_MAX_BYTES
    )

    client = _SequenceClient(
        LLMResponse(text_response="The recorded effect succeeded.")
    )
    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="What happened to the pending effect?",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-with-observations",
        conversation_situation="The effect was pending when the previous turn ended.",
        conversation_observations=observations,
        conversation_observation_state={"omitted_count": 3},
        turn_id="turn-with-observations",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The recorded effect succeeded."
    system_message = client.calls[0]["system_message"]
    assert "omitted-old-a" not in system_message
    assert "omitted-old-b" not in system_message
    assert "retained-00" in system_message
    assert "retained-07" in system_message
    assert '"omitted_count": 5' in system_message
    assert "projected separately from the provisional plain-text situation" in (
        system_message
    )
    assert "Never redispatch an effect merely to learn an outcome already recorded" in (
        system_message
    )
    assert "reconcile that outcome into the visible answer" in system_message


def test_invalid_provider_tool_call_gets_one_contract_informed_repair() -> None:
    invoked_queries: list[str] = []

    def _read(**kwargs: Any) -> dict[str, Any]:
        invoked_queries.append(str(kwargs.get("query")))
        return {"success": True, "value": "grounded result"}

    diagnostic = {
        "schema_version": "tool_call_validation.v1",
        "status": "invalid",
        "tool": "general_read",
        "error_code": "unknown_tool",
        "message": "Unknown tool requested: general_read",
        "payload": {"must_not_be_replayed": "x" * 10_000},
    }
    client = _SequenceClient(
        LLMResponse(text_response="", tool_call_diagnostics=[diagnostic]),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="corrected-read",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "grounded value"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The grounded result is available."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(_read),
        prompt="Read the grounded value.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-tool-call-diagnostic-repair",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "The grounded result is available."
    assert invoked_queries == ["grounded value"]
    assert len(client.calls) == 3
    repair_context = next(
        item
        for item in client.calls[1]["context"]
        if item.get("role") == "system"
        and "provider rejected your previous tool-call" in item.get("content", "")
    )
    assert "turn_invoke_capability" in repair_context["content"]
    assert "delegated capability name is not itself an exposed tool" in (
        repair_context["content"]
    )
    assert "must_not_be_replayed" not in repair_context["content"]
    assert result.llm_calls[0]["provider_tool_call_diagnostics"] == [
        {
            "error_code": "unknown_tool",
            "tool": "general_read",
            "message": "Unknown tool requested: general_read",
        }
    ]
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_tool_call_protocol_recovery"
    )
    assert recovery["action"] == "fresh_retry_with_contract_feedback"
    assert recovery["repair_attempted"] is True
    assert recovery["repair_succeeded"] is True
    assert recovery["repair_result_call_id"] == (
        "turn-tool-call-diagnostic-repair:llm:2"
    )


def test_repeated_invalid_provider_tool_call_returns_typed_non_success() -> None:
    diagnostic = {
        "error_code": "unknown_tool",
        "tool": "general_read",
        "message": "Unknown tool requested: general_read",
    }
    client = _SequenceClient(
        LLMResponse(text_response="", tool_call_diagnostics=[diagnostic]),
        LLMResponse(text_response="", tool_call_diagnostics=[diagnostic]),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Read the grounded value.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-tool-call-diagnostic-repeat",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 2
    assert result.terminal_status == "model_tool_call_invalid"
    assert result.response_text == (
        "The model attempted an invalid capability request and did not repair "
        "it on the bounded retry."
    )
    recovery_events = [
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_tool_call_protocol_recovery"
    ]
    assert [event["action"] for event in recovery_events] == [
        "fresh_retry_with_contract_feedback",
        "terminal_invalid_tool_call",
    ]
    assert recovery_events[0]["repair_succeeded"] is False
    assert recovery_events[0]["repair_result_call_id"] == (
        "turn-tool-call-diagnostic-repeat:llm:2"
    )


def test_oversized_machine_observation_is_omitted_without_partial_projection() -> None:
    projection = _bounded_conversation_observation_projection(
        [
            {
                "observation_id": "too-large",
                "payload": "x" * (_CONVERSATION_OBSERVATIONS_MAX_BYTES + 1),
            }
        ]
    )

    assert projection is not None
    assert projection["observations"] == []
    assert projection["omitted_count"] == 1


def test_absent_situation_sidecar_preserves_the_current_situation() -> None:
    current_situation = "The user and Von are still choosing the next useful step."
    client = _SequenceClient(LLMResponse(text_response="Here is the answer."))

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Answer from the situation already established.",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-unchanged",
        conversation_situation=current_situation,
        turn_id="turn-situation-unchanged",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Here is the answer."
    assert result.conversation_situation == current_situation


def test_situation_sidecar_without_visible_answer_is_a_non_answer() -> None:
    current_situation = "The user still needs a visible answer."
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "<von_conversation_situation>\n"
                "Updated theory but no user work product.\n"
                "</von_conversation_situation>"
            )
        )
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Give me the answer.",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-sidecar-only",
        conversation_situation=current_situation,
        turn_id="turn-sidecar-only",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_non_answer"
    assert result.response_text == (
        "The model returned a conversation-situation update but no "
        "user-visible answer."
    )
    assert result.conversation_situation == current_situation


@pytest.mark.parametrize(
    "raw_response",
    [
        (
            "Visible answer.\n"
            "<von_conversation_situation>\n"
            "Missing the terminal tag."
        ),
        "Visible answer.\n</von_conversation_situation>",
        (
            "Visible answer.\n"
            "<von_conversation_situation>Updated.</von_conversation_situation>\n"
            "Unexpected visible suffix."
        ),
        (
            "Visible answer.\n"
            "<von_conversation_situation>First.</von_conversation_situation>\n"
            "<von_conversation_situation>Second.</von_conversation_situation>"
        ),
        (
            "Visible answer.\n"
            "<von_conversation_situation >Protocol variant.</"
            "von_conversation_situation>"
        ),
        (
            "Visible answer.\n"
            "<VON_CONVERSATION_SITUATION>Protocol variant.</"
            "VON_CONVERSATION_SITUATION>"
        ),
        (
            "Visible answer.\n"
            "<von_conversation_situation>"
            + ("x" * (_CONVERSATION_SITUATION_MAX_CHARS + 1))
            + "</von_conversation_situation>"
        ),
    ],
)
def test_invalid_situation_sidecar_is_hidden_and_preserves_current_state(
    raw_response: str,
) -> None:
    visible_text, situation = _extract_conversation_situation_sidecar(
        raw_response,
        current_situation="Original situation.",
    )

    assert visible_text == "Visible answer."
    assert "von_conversation_situation" not in visible_text
    assert situation == "Original situation."


def test_model_slash_evidence_pointer_hydrates_the_whole_result() -> None:
    client = _RootAliasHydrationClient()

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "": "RFC slash would select this empty-key member.",
                "canonical_concept_id": "#V#stable_readback_id",
                "success": True,
            }
        ),
        prompt="Read the canonical record.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="root-alias-hydration",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The canonical identifier was retained."
    assert client.hydrated_slice is not None
    assert client.hydrated_slice["success"] is True
    assert client.hydrated_slice["selector"]["json_pointer"] is None
    assert (
        json.loads(client.hydrated_slice["content"])["canonical_concept_id"]
        == "#V#stable_readback_id"
    )
    read_tool = next(
        tool
        for tool in client.calls[0]["available_tools"]
        if tool.name == "turn_read_evidence"
    )
    assert "root alias" in read_tool.description
    assert "field_equals" in read_tool.input_schema["properties"]
    assert "JSON null" in (
        read_tool.input_schema["properties"]["field_equals"]["description"]
    )
    assert "root alias" in (
        read_tool.input_schema["properties"]["json_pointer"]["description"]
    )


def test_model_field_equals_hydrates_null_mapping_through_adaptive_tool() -> None:
    client = _FieldEqualsHydrationClient()

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "coverage_complete": True,
                "has_more": False,
                "conversations": [
                    {"session_id": "unnamed", "session_name": None},
                    {"session_id": "named", "session_name": "Named conversation"},
                ],
            }
        ),
        prompt="Find unnamed conversations.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="field-equals-hydration",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The unnamed conversation was found."
    assert client.hydrated_slice is not None
    assert client.hydrated_slice["success"] is True
    assert client.hydrated_slice["matches"][0]["json_pointer"] == "/conversations/0"
    assert client.hydrated_slice["conclusion"]["global_conclusion_supported"] is True


def test_ordinary_delegation_is_actor_capability_not_every_read_method() -> None:
    gateway = _delegation_gateway()

    without_mail = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
    )
    with_mail = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": "represented-profile",
        },
    )

    assert without_mail == ("concept_exists", "search_arxiv")
    assert with_mail == (
        "concept_exists",
        "gmail_list_messages",
        "search_arxiv",
    )
    assert "gmail_list_profiles" not in with_mail
    assert "list_recent_screenshots" not in with_mail

    gateway.disable()
    assert (
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#person",
            trusted_argument_values={
                "gmail_profile": "represented-profile",
            },
        )
        == ()
    )

    gateway.enable()
    assert ordinary_turn_capability_delegation(
        gateway,
        user_concept_id=None,
    ) == ("search_arxiv",)


def test_server_bound_capability_argument_is_not_model_visible() -> None:
    gateway = _gmail_gateway(lambda **_kwargs: {"success": True})
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": "represented-profile",
        },
    )

    catalogue = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["gmail_list_messages"]},
    )

    assert catalogue["total"] == 1
    input_schema = catalogue["capabilities"][0]["input_schema"]
    assert set(input_schema["properties"]) == {"query"}
    assert "profile" not in input_schema.get("required", [])
    assert "x-von-argument-aliases" not in input_schema


def test_optional_trusted_binding_is_hidden_and_does_not_require_a_value() -> None:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="actor_resource_list",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                optional={
                    "query": str,
                    "acting_user_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                },
                allow_unknown=False,
            ),
            category="read",
            ordinary_turn_trusted_argument_bindings={
                "acting_user_concept_id": "actor_user_concept_id",
            },
            ordinary_turn_optional_trusted_argument_bindings={
                "organisation_concept_id": "actor_organisation_concept_id",
            },
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    personal_trusted = {
        "actor_user_concept_id": "#V#person",
        "actor_organisation_concept_id": None,
    }

    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values=personal_trusted,
    )
    assert delegated == ("actor_resource_list",)
    assert ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={"actor_organisation_concept_id": "#V#org"},
    ) == ()
    assert ordinary_turn_capability_delegation(
        gateway,
        user_concept_id=None,
        trusted_argument_values=personal_trusted,
    ) == ()

    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["actor_resource_list"]},
        trusted_argument_values=personal_trusted,
    )["capabilities"][0]
    assert capability["server_bound_arguments"] == [
        "acting_user_concept_id",
        "organisation_concept_id",
    ]
    assert set(capability["input_schema"]["properties"]) == {"query"}

    personal_payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="actor_resource_list",
        model_payload={
            "query": "mine",
            "acting_user_concept_id": "#V#attacker",
            "organisation_concept_id": "#V#attacker_org",
        },
        trusted_argument_values=personal_trusted,
    )
    assert personal_payload == {
        "query": "mine",
        "acting_user_concept_id": "#V#person",
    }

    organisation_payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="actor_resource_list",
        model_payload={"organisation_concept_id": "#V#attacker_org"},
        trusted_argument_values={
            **personal_trusted,
            "actor_organisation_concept_id": "#V#trusted_org",
        },
    )
    assert organisation_payload == {
        "acting_user_concept_id": "#V#person",
        "organisation_concept_id": "#V#trusted_org",
    }


def test_authorised_resource_choices_expose_only_stable_selectors() -> None:
    gateway = _gmail_choice_gateway(lambda **_kwargs: {"success": True})
    trusted = {"gmail_profile": _gmail_profile_choices(default=None)}
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values=trusted,
    )

    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["gmail_list_messages"]},
        trusted_argument_values=trusted,
    )["capabilities"][0]

    assert capability["server_authorised_choice_arguments"] == ["profile"]
    input_schema = capability["input_schema"]
    assert input_schema["properties"]["profile"]["enum"] == [
        "#V#gmail_profile_personal",
        "#V#gmail_profile_zhan",
    ]
    assert "profile" in input_schema["required"]
    projected_choices = input_schema["x-von-authorised-argument-choices"][
        "profile"
    ]["choices"]
    assert projected_choices == [
        {
            "selector": "#V#gmail_profile_personal",
            "resource_id": "#V#gmail_profile_personal",
            "display_label": "michael@example.test",
            "represented_identity_concept_ids": ["#V#michael"],
        },
        {
            "selector": "#V#gmail_profile_zhan",
            "resource_id": "#V#gmail_profile_zhan",
            "display_label": "zhan@example.test",
            "represented_identity_concept_ids": ["#V#zhan"],
        },
    ]
    assert "personal-runtime" not in json.dumps(input_schema)
    assert "zhan-runtime" not in json.dumps(input_schema)


def test_authorised_resource_choice_maps_selector_and_records_scope() -> None:
    seen_arguments: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {
            "success": True,
            "messages": [],
            "effective_query": {"scope": "whole_mailbox"},
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-zhan-mail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {
                            "profile": "#V#gmail_profile_zhan",
                            "scope": "whole_mailbox",
                            "query": "newer_than:1d",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="I checked zhan@example.test."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_choice_gateway(handler),
        prompt="Check Zhan's recent email.",
        context=[],
        llm_client=client,
        model="test-model",
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": _gmail_profile_choices(default=None),
        },
        turn_id="turn-zhan-mail-choice",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "I checked zhan@example.test."
    system_message = client.calls[0]["system_message"]
    assert "AUTHORISED CONNECTOR RESOURCE CHOICES" in system_message
    assert "zhan@example.test" in system_message
    assert "whole_mailbox" in system_message
    assert "profile_default_view" in system_message
    assert "request leaves the connector resource unqualified" in system_message
    assert "selected human-readable resource in the first answer" in system_message
    assert "Repeat it only when the resource changes" in system_message
    assert "never expose an internal resource ID or runtime alias" in system_message
    assert "zhan-runtime" not in system_message
    assert seen_arguments == {
        "profile": "zhan-runtime",
        "scope": "whole_mailbox",
        "query": "newer_than:1d",
    }
    invocation = result.tool_invocations[0]
    assert invocation["effective_arguments"] == seen_arguments
    assert invocation["resource_scope"] == {
        "source_family": "gmail",
        "resource_id": "#V#gmail_profile_zhan",
        "runtime_alias": "zhan-runtime",
        "display_label": "zhan@example.test",
        "selection_source": "adaptive_authorised_choice",
        "view_scope": "whole_mailbox",
    }


def test_authorised_resource_choice_uses_default_or_rejects_foreign_value() -> None:
    seen_arguments: list[dict[str, Any]] = []

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.append(dict(kwargs))
        return {"success": True, "messages": []}

    default_client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-default-mail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {"query": "newer_than:1d"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="No recent messages."),
    )
    execute_adaptive_turn(
        gateway=_gmail_choice_gateway(handler),
        prompt="Check recent email.",
        context=[],
        llm_client=default_client,
        model="test-model",
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": _gmail_profile_choices(
                default="#V#gmail_profile_personal"
            ),
        },
        turn_id="turn-default-mail-choice",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )
    assert seen_arguments == [
        {"profile": "personal-runtime", "query": "newer_than:1d"}
    ]

    foreign_client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-foreign-mail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {"profile": "other-person-runtime"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="That mailbox is not authorised."),
    )
    foreign_result = execute_adaptive_turn(
        gateway=_gmail_choice_gateway(handler),
        prompt="Check somebody else's mailbox.",
        context=[],
        llm_client=foreign_client,
        model="test-model",
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": _gmail_profile_choices(default=None),
        },
        turn_id="turn-foreign-mail-choice",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert seen_arguments == [
        {"profile": "personal-runtime", "query": "newer_than:1d"}
    ]
    invocation = foreign_result.tool_invocations[0]
    assert invocation["status"] == "error"
    assert json.loads(invocation["evidence"]["preview"])["error_code"] == (
        "argument_choice_not_authorised"
    )

    missing_client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-ambiguous-mail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {"query": "newer_than:1d"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="Which mailbox should I check?"),
    )
    missing_result = execute_adaptive_turn(
        gateway=_gmail_choice_gateway(handler),
        prompt="Check recent email.",
        context=[],
        llm_client=missing_client,
        model="test-model",
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": _gmail_profile_choices(default=None),
        },
        turn_id="turn-ambiguous-mail-choice",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )
    assert seen_arguments == [
        {"profile": "personal-runtime", "query": "newer_than:1d"}
    ]
    missing_error = json.loads(
        missing_result.tool_invocations[0]["evidence"]["preview"]
    )
    assert missing_error["error_code"] == "authorised_argument_choice_required"


def test_capability_catalogue_rejects_placeholder_description_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import tool_metadata_service

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="grounded_direct_read",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"query": str}),
            description=(
                "Read grounded records for one anchor and return bounded "
                "content-bearing evidence."
            ),
            ordinary_turn_public=True,
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "_load_from_vontology",
        lambda: {
            "grounded_direct_read": tool_metadata_service.ToolMetadata(
                tool_name="grounded_direct_read",
                concept_id="#V#grounded_direct_read_tool",
                description=("Internal MCP metadata concept for grounded_direct_read."),
            )
        },
    )
    tool_metadata_service.invalidate_cache()
    try:
        capability = _capability_catalogue(
            gateway,
            ("grounded_direct_read",),
            {"names": ["grounded_direct_read"]},
        )["capabilities"][0]
    finally:
        tool_metadata_service.invalidate_cache()

    assert capability["description"] == (
        "Read grounded records for one anchor and return bounded "
        "content-bearing evidence."
    )


def test_effect_delegation_is_authenticated_and_exactly_metadata_marked() -> None:
    gateway = _effect_gateway(lambda _name, _arguments: {"success": True})
    trusted = {
        "turn_namespace": "#V#person@org",
        "actor_user_concept_id": "#V#person",
    }

    assert (
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id=None,
            trusted_argument_values=trusted,
        )
        == ()
    )
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values=trusted,
    )

    assert set(delegated) == {
        "general_read",
        "create_concepts",
        "upsert_text_relation",
        "add_relationship",
    }
    assert "other_write" not in delegated


def test_advisory_effect_does_not_project_inactive_admission_boundary() -> None:
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.5,
        effect_admission_window_sec=0.125,
    )
    trusted = {
        "turn_namespace": "#V#person@org",
        "actor_user_concept_id": "#V#person",
    }
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values=trusted,
    )

    catalogue_entry = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["create_concepts"]},
    )["capabilities"][0]

    assert gateway.get_method_effect_admission_window_sec("create_concepts") is None
    assert gateway.get_method_effect_admission_window_sec("general_read") is None
    assert catalogue_entry["semantic_effect"] is True
    assert "minimum_effect_window_seconds" not in catalogue_entry

    clock = _ManualClock(100.0)
    client = _SequenceClient(LLMResponse(text_response="A useful answer."))
    execute_adaptive_turn(
        gateway=gateway,
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-window-scope",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert "Elapsed-time budgets are advisory" in client.calls[0]["system_message"]
    assert "Model and capability elapsed thresholds are advisory too" in (
        client.calls[0]["system_message"]
    )


def test_default_final_answer_reserve_is_nonzero_and_clamped() -> None:
    gateway = _effect_gateway(lambda _name, _arguments: {"success": True})
    clock = _ManualClock(100.0)
    client = _SequenceClient(LLMResponse(text_response="A useful answer."))

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="default-answer-reserve",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        clock=clock,
    )

    assert "Elapsed-time budgets are advisory" in client.calls[0]["system_message"]
    allocation = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_budget_allocation"
    )
    assert allocation["effective_final_answer_reserve_seconds"] == 4.0
    assert allocation["final_answer_reserve_source"] == "environment_or_default"
    assert allocation["explicit_zero_override"] is False
    assert allocation["enforcement"] == "advisory"
    assert allocation["model_call_advisory_seconds"] == 120.0
    assert allocation["model_call_hard_timeout_seconds"] is None
    assert allocation["provider_retry_wait_max_seconds"] == 90.0


@pytest.mark.parametrize("invalid_wait_max", [0.0, -1.0, float("nan"), float("inf")])
def test_provider_retry_wait_max_must_be_finite_and_positive(
    invalid_wait_max: float,
) -> None:
    with pytest.raises(
        ValueError,
        match="provider_retry_wait_max_seconds must be finite and positive",
    ):
        execute_adaptive_turn(
            gateway=None,
            prompt="A short request.",
            context=[],
            llm_client=_SequenceClient(LLMResponse(text_response="unused")),
            model="test-model",
            turn_id="invalid-provider-retry-wait-max",
            turn_budget_seconds=10,
            final_synthesis_reserve_seconds=2,
            provider_retry_wait_max_seconds=invalid_wait_max,
        )


@pytest.mark.parametrize(
    ("org_concept_id", "user_namespace"),
    (("#V#org", "#V#person@org"), (None, "#V#person")),
)
def test_create_effect_scope_is_model_selected_and_identity_is_server_bound(
    monkeypatch: pytest.MonkeyPatch,
    org_concept_id: str | None,
    user_namespace: str,
) -> None:
    issued = _stub_same_turn_ontology_delegation(monkeypatch)
    seen: dict[str, Any] = {}

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "create_concepts"
        seen.update(arguments)
        return {"success": True, "effect_status": "succeeded", "changed": True}

    gateway = _effect_gateway(handler, selectable_create_scope=True)
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={
            "turn_namespace": user_namespace,
            "actor_user_concept_id": "#V#person",
        },
    )
    assert "create_concepts" in delegated
    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["create_concepts"]},
    )["capabilities"][0]
    assert {
        "namespace",
        "created_by_concept_id",
        "organisation_concept_id",
        "visibility_scope_mode",
    }.isdisjoint(capability["input_schema"]["properties"])
    assert capability["input_schema"]["properties"]["scope_mode"]["enum"] == [
        "user_only_default",
        "organisation_general",
        "global_general",
        None,
    ]

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-1",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "namespace": "#V#spoof@other",
                            "created_by_concept_id": "#V#spoof",
                            "organisation_concept_id": "#V#other",
                            "org_id": "#V#other",
                            "scope_mode": "global_general",
                            "visibility_scope_mode": "global_general",
                            "concepts": [{"name": "Bounded representation"}],
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="Created and ready for canonical read-back."),
    )
    execute_adaptive_turn(
        gateway=gateway,
        prompt="Represent this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace=user_namespace,
        user_concept_id="#V#person",
        org_concept_id=org_concept_id,
        turn_id="create-scope",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert seen["namespace"] == user_namespace
    assert seen["created_by_concept_id"] == "#V#person"
    assert seen["organisation_concept_id"] is None
    assert seen["org_id"] is None
    assert seen["scope_mode"] == "global_general"
    assert seen["visibility_scope_mode"] is None
    assert len(issued) == 1
    assert issued[0]["arguments"]["scope_mode"] == "global_general"
    assert issued[0]["arguments"]["organisation_concept_id"] is None
    assert issued[0]["actor_concept_id"] == "#V#person"
    assert issued[0]["organisation_concept_id"] == org_concept_id


def test_invalid_effect_arguments_are_returned_for_correction_without_poisoning_turn() -> (
    None
):
    invoked: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="record_source_processing_marker",
            handler=lambda **arguments: (
                invoked.append(dict(arguments))
                or {
                    "success": True,
                    "effect_status": "succeeded",
                    "changed": True,
                }
            ),
            input_schema=Schema(
                required={
                    "source_system": str,
                    "source_profile": str,
                    "source_item_id": str,
                },
                allow_unknown=False,
                description="Record one source-processing marker.",
            ),
            category="write",
            ordinary_turn_effect=True,
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(write_timeout_sec=1.0),
        enabled=True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="marker-without-profile",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": {
                            "source_system": "gmail",
                            "source_item_id": "message-1",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="marker-with-profile",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": {
                            "source_system": "gmail",
                            "source_profile": "personal-gmail",
                            "source_item_id": "message-1",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The corrected marker write succeeded."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Record the exact source marker.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="correct-invalid-effect-arguments",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == [
        {
            "source_system": "gmail",
            "source_profile": "personal-gmail",
            "source_item_id": "message-1",
        }
    ]
    validation_message = next(
        item
        for item in client.calls[1]["context"]
        if item.get("tool_call_id") == "marker-without-profile"
    )
    validation_result = json.loads(validation_message["content"])
    assert validation_result["status"] == "not_started"
    assert validation_result["changed"] is False
    assert validation_result["error_code"] == "capability_arguments_invalid"
    assert "source_profile" in validation_result["preview"]
    assert result.terminal_status == "completed"
    assert result.response_text == "The corrected marker write succeeded."
    assert result.tool_invocations[0]["effect_status"] == "not_started"
    assert result.tool_invocations[0]["turn_finality_required"] is False
    assert result.tool_invocations[1]["effect_status"] == "succeeded"


def test_non_object_effect_arguments_are_returned_for_correction_without_poisoning_turn() -> (
    None
):
    invoked: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="record_source_processing_marker",
            handler=lambda **arguments: (
                invoked.append(dict(arguments))
                or {
                    "success": True,
                    "effect_status": "succeeded",
                    "changed": True,
                }
            ),
            input_schema=Schema(
                required={
                    "source_system": str,
                    "source_profile": str,
                    "source_item_id": str,
                },
                allow_unknown=False,
                description="Record one source-processing marker.",
            ),
            category="write",
            ordinary_turn_effect=True,
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(write_timeout_sec=1.0),
        enabled=True,
    )
    corrected_arguments = {
        "source_system": "gmail",
        "source_profile": "personal-gmail",
        "source_item_id": "message-1",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="marker-non-object-arguments",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": ["gmail", "personal-gmail", "message-1"],
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="marker-corrected-object-arguments",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": corrected_arguments,
                    },
                )
            ],
        ),
        LLMResponse(text_response="The corrected marker write succeeded."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Record the exact source marker.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="correct-non-object-effect-arguments",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == [corrected_arguments]
    validation_message = next(
        item
        for item in client.calls[1]["context"]
        if item.get("tool_call_id") == "marker-non-object-arguments"
    )
    validation_result = json.loads(validation_message["content"])
    validation_receipt = json.loads(validation_result["preview"])
    assert validation_receipt["error_code"] == "capability_arguments_invalid"
    assert validation_receipt["status"] == "not_started"
    assert validation_receipt["effect_status"] == "not_started"
    assert validation_receipt["mutation_outcome"] == "not_started"
    assert validation_receipt["changed"] is False
    invalid_invocation, corrected_invocation = result.tool_invocations
    assert invalid_invocation["effect_status"] == "not_started"
    assert invalid_invocation["turn_finality_required"] is False
    assert corrected_invocation["effect_status"] == "succeeded"
    assert result.terminal_status == "completed"
    assert result.response_text == "The corrected marker write succeeded."


def test_non_object_read_arguments_use_the_same_not_started_contract() -> None:
    invoked: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="read_source_marker",
            handler=lambda **arguments: (
                invoked.append(dict(arguments))
                or {"success": True, "source_item_id": arguments["source_item_id"]}
            ),
            input_schema=Schema(
                required={"source_item_id": str},
                allow_unknown=False,
                description="Read one source-processing marker.",
            ),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(write_timeout_sec=1.0),
        enabled=True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-marker-non-object-arguments",
                    payload={
                        "name": "read_source_marker",
                        "arguments": ["message-1"],
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-marker-corrected-arguments",
                    payload={
                        "name": "read_source_marker",
                        "arguments": {"source_item_id": "message-1"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The marker read succeeded."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Read the exact source marker.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="correct-non-object-read-arguments",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == [{"source_item_id": "message-1"}]
    validation_message = next(
        item
        for item in client.calls[1]["context"]
        if item.get("tool_call_id") == "read-marker-non-object-arguments"
    )
    validation_result = json.loads(validation_message["content"])
    validation_receipt = json.loads(validation_result["preview"])
    assert validation_receipt["error_code"] == "capability_arguments_invalid"
    assert validation_receipt["status"] == "not_started"
    assert validation_receipt["effect_status"] == "not_started"
    assert validation_receipt["mutation_outcome"] == "not_started"
    assert validation_receipt["changed"] is False
    assert result.terminal_status == "completed"
    assert result.response_text == "The marker read succeeded."


@pytest.mark.parametrize(
    ("error_code", "effect_status", "expected"),
    (
        ("capability_arguments_invalid", "not_started", False),
        ("invalid_capability_arguments", "failed", False),
        ("ontology_mutation_target_not_accessible", "not_started", True),
    ),
)
def test_only_pre_dispatch_argument_rejections_are_exempt_from_turn_finality(
    error_code: str,
    effect_status: str,
    expected: bool,
) -> None:
    assert (
        _effect_requires_turn_finality(
            capability_kind="registered_tool",
            effect_status=effect_status,
            changed=False,
            raw_payload={
                "success": False,
                "status": "not_started",
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "error_code": error_code,
            },
        )
        is expected
    )


def test_governed_scope_preview_is_receipted_but_not_semantic_finality() -> None:
    assert (
        _effect_requires_turn_finality(
            capability_kind="registered_tool",
            effect_status="not_started",
            changed=False,
            raw_payload={
                "success": True,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "preview": True,
                "operational_state_effect": True,
                "semantic_effect": False,
                "authority_receipt": {"status": "previewed"},
            },
        )
        is False
    )
    assert (
        _effect_requires_turn_finality(
            capability_kind="registered_tool",
            effect_status="not_started",
            changed=False,
            raw_payload={
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "preview": True,
            },
        )
        is True
    )


def test_effect_subject_authority_matches_actor_or_organisation_scope(
    monkeypatch,
) -> None:
    from src.backend.db import mongo_client

    class _Collection:
        def __init__(self) -> None:
            self.relationships: dict[str, Any] = {}

        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": dict(self.relationships)}

    collection = _Collection()
    monkeypatch.setattr(mongo_client, "get_concepts_collection", lambda: collection)
    scope = TrustedTurnScope(
        user_concept_id="#V#person",
        organisation_concept_id="#V#org",
        namespace="#V#person@org",
    )

    collection.relationships = {
        "#V#specific_to_user": ["#V#other_person"],
        "#V#specific_to_organisation": ["#V#org"],
    }
    assert _effect_subject_authorised("#V#subject", scope) is True
    collection.relationships["#V#specific_to_user"] = ["#V#person"]
    assert _effect_subject_authorised("#V#subject", scope) is True
    collection.relationships = {
        "#V#specific_to_organisation": ["#V#org"],
    }
    assert _effect_subject_authorised("#V#subject", scope) is True
    collection.relationships = {
        "#V#specific_to_user": ["#V#other_person"],
        "#V#specific_to_organisation": ["#V#other_org"],
    }
    assert _effect_subject_authorised("#V#subject", scope) is False
    collection.relationships = {}
    assert _effect_subject_authorised("#V#subject", scope) is False
    assert _effect_subject_authorised("#V#person", scope) is True
    assert _effect_subject_authorised("#V#org", scope) is False


def test_ordinary_effect_authorises_actor_profile_without_visibility_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.db import mongo_client

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: (_ for _ in ()).throw(
            AssertionError("actor identity should not require visibility lookup")
        ),
    )
    issued = _stub_same_turn_ontology_delegation(monkeypatch)
    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="actor-profile",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#person",
                            "predicate": "#V#hasResearchInterest",
                            "target": "#V#topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The actor profile was updated."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Add this research interest to my profile.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="actor-profile",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == ["add_relationship"]
    assert [item["method_name"] for item in issued] == ["add_relationship"]
    assert result.tool_invocations[0]["effect_status"] == "succeeded"
    assert result.tool_invocations[0]["changed"] is True


def test_canonical_outcome_rejects_pre_reconciliation_situation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    current_situation = "The representation effect has not yet been observed."
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I have not changed anything.\n"
                "<von_conversation_situation>\n"
                "The representation definitely did not change.\n"
                "</von_conversation_situation>"
            ),
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="effect-before-model-failure",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [{"name": "A real represented concept"}]
                        },
                    },
                )
            ],
        ),
        TimeoutError("final model call failed"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, _arguments: {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
            }
        ),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-before-model-failure",
        conversation_id="conversation-with-effect-failure",
        conversation_situation=current_situation,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_error"
    assert result.response_authority == "canonical_outcome"
    assert result.canonical_outcome_spoken_text == (
        "I couldn't produce a fully reliable final answer. The create concepts "
        "step was reported as successful, but I couldn't confirm the result "
        "independently. The exact details are on screen."
    )
    assert "## Effect outcome report" in result.response_text
    assert "`create_concepts`" in result.response_text
    assert "I have not changed anything" not in result.response_text
    assert "von_conversation_situation" not in result.response_text
    assert result.conversation_situation == current_situation
    assert result.tool_invocations[0]["effect_status"] == "succeeded"
    assert result.tool_invocations[0]["changed"] is True


def test_model_error_after_read_rejects_interim_situation_sidecar() -> None:
    current_situation = "The canonical corpus is not yet observed."
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I am checking the canonical corpus.\n"
                "<von_conversation_situation>\n"
                "Stale interim theory before the read result.\n"
                "</von_conversation_situation>"
            ),
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-model-failure",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "canonical corpus"},
                    },
                )
            ],
        ),
        TimeoutError("final model call failed"),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "canonical_corpus": "Lancaster-Oslo/Bergen",
            }
        ),
        prompt="Which corpus is canonical?",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="read-before-model-failure",
        conversation_id="conversation-read-failure",
        conversation_situation=current_situation,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_error"
    assert result.response_text == "I am checking the canonical corpus."
    assert "von_conversation_situation" not in result.response_text
    assert result.conversation_situation == current_situation


def test_post_handler_output_validation_failure_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    committed: list[str] = []

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        committed.append("#V#committed_before_invalid_receipt")
        return {"success": True}

    client = _SequenceClient(
        LLMResponse(
            text_response="I have not changed anything.",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invalid-receipt-after-commit",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [{"name": "Committed before invalid receipt"}]
                        },
                    },
                )
            ],
        ),
        TimeoutError("final model call failed"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            effect_output_schema=Schema(
                required={"success": bool, "changed": bool},
                allow_unknown=True,
            ),
        ),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="invalid-receipt-after-commit",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert committed == ["#V#committed_before_invalid_receipt"]
    assert result.terminal_status == "model_error"
    assert result.response_authority == "canonical_outcome"
    assert "## Effect outcome report" in result.response_text
    assert "`create_concepts`" in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert result.tool_invocations[0]["changed"] is None


@pytest.mark.parametrize(
    ("answer_reserve", "effect_finished_at"),
    [
        (0.0, 8.1),
        (2.0, 8.5),
    ],
    ids=["evidence-capable-final", "answer-only-final"],
)
def test_effect_removes_false_draft_from_fresh_final_context(
    answer_reserve: float,
    effect_finished_at: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    started = time.monotonic()
    clock = _ManualClock(started)

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        clock.now = started + effect_finished_at
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    false_draft = "I have not changed anything."
    client = _SequenceClient(
        LLMResponse(
            text_response=false_draft,
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=f"effect-before-{answer_reserve}",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "A represented concept"}]},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The represented concept was created."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"effect-draft-{answer_reserve}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=answer_reserve,
        clock=clock,
    )

    assert result.response_text == "The represented concept was created."
    assert len(client.calls) == 2
    final_call = client.calls[1]
    assert {tool.name for tool in final_call["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    assert not any(
        item.get("role") == "assistant" and item.get("content") == false_draft
        for item in final_call["context"]
    )
    assert false_draft not in json.dumps(final_call["context"])


def test_late_effect_completion_is_persisted_without_rewriting_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import turn_execution_record_service

    # This unit test replaces the durable observation write below. Keep the
    # remaining turn-finality bookkeeping on the in-memory database so an
    # unavailable developer/CI Mongo does not consume the observer's bounded
    # release window and turn the intended late success into a late error.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    _stub_same_turn_ontology_delegation(monkeypatch)
    release_handler = Event()
    observation_persisted = Event()
    persisted: list[dict[str, Any]] = []

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        while not internal_mcp_cancellation_requested():
            time.sleep(0.001)
        assert release_handler.wait(timeout=10.0)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_ids": ["#V#late_real_concept"],
        }

    def persist_phase(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        if kwargs.get("phase") == "late_terminal":
            observation_persisted.set()
        return {"updated": True}

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        persist_phase,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="late-effect",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "A late real concept"}]},
                    },
                )
            ],
        ),
        TimeoutError("model failed after the effect timeout"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            write_timeout_sec=0.02,
            hard_timeout_enabled=True,
        ),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="late-effect-request",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_authority == "canonical_outcome"
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert "status `indeterminate`" in result.response_text

    release_handler.set()
    assert observation_persisted.wait(timeout=1.0)
    late_phases = [item for item in persisted if item.get("phase") == "late_terminal"]
    assert len(late_phases) == 1
    durable = late_phases[0]
    assert durable["request_id"] == "late-effect-request"
    assert durable["effect_id"] == result.tool_invocations[0]["effect_id"]
    assert durable["observation"]["effect_status"] == "succeeded"
    assert durable["observation"]["changed"] is True
    # The already returned turn remains an honest point-in-time snapshot.
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"


def test_effect_uses_its_method_liveness_window_not_a_turn_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    clock = _ManualClock()
    observed_deadlines: list[float | None] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=3.0,
        effect_admission_window_sec=3.0,
    )

    def invoke(
        method_name: str,
        _arguments: dict[str, Any],
        *,
        deadline_monotonic: float | None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        observed_deadlines.append(deadline_monotonic)
        return SimpleNamespace(
            payload={
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "mutation_outcome": "completed",
                "outcome_finality": "terminal_for_turn",
                "private_detail": "x" * 10_000,
            },
            execution_id=f"execution-{method_name}",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    client = _TimedSequenceClient(
        clock,
        (
            3.5,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="protected-window-effect",
                        payload={
                            "name": "create_concepts",
                            "arguments": {"concepts": [{"name": "Protected"}]},
                        },
                    )
                ],
            ),
        ),
        (3.6, LLMResponse(text_response="The effect completed.")),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Represent this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="protected-effect-window",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=6,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "The effect completed."
    assert observed_deadlines == [None]
    invocation = result.tool_invocations[0]
    assert invocation["effect_status"] == "succeeded"
    assert invocation["mutation_outcome"] == "completed"
    assert invocation["outcome_finality"] == "terminal_for_turn"
    assert "effective_payload" not in invocation
    assert "private_detail" not in invocation
    assert "x" * 10_000 not in json.dumps(invocation)


def test_mixed_effect_batch_preserves_order_with_independent_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    clock = _ManualClock()
    observed: list[tuple[str, float | None]] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.05,
        effect_admission_window_sec=0.05,
    )

    def invoke(
        method_name: str,
        _arguments: dict[str, Any],
        *,
        deadline_monotonic: float | None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        observed.append((method_name, deadline_monotonic))
        is_effect = method_name != "general_read"
        return SimpleNamespace(
            payload={
                "success": True,
                **(
                    {"effect_status": "succeeded", "changed": True}
                    if is_effect
                    else {"observed": True}
                ),
            },
            execution_id=f"execution-{len(observed)}",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"mixed-{index}",
            payload={"name": name, "arguments": arguments},
        )
        for index, (name, arguments) in enumerate(
            (
                ("general_read", {"query": "before"}),
                ("create_concepts", {"concepts": [{"name": "One"}]}),
                ("general_read", {"query": "between"}),
                ("add_relationship", {"source_id": "#V#person"}),
                ("general_read", {"query": "after"}),
            )
        )
    ]
    client = _TimedSequenceClient(
        clock,
        (0.1, LLMResponse(text_response="", tool_calls=calls)),
        (0.15, LLMResponse(text_response="The ordered batch completed.")),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Apply and verify both effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="mixed-suffix-reservation",
        turn_budget_seconds=1.0,
        final_synthesis_reserve_seconds=0.8,
        final_answer_reserve_seconds=0.2,
        clock=clock,
    )

    assert result.response_text == "The ordered batch completed."
    assert [item[0] for item in observed] == [
        "general_read",
        "create_concepts",
        "general_read",
        "add_relationship",
        "general_read",
    ]
    assert [item[1] for item in observed] == [None] * 5


def test_invalid_effect_does_not_reserve_window_or_block_valid_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    clock = _ManualClock()
    invoked: list[str] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.11,
        effect_admission_window_sec=0.11,
    )

    def invoke(method_name: str, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        invoked.append(method_name)
        return SimpleNamespace(
            payload={
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
            },
            execution_id=f"execution-{method_name}",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    client = _TimedSequenceClient(
        clock,
        (
            0.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="invalid-relationship",
                        payload={
                            "name": "add_relationship",
                            "arguments": {
                                "source_id": "#V#person",
                                "predicate": "#V#specific_to_user",
                                "target": "#V#other_actor",
                            },
                        },
                    ),
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="valid-create",
                        payload={
                            "name": "create_concepts",
                            "arguments": {"concepts": [{"name": "One"}]},
                        },
                    ),
                ],
            ),
        ),
        (0.15, LLMResponse(text_response="The valid effect completed.")),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Apply both effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="independent-effect-admission",
        turn_budget_seconds=1.0,
        final_synthesis_reserve_seconds=0.8,
        final_answer_reserve_seconds=0.7,
        clock=clock,
    )

    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert "The valid effect completed." in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text
    assert "visibility_effect_not_delegated" in result.response_text
    assert "`create_concepts`" in result.response_text
    assert invoked == ["create_concepts"]
    assert len(result.tool_invocations) == 2
    assert len({item["effect_id"] for item in result.tool_invocations}) == 2
    assert result.tool_invocations[0]["status"] == "error"
    assert result.tool_invocations[0]["effect_status"] == "failed"
    assert result.tool_invocations[0]["changed"] is False
    assert "visibility_effect_not_delegated" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )
    assert result.tool_invocations[1]["status"] == "ok"
    assert result.tool_invocations[1]["effect_status"] == "succeeded"


def test_cited_non_succeeded_ontology_effect_replaces_false_success_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_id = "cited-failed-ontology-effect"
    good_call_id = "add-student-a"
    failed_call_id = "add-gael-gendron"
    good_effect_id = _effect_id(
        turn_id=turn_id,
        call_id=good_call_id,
        capability_name="add_relationship",
    )
    failed_effect_id = _effect_id(
        turn_id=turn_id,
        call_id=failed_call_id,
        capability_name="add_relationship",
    )
    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        lambda **_kwargs: {"delegation_id": "delegation-test"},
    )

    def handler(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        source_id = str(arguments["source_id"])
        if source_id == "#V#gael_gendron":
            return {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "error_code": "ontology_mutation_target_not_accessible",
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "source_id": source_id,
            "predicate": "is_an_instance_of",
            "target": "#V#student",
            "canonical_read_back": {
                "source_id": source_id,
                "source_exists": True,
                "predicate": "is_an_instance_of",
                "target": "#V#student",
                "relationship_present": True,
                "inverse_predicate": "has_instance",
                "inverse_relationship_present": True,
            },
        }

    drafted_claim = (
        "| #V#student_a | Added; forward and inverse verified | "
        f"{good_effect_id} |\n"
        "| #V#gael_gendron | Added; forward and inverse verified | "
        f"{failed_effect_id} |"
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=good_call_id,
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#student_a",
                            "predicate": "is_an_instance_of",
                            "target": "#V#student",
                        },
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=failed_call_id,
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#gael_gendron",
                            "predicate": "is_an_instance_of",
                            "target": "#V#student",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response=drafted_claim),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Add and canonically verify both student relations.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=turn_id,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert good_effect_id in result.response_text
    assert failed_effect_id in result.response_text
    assert "Added; forward and inverse verified" in result.response_text
    assert result.response_text.index("### Unsuccessful or unresolved") < (
        result.response_text.index("### Model draft (non-authoritative)")
    )
    assert "source_id `#V#gael_gendron`" in result.response_text
    assert "predicate `is_an_instance_of`" in result.response_text
    assert "target `#V#student`" in result.response_text
    assert "status `not_started`" in result.response_text
    assert "reported no change" in result.response_text
    assert "error code `ontology_mutation_target_not_accessible`" in (
        result.response_text
    )
    rejection = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_cited_ontology_mutation_claim_rejected"
    )
    assert len(rejection["conflicts"]) == 1
    conflict = rejection["conflicts"][0]
    assert conflict["effect_id"] == failed_effect_id
    assert conflict["method"] == "add_relationship"
    assert conflict["effect_status"] == "not_started"
    assert conflict["changed"] is False
    assert conflict["reason"] == "effect_not_succeeded"
    assert conflict["error_code"] == "ontology_mutation_target_not_accessible"
    assert conflict["source_id"] == "#V#gael_gendron"
    assert conflict["predicate"] == "is_an_instance_of"
    assert conflict["target"] == "#V#student"
    assert conflict["canonical_relationship_present"] is None


def test_cited_ontology_success_without_relation_readback_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_id = "cited-unverified-ontology-effect"
    call_id = "add-unverified-student"
    effect_id = _effect_id(
        turn_id=turn_id,
        call_id=call_id,
        capability_name="add_relationship",
    )
    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        lambda **_kwargs: {"delegation_id": "delegation-test"},
    )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=call_id,
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#unverified_student",
                            "predicate": "is_an_instance_of",
                            "target": "#V#student",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "| #V#unverified_student | Added and canonically verified | "
                f"{effect_id} |"
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, arguments: {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "source_id": arguments["source_id"],
                "predicate": "is_an_instance_of",
                "target": "#V#student",
                "canonical_read_back": {
                    "source_id": arguments["source_id"],
                    "source_exists": True,
                    "predicate": "is_an_instance_of",
                    "target": "#V#student",
                    "relationship_present": False,
                    "inverse_predicate": "has_instance",
                    "inverse_relationship_present": False,
                },
            }
        ),
        prompt="Add and canonically verify the student relation.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=turn_id,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert "Added and canonically verified" in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text
    assert "handler reported `succeeded`" in result.response_text
    assert "canonical read-back did not verify the outcome" in (
        result.response_text
    )
    rejection = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_cited_ontology_mutation_claim_rejected"
    )
    assert rejection["conflicts"][0]["effect_id"] == effect_id
    assert rejection["conflicts"][0]["reason"] == "canonical_relation_not_verified"
    assert rejection["conflicts"][0]["canonical_relationship_present"] is False


def test_indeterminate_effect_stops_later_effect_but_allows_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    invoked: list[str] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.2,
        effect_admission_window_sec=0.05,
    )

    def invoke(method_name: str, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        invoked.append(method_name)
        if method_name == "create_concepts":
            return SimpleNamespace(
                payload={
                    "success": False,
                    "error_code": "tool_timeout_outcome_unknown",
                    "mutation_outcome": "unknown",
                    "outcome_finality": "terminal_for_turn",
                },
                execution_id="execution-indeterminate",
                timed_out=True,
                telemetry_metadata=lambda: {
                    "schema_version": "internal_mcp_transport.v1",
                    "outcome": "timed_out",
                    "timeout_phase": "handler",
                },
            )
        assert method_name == "general_read"
        return SimpleNamespace(
            payload={"success": True, "concept_id": "#V#created_if_present"},
            execution_id="execution-readback",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="indeterminate-create",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Uncertain"}]},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-after-indeterminate",
                    payload={
                        "name": "general_read",
                        "arguments": {"concept_id": "#V#created_if_present"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="blocked-later-effect",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": "#V#person",
                            "predicate": "#V#has_note",
                            "text": "must not run",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response="The first effect needs canonical inspection."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create, inspect, then update.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="stop-after-indeterminate-effect",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=1,
    )

    assert invoked == ["create_concepts", "general_read"]
    assert result.terminal_status == "effect_outcome_indeterminate"
    assert result.response_authority == "canonical_outcome"
    assert "indeterminate" in result.response_text
    assert "The first effect needs canonical inspection." in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert result.tool_invocations[1]["result_target_ids"] == ["#V#created_if_present"]
    blocked = result.tool_invocations[2]
    assert blocked["status"] == "error"
    assert blocked["effect_status"] == "not_started"
    assert blocked["changed"] is False
    assert blocked["mutation_outcome"] == "not_started"
    assert blocked["error_code"] == "prior_effect_outcome_indeterminate"


def test_exact_terminal_reconciliation_preserves_recovered_ontology_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    concept_id = "#V#nichola_raihani"
    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "reconcile_governed_ontology_postcondition",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "status": "verified",
            "method_name": "create_concepts",
            "receipt_id": "omr-nichola",
            "intent_fingerprint": "intent-nichola",
            "target_concept_ids": [concept_id],
            "canonical_read_back": {"concepts": [{"concept_id": concept_id}]},
        },
    )

    gateway = _effect_gateway(
        lambda name, _arguments: (
            {
                "success": False,
                "effect_status": "indeterminate",
                "mutation_outcome": "unknown",
                "outcome_finality": "requires_canonical_reconciliation",
                "changed": None,
                "error_code": "ontology_mutation_postcondition_failed",
                "created_concept_ids": [concept_id],
                "authority_receipt": {
                    "receipt_id": "omr-nichola",
                    "status": "indeterminate",
                    "intent_fingerprint": "intent-nichola",
                },
                "postcondition_reconciliation": {
                    "schema_version": (
                        "ontology_mutation_postcondition_reconciliation.v1"
                    )
                },
            }
            if name == "create_concepts"
            else {"success": True}
        )
    )
    answer = f"I represented Nichola Raihani as {concept_id}."
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-nichola",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [
                                {
                                    "concept_id": concept_id,
                                    "name": "Nichola Raihani",
                                }
                            ]
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response=answer),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Represent her comprehensively.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-reconcile-nichola",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    assert result.response_text == answer
    tool_messages = [
        item
        for item in client.calls[1]["context"]
        if item.get("role") == "tool"
    ]
    model_receipt = json.loads(tool_messages[-1]["content"])
    assert model_receipt["effect_status"] == "succeeded"
    assert model_receipt["reconciliation_status"] == "canonically_verified"
    assert model_receipt["result_target_ids"] == [concept_id]
    invocation = result.tool_invocations[0]
    assert invocation["effect_status"] == "succeeded"
    assert invocation["initial_effect_status"] == "indeterminate"
    assert invocation["reconciliation_status"] == "canonically_verified"
    assert invocation["canonical_readback"]["verified"] is True
    assert invocation["result_target_ids"] == [concept_id]


@pytest.mark.parametrize("marker_succeeds", [True, False])
def test_exact_current_readback_renders_truthful_paper_partial_outcome(
    monkeypatch: pytest.MonkeyPatch,
    marker_succeeds: bool,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    concept_id = "#V#paper_2512_23333"
    marker_id = "#V#paper_2512_23333_processing_marker"
    invoked: list[str] = []
    persisted: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "reconcile_governed_ontology_postcondition",
        lambda **_kwargs: {
            "success": False,
            "verified": False,
            "method_name": "create_concepts",
        },
    )

    from src.backend.services import turn_execution_record_service

    def persist_phase(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        return {
            "updated": True,
            "duplicate": False,
            "phase": kwargs.get("phase"),
            "stored_phase": dict(kwargs.get("observation") or {}),
        }

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        persist_phase,
    )

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        if name == "download_paper":
            return {
                "success": False,
                "effect_status": "failed",
                "changed": False,
                "error_code": "arxiv_acquisition_unavailable",
                "error": (
                    "RuntimeError: asyncio lock is bound to a different event loop"
                ),
            }
        if name == "create_concepts":
            return {
                "success": False,
                "effect_status": "indeterminate",
                "changed": None,
                "mutation_outcome": "unknown",
                "outcome_finality": "requires_canonical_reconciliation",
                "error_code": "ontology_mutation_postcondition_failed",
                "created_concept_ids": [concept_id],
                "postcondition_reconciliation": {
                    "schema_version": (
                        "ontology_mutation_postcondition_reconciliation.v1"
                    )
                },
            }
        if name == "record_source_processing_marker":
            if not marker_succeeds:
                return {
                    "success": False,
                    "effect_status": "failed",
                    "changed": False,
                    "error_code": "processing_marker_write_failed",
                }
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "marker_concept_id": marker_id,
            }
        raise AssertionError(name)

    gateway = _effect_gateway(handler)
    gateway._catalogue.register(
        MethodDefinition(
            name="download_paper",
            handler=lambda **kwargs: handler("download_paper", kwargs),
            input_schema=Schema(allow_unknown=True),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="write",
            ordinary_turn_effect=True,
        )
    )
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": True,
                "concept_id": concept_id,
                "publication_context": {
                    "kind": "user",
                    "concept_id": "#V#michael_witbrock",
                },
                "relationships": {
                    "#V#specific_to_user": ["#V#michael_witbrock"]
                },
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway._catalogue.register(
        MethodDefinition(
            name="record_source_processing_marker",
            handler=lambda **kwargs: handler(
                "record_source_processing_marker",
                kwargs,
            ),
            input_schema=Schema(allow_unknown=True),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="write",
            ordinary_turn_effect=True,
        )
    )
    for method_name in (
        "download_paper",
        "fetch_concept",
        "record_source_processing_marker",
    ):
        gateway.register_metrics_if_missing(method_name)

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="download-paper",
                    payload={
                        "name": "download_paper",
                        "arguments": {"arxiv_id": "2512.23333"},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-paper-concept",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [
                                {
                                    "concept_id": concept_id,
                                    "name": "A represented arXiv paper",
                                }
                            ]
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="fetch-paper-concept",
                    payload={
                        "name": "fetch_concept",
                        "arguments": {"concept_id": concept_id},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="mark-paper-processed",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": {
                            "source_system": "arxiv",
                            "source_item_id": "2512.23333",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The paper and PDF were represented in the University of "
                "Auckland Strong AI Lab organisation namespace."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Represent this arXiv paper and report what actually persisted.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#michael_witbrock@uoasail",
        user_concept_id="#V#michael_witbrock",
        org_concept_id="#V#uoasail",
        turn_id="paper-partial-current-state-readback",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == [
        "download_paper",
        "create_concepts",
        "record_source_processing_marker",
    ]
    assert len(client.calls) == 5
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert concept_id in result.response_text
    if marker_succeeds:
        assert marker_id in result.response_text
    else:
        assert marker_id not in result.response_text
        assert "processing_marker_write_failed" in result.response_text
    assert "arxiv_acquisition_unavailable" in result.response_text
    assert "asyncio lock is bound to a different event loop" in result.response_text
    assert "User scope `#V#michael_witbrock`" in result.response_text
    assert "organisation namespace" in result.response_text
    assert result.response_text.index("User scope `#V#michael_witbrock`") < (
        result.response_text.index("organisation namespace")
    )
    assert "### Model draft (non-authoritative)" in result.response_text
    create_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == "create_concepts"
    )
    assert create_invocation["effect_status"] == "indeterminate"
    assert create_invocation["initial_effect_status"] == "indeterminate"
    assert create_invocation["current_outcome_status"] == "target_observed"
    assert create_invocation["outcome_resolved"] is True
    current_state_phases = [
        item
        for item in persisted
        if item.get("phase") == "current_state_observation"
    ]
    assert len(current_state_phases) == 1
    assert current_state_phases[0]["observation"]["target_concept_ids"] == [
        concept_id
    ]
    outcome_report = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_effect_outcome_report"
    )
    assert outcome_report["response_authority"] == "canonical_outcome"
    assert outcome_report["model_draft"]["authority"] == "non_authoritative"
    assert outcome_report["model_draft"]["preview"].startswith(
        "The paper and PDF were represented"
    )


def test_one_exact_read_does_not_resolve_multi_target_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    target_ids = ["#V#multi_target_a", "#V#multi_target_b"]
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "reconcile_governed_ontology_postcondition",
        lambda **_kwargs: {"success": False, "verified": False},
    )

    from src.backend.services import turn_execution_record_service

    def persist_phase(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        return {"updated": True, "duplicate": False}

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        persist_phase,
    )

    gateway = _effect_gateway(
        lambda name, _arguments: (
            {
                "success": False,
                "effect_status": "indeterminate",
                "changed": None,
                "mutation_outcome": "unknown",
                "outcome_finality": "requires_canonical_reconciliation",
                "error_code": "ontology_mutation_postcondition_failed",
                "created_concept_ids": target_ids,
                "postcondition_reconciliation": {
                    "schema_version": (
                        "ontology_mutation_postcondition_reconciliation.v1"
                    )
                },
            }
            if name == "create_concepts"
            else {"success": True}
        )
    )
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": True,
                "concept_id": concept_id,
                "publication_context": {
                    "kind": "user",
                    "concept_id": "#V#user",
                },
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("fetch_concept")
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-multiple-targets",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [
                                {"concept_id": target_id, "name": target_id}
                                for target_id in target_ids
                            ]
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-only-one-target",
                    payload={
                        "name": "fetch_concept",
                        "arguments": {"concept_id": target_ids[0]},
                    },
                )
            ],
        ),
        LLMResponse(text_response="Both targets were created."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create both targets and verify them.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#user@org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        turn_id="turn-partial-multi-target-readback",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == "create_concepts"
    )
    assert result.terminal_status == "effect_outcome_indeterminate"
    assert invocation["effect_status"] == "indeterminate"
    assert "current_outcome_status" not in invocation
    assert not any(
        item.get("phase") == "current_state_observation" for item in persisted
    )


def test_unacknowledged_current_state_observation_does_not_resolve_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    concept_id = "#V#unacknowledged_current_state"
    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "reconcile_governed_ontology_postcondition",
        lambda **_kwargs: {"success": False, "verified": False},
    )

    from src.backend.services import turn_execution_record_service

    def persist_phase(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("phase") == "current_state_observation":
            return {
                "updated": False,
                "duplicate": False,
                "reason": "collection_unavailable",
            }
        return {"updated": True, "duplicate": False}

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        persist_phase,
    )
    gateway = _effect_gateway(
        lambda name, _arguments: (
            {
                "success": False,
                "effect_status": "indeterminate",
                "changed": None,
                "mutation_outcome": "unknown",
                "outcome_finality": "requires_canonical_reconciliation",
                "error_code": "ontology_mutation_postcondition_failed",
                "created_concept_ids": [concept_id],
                "postcondition_reconciliation": {
                    "schema_version": (
                        "ontology_mutation_postcondition_reconciliation.v1"
                    )
                },
            }
            if name == "create_concepts"
            else {"success": True}
        )
    )
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": True,
                "concept_id": concept_id,
                "publication_context": {
                    "kind": "user",
                    "concept_id": "#V#user",
                },
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("fetch_concept")
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-before-unacknowledged-read",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [
                                {"concept_id": concept_id, "name": "Unacknowledged"}
                            ]
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-current-without-journal-ack",
                    payload={
                        "name": "fetch_concept",
                        "arguments": {"concept_id": concept_id},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The concept exists."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create and inspect this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#user@org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        turn_id="turn-unacknowledged-current-state",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == "create_concepts"
    )
    assert result.terminal_status == "effect_outcome_indeterminate"
    assert invocation["effect_status"] == "indeterminate"
    assert "current_outcome_status" not in invocation
    failure = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "effect_observation_persistence_failure"
        and item.get("phase") == "current_state_observation"
    )
    assert failure["reason"] == "collection_unavailable"


def test_per_method_minimum_admits_sequential_effects_below_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        time.sleep(0.015)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="first-late-window-effect",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {"concept_id": "#V#person"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="second-late-window-effect",
                    payload={
                        "name": "add_relationship",
                        "arguments": {"source_id": "#V#person"},
                    },
                ),
            ],
        ),
        LLMResponse(text_response="Both bounded effects completed."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            write_timeout_sec=0.2,
            effect_admission_window_sec=0.01,
        ),
        prompt="Apply both bounded effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="sequential-effect-admission",
        turn_budget_seconds=0.2,
        final_synthesis_reserve_seconds=0.12,
    )

    assert invoked == ["upsert_text_relation", "add_relationship"]
    assert [item["effect_status"] for item in result.tool_invocations] == [
        "succeeded",
        "succeeded",
    ]


def test_effect_is_not_dispatched_when_durable_intent_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import turn_execution_record_service

    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        lambda **_kwargs: {
            "updated": False,
            "duplicate": False,
            "reason": "collection_unavailable",
        },
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="effect-without-durable-intent",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Must not be dispatched"}]},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The effect was not started because its durable intent could "
                "not be recorded."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-intent-persistence-failure",
    )

    assert invoked == []
    assert result.terminal_status == "effect_not_started"
    assert "status `not_started`" in result.response_text
    assert "effect_observation_unavailable" in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "not_started"
    assert result.tool_invocations[0]["changed"] is False
    preview = result.tool_invocations[0]["evidence"]["preview"]
    assert "effect_observation_unavailable" in preview
    assert "not_started" in preview


@pytest.mark.parametrize(
    "relationships",
    [
        {
            "#V#specific_to_user": ["#V#other_person"],
            "#V#specific_to_organisation": ["#V#other_org"],
        },
        {},
    ],
    ids=["foreign-scoped", "global"],
)
def test_ordinary_effect_rejects_unscoped_subject_before_handler(
    monkeypatch,
    relationships: dict[str, Any],
) -> None:
    from src.backend.db import mongo_client

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": relationships}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {"success": True}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="foreign-subject",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#foreign_subject",
                            "predicate": "#V#hasResearchInterest",
                            "target": "#V#topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The subject was outside delegated authority."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Update this represented concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="foreign-subject",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == []
    assert result.tool_invocations[0]["effect_status"] == "not_started"
    assert "ontology_mutation_target_not_accessible" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )


def test_canonical_text_denial_exposes_scoped_assertion_recovery(
    monkeypatch,
) -> None:
    from src.backend.db import mongo_client
    from src.backend.services import ontology_mutation_command_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    original_is_ontology_mutation_method = (
        ontology_mutation_command_service.is_ontology_mutation_method
    )
    monkeypatch.setattr(
        ontology_mutation_command_service,
        "is_ontology_mutation_method",
        lambda method: (
            False
            if method == "upsert_text_relation"
            else original_is_ontology_mutation_method(method)
        ),
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_recovered",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="canonical-text-denied",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": "#V#globally_visible_subject",
                            "predicate": "hasNote",
                            "text": "Actor-relative observation.",
                            "language": "en-NZ",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="scoped-text-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": ("#V#globally_visible_subject"),
                            "predicate": "hasNote",
                            "target_text": "Actor-relative observation.",
                            "language": "en-NZ",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The actor-scoped assertion was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            include_scoped_assertion=True,
        ),
        prompt="Record this observation without changing shared publication.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="canonical-text-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(invoked) == 1
    recovered_name, recovered_arguments = invoked[0]
    assert recovered_name == "upsert_scoped_assertion"
    assert recovered_arguments["acting_user_concept_id"] == "#V#person"
    assert recovered_arguments["organisation_concept_id"] == "#V#org"
    assert recovered_arguments["namespace"] == "#V#person@org"
    assert recovered_arguments["canonical_publication"] is False

    denial = result.tool_invocations[0]
    assert denial["effect_status"] == "failed"
    preview = denial["evidence"]["preview"]
    assert "effect_subject_not_authorised" in preview
    assert "assert_in_actor_scope" in preview
    assert "upsert_scoped_assertion" in preview
    assert "#V#globally_visible_subject" in preview
    assert "Actor-relative observation." in preview
    recovery = result.tool_invocations[1]
    assert recovery["effect_status"] == "succeeded"
    assert recovery["changed"] is True
    assert result.terminal_status == "completed"
    assert result.response_text == "The actor-scoped assertion was recorded."
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]


@pytest.mark.parametrize(
    "predicate_arguments",
    [
        {"predicate": "#V#hasResearchInterest"},
        {
            "predicate_ref": {
                "concept_id": "#V#hasResearchInterest",
                "value_kind": "concept",
            }
        },
    ],
)
def test_canonical_relationship_denial_recovers_as_scoped_assertion(
    monkeypatch: pytest.MonkeyPatch,
    predicate_arguments: dict[str, Any],
) -> None:
    _stub_proven_scoped_assertion_denial(monkeypatch)
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "concept",
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_relationship_recovered",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="canonical-relationship-denied",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#globally_visible_subject",
                            **predicate_arguments,
                            "target": "#V#represented_topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="scoped-relationship-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": ("#V#globally_visible_subject"),
                            "predicate": "#V#hasResearchInterest",
                            "target_concept_id": "#V#represented_topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The actor-scoped relationship was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            include_scoped_assertion=True,
        ),
        prompt=(
            "Record this represented relationship without changing shared "
            "publication."
        ),
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="canonical-relationship-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(invoked) == 1
    recovered_name, recovered_arguments = invoked[0]
    assert recovered_name == "upsert_scoped_assertion"
    assert recovered_arguments["subject_concept_id"] == ("#V#globally_visible_subject")
    assert recovered_arguments["predicate"] == "#V#hasResearchInterest"
    assert recovered_arguments["target_concept_id"] == "#V#represented_topic"
    assert recovered_arguments["acting_user_concept_id"] == "#V#person"
    assert recovered_arguments["organisation_concept_id"] == "#V#org"
    assert recovered_arguments["namespace"] == "#V#person@org"
    assert recovered_arguments["canonical_publication"] is False

    denial = result.tool_invocations[0]
    assert denial["effect_status"] == "not_started"
    preview = denial["evidence"]["preview"]
    assert "global_ontology_admin_authority_required" in preview
    assert "assert_in_actor_scope" in preview
    assert "#V#globally_visible_subject" in preview
    assert "#V#hasResearchInterest" in preview
    assert "#V#represented_topic" in preview
    recovery = result.tool_invocations[1]
    assert recovery["effect_status"] == "succeeded"
    assert recovery["changed"] is True
    assert result.terminal_status == "completed"
    assert result.response_text == ("The actor-scoped relationship was recorded.")
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]


def test_multi_person_recovery_preserves_successes_and_only_repairs_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_proven_scoped_assertion_denial(monkeypatch)
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "concept",
    )
    persisted_subject_ids: list[str] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "upsert_scoped_assertion"
        subject_id = arguments["subject_concept_id"]
        persisted_subject_ids.append(subject_id)
        assertion_id = f"ska_{subject_id.removeprefix('#V#')}"
        readback = {
            "schema_version": "scoped_knowledge_assertion.v1",
            "assertion_id": assertion_id,
            "subject_concept_id": subject_id,
            "predicate": arguments["predicate"],
            "object_kind": "concept",
            "object_concept_id": arguments["target_concept_id"],
            "object_text": None,
            "scope": {
                "mode": arguments["scope_mode"],
                "user_concept_id": arguments["acting_user_concept_id"],
                "organisation_concept_id": arguments[
                    "organisation_concept_id"
                ],
                "namespace": arguments["namespace"],
                "audience_keys": [
                    f"org:{arguments['organisation_concept_id']}"
                ],
            },
            "provenance": {
                "asserted_by_user_concept_id": arguments[
                    "acting_user_concept_id"
                ],
                "organisation_concept_id": arguments[
                    "organisation_concept_id"
                ],
                "namespace": arguments["namespace"],
            },
            "canonical_publication": False,
            "status": "asserted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": assertion_id,
            "assertion": readback,
            "canonical_read_back": readback,
            "canonical_publication": False,
        }

    relationship_arguments = {
        "source_id": "#V#person_b",
        "predicate": "#V#affiliated_with",
        "target": "#V#school_of_computer_science",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="persist-person-a",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#person_a",
                            "predicate": "#V#affiliated_with",
                            "target_concept_id": "#V#school_of_computer_science",
                            "scope_mode": "organisation",
                        },
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="deny-person-b-canonical",
                    payload={
                        "name": "add_relationship",
                        "arguments": relationship_arguments,
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="persist-person-c",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#person_c",
                            "predicate": "#V#affiliated_with",
                            "target_concept_id": "#V#school_of_computer_science",
                            "scope_mode": "organisation",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="repeat-person-b-terminal-denial",
                    payload={
                        "name": "add_relationship",
                        "arguments": relationship_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="recover-person-b-scoped",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#person_b",
                            "predicate": "#V#affiliated_with",
                            "target_concept_id": "#V#school_of_computer_science",
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "All three people now have read-back organisation-scoped "
                "affiliation assertions; only person B needed recovery."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Represent the same School affiliation for people A, B, and C.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-multi-person-partial-recovery",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert persisted_subject_ids == ["#V#person_a", "#V#person_c", "#V#person_b"]
    relationship_attempts = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == "add_relationship"
    ]
    assert len(relationship_attempts) == 2
    denial, repeated = relationship_attempts
    assert denial["effect_status"] == "not_started"
    assert denial["changed"] is False
    assert repeated["error_code"] == (
        "effect_request_unchanged_after_terminal_failure"
    )
    assert repeated["effect_status"] == "not_started"
    assert repeated["changed"] is False
    assert repeated["turn_finality_required"] is False
    recoveries = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == "upsert_scoped_assertion"
    ]
    assert len(recoveries) == 3
    assert all(invocation["effect_status"] == "succeeded" for invocation in recoveries)
    assert all(
        invocation.get("canonical_readback", {}).get("object_concept_id")
        == "#V#school_of_computer_science"
        for invocation in recoveries
    )
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recoveries[-1]["effect_id"]
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    assert result.response_text == (
        "All three people now have read-back organisation-scoped affiliation "
        "assertions; only person B needed recovery."
    )


@pytest.mark.parametrize(
    "predicate_arguments",
    [
        {"predicate": "#V#has_email"},
        {
            "predicate_ref": {
                "concept_id": "#V#has_email",
                "value_kind": "text",
            }
        },
    ],
)
def test_canonical_literal_relationship_denial_recovers_in_chosen_scope(
    monkeypatch: pytest.MonkeyPatch,
    predicate_arguments: dict[str, Any],
) -> None:
    _stub_proven_scoped_assertion_denial(monkeypatch)
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "text",
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_literal_relationship_recovered",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="canonical-literal-relationship-denied",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#globally_visible_subject",
                            **predicate_arguments,
                            "target": "Actor-relative observation.",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="scoped-literal-relationship-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": ("#V#globally_visible_subject"),
                            "predicate": "#V#has_email",
                            "target_text": "Actor-relative observation.",
                            "language": "en-NZ",
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The scoped observation was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Record this observation without changing shared publication.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="canonical-literal-relationship-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(invoked) == 1
    recovered_name, recovered_arguments = invoked[0]
    assert recovered_name == "upsert_scoped_assertion"
    assert recovered_arguments["scope_mode"] == "organisation"
    assert recovered_arguments["target_text"] == "Actor-relative observation."
    denial, recovery = result.tool_invocations
    assert denial["effect_status"] == "not_started"
    assert "target_text" in denial["evidence"]["preview"]
    assert recovery["effect_status"] == "succeeded"
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]
    assert result.terminal_status == "completed"
    assert result.response_text == "The scoped observation was recorded."


@pytest.mark.parametrize(
    "recovery_arguments",
    [
        {
            "subject_concept_id": "#V#different_subject",
            "predicate": "#V#has_email",
            "target_text": "Actor-relative observation.",
        },
        {
            "subject_concept_id": "#V#globally_visible_subject",
            "predicate": "hasDescription",
            "target_text": "Actor-relative observation.",
        },
        {
            "subject_concept_id": "#V#globally_visible_subject",
            "predicate": "#V#has_email",
            "target_text": "Different observation.",
        },
        {
            "subject_concept_id": "#V#globally_visible_subject",
            "predicate": "#V#has_email",
            "target_concept_id": "#V#actor_relative_observation",
        },
    ],
    ids=["subject", "predicate", "value", "target-kind"],
)
def test_canonical_literal_relationship_recovery_requires_same_object(
    monkeypatch: pytest.MonkeyPatch,
    recovery_arguments: dict[str, Any],
) -> None:
    _stub_proven_scoped_assertion_denial(monkeypatch)
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "text",
    )

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_unrelated_recovery",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="literal-relationship-denied",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#globally_visible_subject",
                            "predicate": "#V#has_email",
                            "target": "Actor-relative observation.",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="unrelated-scoped-assertion",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            **recovery_arguments,
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The scoped assertion was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Record this observation without changing shared publication.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="literal-relationship-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    denial = result.tool_invocations[0]
    unrelated_recovery = result.tool_invocations[1]
    assert denial["effect_status"] == "not_started"
    assert denial["recovery_status"] == "mismatched"
    assert denial["attempted_recovery_effect_id"] == unrelated_recovery["effect_id"]
    assert "recovered_by_effect_id" not in denial
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_text != "The scoped assertion was recorded."


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "source_id": "#V#subject",
            "predicate": "hasResearchInterest",
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate_ref": {"name": "hasResearchInterest"},
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate": "#V#hasResearchInterest",
            "predicate_ref": {"concept_id": "#V#otherPredicate"},
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate_ref": {
                "concept_id": "#V#hasResearchInterest",
                "on_missing": "create_typed_predicate",
            },
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate": "#V#hasResearchInterest",
            "predicate_if_missing": {"name": "hasResearchInterest"},
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate_ref": {
                "concept_id": "#V#hasResearchInterest",
                "value_kind": "concept",
            },
            "target": "Natural-language target",
        },
        {
            "source_id": "#V#subject",
            "predicate": "#V#hasResearchInterest",
            "target": "#V#malformed target",
        },
    ],
)
def test_non_exact_relationship_denial_has_no_scoped_recovery(
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import relationship_write_service

    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "concept",
    )
    denial = _effect_subject_authority_denial(
        capability_name="add_relationship",
        arguments=arguments,
        scoped_assertion_available=True,
    )

    assert denial["error_code"] == "effect_subject_not_authorised"
    assert "recovery_affordances" not in denial


@pytest.mark.parametrize(
    ("arguments", "canonical_kind", "expected_target_field"),
    [
        (
            {
                "source_id": "#V#subject",
                "predicate": "#V#is_an_instance_of",
                "target": "Professor",
            },
            "concept",
            None,
        ),
        (
            {
                "source_id": "#V#subject",
                "predicate_ref": {
                    "concept_id": "#V#has_email",
                    "value_kind": "concept",
                },
                "target": "#V#student@example.invalid",
            },
            "text",
            "target_text",
        ),
        (
            {
                "source_id": "#V#subject",
                "predicate": "#V#hasResearchInterest",
                "target": "#v#topic",
            },
            "concept",
            None,
        ),
        (
            {
                "source_id": "#V#subject",
                "predicate": "is_an_instance_of",
                "target": "#V#sail_phd_alumni",
            },
            "concept",
            "target_concept_id",
        ),
    ],
    ids=[
        "concept-literal",
        "text-represented-looking-literal",
        "lowercase-id",
        "structural-storage-alias",
    ],
)
def test_relationship_recovery_uses_represented_predicate_kind(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    canonical_kind: str,
    expected_target_field: str | None,
) -> None:
    from src.backend.services import (
        concept_predicate_metadata_service,
        relationship_write_service,
    )

    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: canonical_kind,
    )
    monkeypatch.setattr(
        concept_predicate_metadata_service,
        "get_structural_predicate_aliases",
        lambda: {"#V#is_an_instance_of": "is_an_instance_of"},
    )

    denial = _effect_subject_authority_denial(
        capability_name="add_relationship",
        arguments=arguments,
        scoped_assertion_available=True,
    )

    affordances = denial.get("recovery_affordances") or []
    if expected_target_field is None:
        assert affordances == []
    else:
        assert len(affordances) == 1
        recovery_arguments = affordances[0]["arguments"]
        assert recovery_arguments[expected_target_field] == arguments["target"]
        unexpected_target_field = (
            "target_text"
            if expected_target_field == "target_concept_id"
            else "target_concept_id"
        )
        assert unexpected_target_field not in recovery_arguments
        if arguments.get("predicate") == "is_an_instance_of":
            assert recovery_arguments["predicate"] == "#V#is_an_instance_of"


def test_existing_predicate_value_kind_comes_from_canonical_typing() -> None:
    from src.backend.services.relationship_write_service import (
        resolve_existing_predicate_value_kind,
    )

    class _PredicateRepo:
        @staticmethod
        def find_one(query: dict[str, Any], *_args: Any) -> dict[str, Any] | None:
            concept_id = query.get("concept_id")
            if concept_id == "#V#has_email":
                return {
                    "concept_id": concept_id,
                    "relationships": {
                        "is_an_instance_of": ["#V#binary_text_predicate"]
                    },
                }
            if concept_id == "#V#hasResearchInterest":
                return {
                    "concept_id": concept_id,
                    "relationships": {"is_an_instance_of": ["#V#predicate"]},
                }
            return None

    assert (
        resolve_existing_predicate_value_kind("#V#has_email", _PredicateRepo) == "text"
    )
    assert (
        resolve_existing_predicate_value_kind(
            "#V#hasResearchInterest",
            _PredicateRepo,
        )
        == "concept"
    )
    assert (
        resolve_existing_predicate_value_kind("#V#hasDescription", _PredicateRepo)
        == "text"
    )
    assert (
        resolve_existing_predicate_value_kind("#V#is_an_instance_of", _PredicateRepo)
        == "concept"
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "specific_to_user",
        "#V#specific_to_user",
        "specific_to_org",
        "specific_to_organisation",
        "#V#specific_to_org",
        "#V#specific_to_organisation",
    ],
)
def test_ordinary_relationship_effect_cannot_widen_visibility(
    monkeypatch,
    predicate: str,
) -> None:
    from src.backend.services import adaptive_turn_service

    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {"success": True}

    monkeypatch.setattr(
        adaptive_turn_service,
        "_effect_subject_authorised",
        lambda *_args: True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=f"visibility-{predicate}",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#private_subject",
                            "predicate": predicate,
                            "target": "#V#other_actor",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="Visibility was not changed."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Share this represented concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"visibility-{predicate}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == []
    assert result.tool_invocations[0]["effect_status"] == "failed"
    assert "visibility_effect_not_delegated" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "#V#has_authorised_mail_profile",
        "#V#has_default_mail_profile",
        "#V#has_runtime_profile_alias",
        "#V#has_oauth_scope",
        "#V#mail_profile_represents_identity",
    ],
)
def test_ordinary_relationship_effect_reserves_mail_profile_control_predicates(
    predicate: str,
) -> None:
    denial = _ordinary_effect_argument_denial(
        "add_relationship",
        {
            "source_id": "#V#person",
            "predicate_ref": {"concept_id": predicate},
            "target": "#V#gmail_profile_vonwitbrock_gmail",
        },
    )

    assert denial is not None
    assert denial["error_code"] == "mail_profile_authority_effect_not_delegated"


def test_ordinary_actor_cannot_self_grant_mail_profile_but_safe_relation_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = _stub_same_turn_ontology_delegation(monkeypatch)
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, dict(arguments)))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="self-grant-mail-profile",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#person",
                            "predicate": "#V#has_authorised_mail_profile",
                            "target": "#V#gmail_profile_vonwitbrock_gmail",
                        },
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="safe-research-interest",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#person",
                            "predicate": "#V#hasResearchInterest",
                            "target": "#V#knowledge_representation",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response="The safe relationship was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Authorise that Gmail profile and record my research interest.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="mail-profile-self-grant",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == [
        (
            "add_relationship",
            {
                "source_id": "#V#person",
                "predicate": "#V#hasResearchInterest",
                "target": "#V#knowledge_representation",
            },
        )
    ]
    assert [item["method_name"] for item in issued] == ["add_relationship"]
    assert result.tool_invocations[0]["effect_status"] == "failed"
    assert "mail_profile_authority_effect_not_delegated" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )
    assert result.tool_invocations[1]["effect_status"] == "succeeded"


def test_capability_query_ranks_without_eliminating_the_delegated_set(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    catalogue = MethodCatalogue()
    for name, description, bound_arguments in (
        (
            "internal_record_search",
            "Search Von internal records.",
            {"actor_id": "actor"},
        ),
        (
            "public_web_search",
            "Search current public web sources.",
            None,
        ),
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    optional={
                        "query": str,
                        "actor_id": (str, type(None)),
                    },
                    allow_unknown=False,
                ),
                category="read",
                description=description,
                ordinary_turn_trusted_argument_bindings=bound_arguments,
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda name: ToolDispatchSurfaceMetadata(
            surface_family=(
                "web" if name == "public_web_search" else "represented_records"
            ),
            evidence_surface_family=(
                "web" if name == "public_web_search" else "represented_records"
            ),
            external_surface=name == "public_web_search",
        ),
    )

    unmatched_query = _capability_catalogue(
        gateway,
        ("internal_record_search", "public_web_search"),
        {"query": "unrepresented vocabulary"},
    )
    assert unmatched_query["total"] == 2
    assert unmatched_query["delegated_total"] == 2
    assert unmatched_query["matched_total"] == 0
    assert unmatched_query["catalogue_scope"] == "complete_delegated_capability_set"
    assert [item["name"] for item in unmatched_query["capabilities"]] == [
        "internal_record_search",
        "public_web_search",
    ]
    assert all(item["query_match"] is False for item in unmatched_query["capabilities"])

    web_query = _capability_catalogue(
        gateway,
        ("internal_record_search", "public_web_search"),
        {"query": "web"},
    )
    assert web_query["matched_total"] == 1
    assert [item["name"] for item in web_query["capabilities"]] == [
        "public_web_search",
        "internal_record_search",
    ]
    assert all(
        set(item) == {"name", "query_match", "selection"}
        for item in web_query["capabilities"]
    )
    exact = _capability_catalogue(
        gateway,
        ("internal_record_search", "public_web_search"),
        {"names": ["internal_record_search", "public_web_search"]},
    )
    exact_by_name = {item["name"]: item for item in exact["capabilities"]}
    public_web = exact_by_name["public_web_search"]
    internal = exact_by_name["internal_record_search"]
    assert public_web["surface_family"] == "web"
    assert public_web["external_surface"] is True
    assert internal["surface_family"] == "represented_records"
    assert internal["external_surface"] is False
    assert internal["server_bound_arguments"] == ["actor_id"]
    assert "actor_id" not in internal["input_schema"]["properties"]


def test_complete_purpose_index_keeps_zero_overlap_direct_tool_visible(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    target_name = "find_relations_with_argument"
    target_description = (
        "Return represented relations containing a supplied entity at a selected "
        "argument position, with bounded predicate, certainty, direction, preview, "
        "and pagination controls for direct evidence reads. A second sentence adds "
        "detail that the compact purpose index must not repeat."
    )
    families = (
        "knowledge_lookup",
        "mail_search",
        "project_read",
        "record_fetch",
        "task_query",
        "workflow_status",
        "web_research",
        "zotero_search",
    )
    filler_names = [
        f"{family}_{index:02d}" for family in families for index in range(12)
    ][:95]
    capability_names = (target_name, *filler_names)
    catalogue = MethodCatalogue()
    for name in capability_names:
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    required={"query": str},
                    optional={f"bounded_field_{index}": str for index in range(8)},
                    allow_unknown=False,
                ),
                category="read",
                description=(
                    target_description
                    if name == target_name
                    else (
                        "Inspect a bounded source and return provenance-bearing "
                        "records for the requested research operation."
                    )
                ),
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda name, *, fallback_description=None: (
            target_description if name == target_name else fallback_description
        ),
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflows = tuple(
        WorkflowTurnCapability(
            name=f"represented_workflow_academic_profile_{index:02d}",
            workflow_id=f"#V#academic_profile_workflow_{index:02d}",
            display_name=f"Academic profile workflow {index:02d}",
            description=(
                "Compile and verify a multi-source academic mentorship dossier."
                if index == 0
                else "Compile and verify a bounded multi-source research dossier."
            ),
            relevance_score=0.97 - (index * 0.01),
            input_schema={"type": "object", "properties": {}},
            declared_step_count=5,
            semantic_effect=False,
            semantic_effect_source="declared_registered_read_components",
        )
        for index in range(10)
    )

    raw = _capability_catalogue(
        gateway,
        capability_names,
        {"query": "Which scholar mentors Michael?", "limit": 50},
        workflow_capabilities=workflows,
    )
    assert len(_json_bytes(raw)) <= 24_000
    assert len(raw["capabilities"]) == 50
    assert all(
        "description" not in item and "input_schema" not in item
        for item in raw["capabilities"]
    )

    target_detail = next(
        item for item in raw["capabilities"] if item["name"] == target_name
    )
    assert target_detail["query_match"] is False
    purpose_index = raw["purpose_index"]
    entries = purpose_index["entries"]
    assert purpose_index["complete"] is True
    assert purpose_index["ordering"] == "name_ascending_unranked"
    assert purpose_index["purpose_projection"] == "first_authored_sentence"
    assert len(entries) == len(capability_names) + len(workflows)
    assert [entry["name"] for entry in entries] == sorted(
        (*capability_names, *(workflow.name for workflow in workflows)),
        key=str.lower,
    )
    target_purpose = next(entry for entry in entries if entry["name"] == target_name)
    assert target_purpose["purpose"].endswith("...")
    assert len(target_purpose["purpose"]) <= 160
    assert "second sentence" not in target_purpose["purpose"].lower()
    assert set(target_purpose) == {"name", "purpose"}
    workflow_purpose = next(
        entry for entry in entries if entry["name"] == workflows[0].name
    )
    assert workflow_purpose["shape"] == "represented_workflow"
    assert "query_match" not in workflow_purpose
    assert "selection" not in workflow_purpose

    non_english = _capability_catalogue(
        gateway,
        capability_names,
        {"query": "Qui Michel encadre-t-il ?", "limit": 50},
        workflow_capabilities=workflows,
    )
    non_english_target = next(
        item for item in non_english["capabilities"] if item["name"] == target_name
    )
    assert non_english_target["query_match"] is False
    assert non_english["purpose_index"] == purpose_index

    bounded_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="realistic-purpose-index",
                tool_name="turn_capabilities",
                output=raw,
                status="ok",
            )
        ]
    )
    assert bounded_results is not None
    bounded = bounded_results[0].output
    bounded_size = len(_json_bytes(bounded))
    assert len(_json_bytes(purpose_index)) < 24_000
    assert bounded_size <= 24_000
    assert bounded["purpose_index"] == purpose_index

    exact = _capability_catalogue(
        gateway,
        capability_names,
        {"names": [target_name], "limit": 1},
        workflow_capabilities=workflows,
    )
    assert "purpose_index" not in exact
    assert exact["capabilities"][0]["name"] == target_name
    assert set(exact["capabilities"][0]["input_schema"]["properties"]) == {
        "query",
        *(f"bounded_field_{index}" for index in range(8)),
    }


def test_schema_discovery_metadata_is_retrievable_without_list_word_trigger(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="vontology_concept_search",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                required={"query": str},
                optional={"filter_kind": list},
                allow_unknown=False,
            ),
            category="read",
            description="Namespaced concept-search alias.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(tool_metadata_service, "_load_from_vontology", dict)
    tool_metadata_service.invalidate_cache()
    try:
        positive = _capability_catalogue(
            gateway,
            ("vontology_concept_search",),
            {"query": "schema discovery for represented relationships"},
        )
        negative = _capability_catalogue(
            gateway,
            ("vontology_concept_search",),
            {"query": "list more concise alternatives"},
        )

        capability = positive["capabilities"][0]
        assert capability["query_match"] is True
        exact = _capability_catalogue(
            gateway,
            ("vontology_concept_search",),
            {"names": ["vontology_concept_search"]},
        )
        planner_hint = exact["capabilities"][0]["planner_hint"].lower()
        assert "unknown" in planner_hint
        assert "relation-bearing read" in planner_hint
        assert "what is possible, not what is actually used" in planner_hint
        assert negative["capabilities"][0]["query_match"] is False
    finally:
        tool_metadata_service.invalidate_cache()


def test_possible_duplicate_review_intent_discovers_uncertain_assertion_lifecycle(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.services import tool_metadata_service

    catalogue = build_default_catalogue()
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(tool_metadata_service, "_load_from_vontology", dict)
    tool_metadata_service.invalidate_cache()
    try:
        delegated = ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#ordinary_actor",
        )
        assert "list_uncertain_relationship_assertions" in delegated
        assert "upsert_uncertain_relationship_assertion" in delegated

        discovered = _capability_catalogue(
            gateway,
            delegated,
            {"query": "mark as a possible duplicate for later review", "limit": 50},
        )
        discovered_by_name = {
            item["name"]: item for item in discovered["capabilities"]
        }
        assert discovered_by_name[
            "upsert_uncertain_relationship_assertion"
        ]["query_match"] is True
        assert discovered_by_name[
            "list_uncertain_relationship_assertions"
        ]["query_match"] is True

        purpose_by_name = {
            item["name"]: item["purpose"]
            for item in discovered["purpose_index"]["entries"]
        }
        assert "possible duplicate" in purpose_by_name[
            "upsert_uncertain_relationship_assertion"
        ]
        assert "later review" in purpose_by_name[
            "list_uncertain_relationship_assertions"
        ]

        exact = _capability_catalogue(
            gateway,
            delegated,
            {
                "names": [
                    "list_uncertain_relationship_assertions",
                    "upsert_uncertain_relationship_assertion",
                ]
            },
        )
        exact_by_name = {
            item["name"]: item for item in exact["capabilities"]
        }
        upsert = exact_by_name["upsert_uncertain_relationship_assertion"]
        listed = exact_by_name["list_uncertain_relationship_assertions"]
        assert upsert["semantic_effect"] is True
        assert upsert["plan_profile"]["shape"] == "single_capability"
        assert "reuse an existing represented predicate" in upsert[
            "planner_hint"
        ].lower()
        assert "do not mint a predicate" in upsert["planner_hint"].lower()
        assert "upsert_uncertain_relationship_assertion" in listed[
            "planner_hint"
        ]
    finally:
        tool_metadata_service.invalidate_cache()


def test_capability_metadata_failure_does_not_remove_delegated_reads(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service

    gateway = _gateway(lambda **_kwargs: {"success": True})

    def fail_metadata(*_args, **_kwargs):
        raise RuntimeError("represented metadata unavailable")

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        fail_metadata,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        fail_metadata,
    )

    result = _capability_catalogue(
        gateway,
        ("general_read",),
        {"query": "unmatched vocabulary"},
    )

    assert result["total"] == 1
    assert result["matched_total"] == 0
    assert result["capabilities"][0]["name"] == "general_read"
    assert result["purpose_index"]["entries"][0]["purpose"] == (
        "Read arbitrary general evidence."
    )
    exact = _capability_catalogue(
        gateway,
        ("general_read",),
        {"names": ["general_read"]},
    )
    assert exact["capabilities"][0]["description"] == (
        "Read arbitrary general evidence."
    )


def test_capability_frontier_promotes_direct_components_without_hiding_workflow(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    for name in (
        "fetch_concept",
        "find_relations_with_argument",
        "students_index",
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    optional={"concept_id": str},
                    allow_unknown=False,
                ),
                category="read",
                description=f"Use {name}.",
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_entity_lookup",
        workflow_id="#V#entity_lookup_workflow",
        display_name="Entity lookup workflow",
        description="Retrieve represented relationships for an entity.",
        relevance_score=0.93,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=(
            "fetch_concept",
            "find_relations_with_argument",
        ),
        declared_component_count=2,
        declared_step_count=4,
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        ("fetch_concept", "find_relations_with_argument", "students_index"),
        {"query": "students supervised by me", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert result["ranking"] == "minimum_adequate_cost_sensitive_frontier"
    assert result["selection_policy"]["representedness_priority"] is False
    assert [item["name"] for item in result["capabilities"]] == [
        "fetch_concept",
        "represented_workflow_entity_lookup",
        "find_relations_with_argument",
        "students_index",
    ]
    direct_reference = result["capabilities"][0]
    exact = _capability_catalogue(
        gateway,
        ("fetch_concept", "find_relations_with_argument", "students_index"),
        {
            "names": [
                "fetch_concept",
                "represented_workflow_entity_lookup",
            ]
        },
        workflow_capabilities=(workflow,),
    )
    exact_by_name = {item["name"]: item for item in exact["capabilities"]}
    direct = exact_by_name["fetch_concept"]
    represented = exact_by_name["represented_workflow_entity_lookup"]
    assert direct["plan_profile"]["shape"] == "single_capability"
    assert represented["plan_profile"]["shape"] == "represented_workflow"
    assert represented["semantic_effect"] is False
    assert represented["effect_profile"]["operational_state_effect"] is True
    assert "declared_component_of_matched_workflow" in (
        direct_reference["selection"]["adequacy_sources"]
    )
    assert result["frontier_total"] == 4
    assert result["dominated_total"] == 0


def test_workflow_components_remain_visible_ahead_of_unrelated_tool_matches(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    component_names = (
        "fetch_concept",
        "find_relations_with_argument",
        "get_predicate_incidence",
    )
    catalogue = MethodCatalogue()
    for name in (*component_names, "workflow_list_instances"):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(optional={}, allow_unknown=True),
                category="read",
                description=f"Use {name}.",
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_entity_lookup",
        workflow_id="#V#entity_lookup_workflow",
        display_name="Entity lookup workflow",
        description="Retrieve represented relationships for an entity.",
        relevance_score=0.96,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=component_names,
        declared_component_count=3,
        declared_step_count=3,
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        (*component_names, "workflow_list_instances"),
        {"query": "who is supervised by this person", "limit": 4},
        workflow_capabilities=(workflow,),
    )

    assert [item["name"] for item in result["capabilities"]] == [
        "fetch_concept",
        "represented_workflow_entity_lookup",
        "find_relations_with_argument",
        "get_predicate_incidence",
    ]
    assert all(
        "declared_component_of_matched_workflow"
        in item["selection"]["adequacy_sources"]
        for item in (
            result["capabilities"][0],
            result["capabilities"][2],
            result["capabilities"][3],
        )
    )


def test_descriptions_put_relevant_direct_plan_in_visible_frontier(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    descriptions = {
        "generic_read": "Inspect an unrelated operational record.",
        "search_concepts": (
            "Search represented concept names, predicates, and types for "
            "relationship schema discovery."
        ),
        "find_relations_with_argument": (
            "Find represented relationships, including people supervised by "
            "a concept, where it appears as subject or target."
        ),
    }
    for name, description in descriptions.items():
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(optional={"query": str}, allow_unknown=True),
                category="read",
                description=description,
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_entity_lookup",
        workflow_id="#V#entity_lookup_workflow",
        display_name="Entity lookup workflow",
        description="Retrieve represented relationships for an entity.",
        relevance_score=0.96,
        input_schema={"type": "object", "properties": {}},
        declared_component_count=2,
        unresolved_component_count=2,
        declared_step_count=3,
        semantic_effect=None,
    )

    result = _capability_catalogue(
        gateway,
        ("generic_read", "search_concepts", "find_relations_with_argument"),
        {"query": "who is supervised by this person", "limit": 6},
        workflow_capabilities=(workflow,),
    )

    capability_names = [item["name"] for item in result["capabilities"]]
    assert capability_names[:2] == [
        "find_relations_with_argument",
        "represented_workflow_entity_lookup",
    ]
    assert set(capability_names[2:]) == {"search_concepts", "generic_read"}
    exact = _capability_catalogue(
        gateway,
        ("generic_read", "search_concepts", "find_relations_with_argument"),
        {"names": ["find_relations_with_argument"]},
        workflow_capabilities=(workflow,),
    )
    direct_candidate = exact["capabilities"][0]
    assert direct_candidate["plan_profile"]["cost_profile"]["durable_runtime"] is False
    assert result["selection_policy"]["semantic_adequacy_owner"] == "adaptive_model"


def test_capability_frontier_does_not_prefer_unrelated_direct_tool(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="unrelated_direct_read",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"record_id": str}, allow_unknown=False),
            category="read",
            description=(
                "Inspect an unrelated verified record. The shared adjective is "
                "incidental rather than routing evidence."
            ),
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_multi_step",
        workflow_id="#V#multi_step_workflow",
        display_name="Multi-step workflow",
        description="Produce a verified multi-step research work product.",
        relevance_score=0.95,
        input_schema={"type": "object", "properties": {}},
        declared_step_count=6,
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        ("unrelated_direct_read",),
        {"query": "verified multi-step research work product", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert [item["name"] for item in result["capabilities"]] == [
        "represented_workflow_multi_step",
        "unrelated_direct_read",
    ]
    assert result["frontier_total"] == 1


def test_capability_frontier_uses_catalogue_rarity_not_generic_routing_words(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service

    catalogue = MethodCatalogue()
    generic_names = tuple(f"list_inventory_{index}" for index in range(5))
    for name in (*generic_names, "students_relations"):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(optional={}, allow_unknown=False),
                category="read",
                description="Inspect a bounded represented record.",
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )

    result = _capability_catalogue(
        gateway,
        (*generic_names, "students_relations"),
        {"query": "list students", "limit": 10},
    )

    assert result["frontier_total"] == 1
    assert result["matched_total"] == 1
    assert result["capabilities"][0]["name"] == "students_relations"
    by_name = {item["name"]: item for item in result["capabilities"]}
    assert all(
        by_name[name]["selection"]["frontier_status"] == "outside_query_frontier"
        for name in generic_names
    )
    assert by_name[generic_names[0]]["selection"]["adequacy_sources"] == [
        "literal_query_terms"
    ]


def test_declared_read_only_direct_equivalence_prunes_only_frontier(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="direct_lookup",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"query": str}, allow_unknown=False),
            category="read",
            description="Look up the requested record.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_degenerate_lookup",
        workflow_id="#V#degenerate_lookup_workflow",
        display_name="Degenerate lookup workflow",
        description="Look up the requested record through a durable workflow.",
        relevance_score=0.9,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=("direct_lookup",),
        declared_component_count=1,
        declared_step_count=1,
        direct_equivalent_capability_names=("direct_lookup",),
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        ("direct_lookup",),
        {"query": "look up the requested record", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert [item["name"] for item in result["capabilities"]] == [
        "direct_lookup",
        "represented_workflow_degenerate_lookup",
    ]
    assert result["total"] == 2
    assert result["frontier_total"] == 1
    assert result["dominated_total"] == 1
    assert result["dominated_capabilities"] == [
        {
            "name": "represented_workflow_degenerate_lookup",
            "dominated_by": "direct_lookup",
            "reason": (
                "declared_direct_equivalence_with_lower_declared_orchestration_cost"
            ),
            "equivalence_source": "represented_workflow_routing_profile",
        }
    ]
    assert result["capabilities"][1]["selection"]["frontier_status"] == (
        "declared_dominated"
    )


def test_declared_direct_equivalence_does_not_prune_effectful_workflow(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="direct_lookup",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"query": str}, allow_unknown=False),
            category="read",
            description="Look up the requested record.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(tool_metadata_service, "get_tool_planner_hint", lambda _: None)
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_effectful_lookup",
        workflow_id="#V#effectful_lookup_workflow",
        display_name="Effectful lookup workflow",
        description="Look up the record and publish a represented result.",
        relevance_score=0.9,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=("direct_lookup",),
        declared_component_count=1,
        declared_step_count=2,
        direct_equivalent_capability_names=("direct_lookup",),
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
    )

    result = _capability_catalogue(
        gateway,
        ("direct_lookup",),
        {"query": "look up and publish the requested record", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert result["dominated_total"] == 0
    assert result["dominated_capabilities"] == []
    assert {item["name"] for item in result["capabilities"]} == {
        "direct_lookup",
        "represented_workflow_effectful_lookup",
    }
    purpose = next(
        item
        for item in result["purpose_index"]["entries"]
        if item["name"] == "represented_workflow_effectful_lookup"
    )
    assert purpose["semantic_effect"] is True


def test_capability_page_budget_preserves_every_alternative_and_cursor(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: ToolDispatchSurfaceMetadata(
            surface_family="research",
            evidence_surface_family="research",
            external_surface=False,
        ),
    )
    catalogue = MethodCatalogue()
    capability_names = [f"research_search_{index:02d}" for index in range(20)]
    for index, name in enumerate(capability_names):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    optional={
                        "actor_id": (str, type(None)),
                        **{f"field_{field}_{index}": str for field in range(30)},
                    },
                    allow_unknown=False,
                ),
                category="read",
                description=("Search research material. " + ("description " * 30)),
                ordinary_turn_trusted_argument_bindings={
                    "actor_id": "actor",
                },
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    first_raw_page = _capability_catalogue(
        gateway,
        capability_names,
        {"query": "research", "offset": 0, "limit": 20},
    )
    assert len(_json_bytes(first_raw_page)) <= 24_000
    assert len(first_raw_page["capabilities"]) == 20

    paired = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id=f"catalogue-{index}",
                tool_name="turn_capabilities",
                output=first_raw_page,
                status="ok",
            )
            for index in range(2)
        ]
    )
    assert paired is not None
    assert all(item.output.get("capabilities") for item in paired)
    assert all(item.output["next_offset"] is None for item in paired)
    assert all(len(item.output["capabilities"]) == 20 for item in paired)
    assert (
        len(
            _json_bytes(
                [
                    {
                        "call_id": item.call_id,
                        "tool_name": item.tool_name,
                        "status": item.status,
                        "output": item.output,
                    }
                    for item in paired
                ]
            )
        )
        <= 24_000
    )

    seen_names: list[str] = []
    seen_entries: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        raw_page = _capability_catalogue(
            gateway,
            capability_names,
            {"query": "research", "offset": offset, "limit": 20},
        )
        bounded_results = _bound_tool_results_for_model(
            [
                ToolResult(
                    call_id=f"catalogue-page-{offset}",
                    tool_name="turn_capabilities",
                    output=raw_page,
                    status="ok",
                )
            ]
        )
        assert bounded_results is not None
        bounded = bounded_results[0].output
        assert len(_json_bytes(bounded)) <= 24_000
        entries = bounded["capabilities"]
        assert entries
        assert [item["name"] for item in entries] == capability_names[
            offset : offset + len(entries)
        ]
        seen_names.extend(item["name"] for item in entries)
        seen_entries.extend(entries)
        next_offset = bounded["next_offset"]
        if next_offset is not None:
            assert next_offset == offset + len(entries)
        offset = next_offset

    assert seen_names == capability_names
    reference = seen_entries[0]
    assert reference["query_match"] is True
    assert reference["selection"]["frontier_status"] == "candidate"
    assert "description" not in reference
    assert "input_schema" not in reference
    purpose_entry = next(
        item
        for item in bounded["purpose_index"]["entries"]
        if item["name"] == reference["name"]
    )
    assert purpose_entry["purpose"].startswith("Search research material.")
    assert purpose_entry["purpose"].endswith("...")
    assert len(purpose_entry["purpose"]) <= 160
    assert bounded["purpose_index"]["exact_schema_hydration"] == {
        "tool": "turn_capabilities",
        "guidance": "Request all alternatives being compared in one names array.",
        "arguments": {"names": ["<capability names>"], "limit": 50},
    }

    exact_page = _capability_catalogue(
        gateway,
        capability_names,
        {"names": [reference["name"]], "limit": 1},
    )
    exact_bounded = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="exact-schema",
                tool_name="turn_capabilities",
                output=exact_page,
                status="ok",
            )
        ]
    )
    assert exact_bounded is not None
    assert "input_schema" in exact_bounded[0].output["capabilities"][0]


def test_bounded_catalogue_compacts_purposes_without_erasing_candidates() -> None:
    from src.backend.services.adaptive_turn_service import (
        _bounded_capability_catalogue_output,
        _json_bytes,
    )

    entries = [
        {
            "name": f"general_capability_{index:03d}",
            "purpose": (
                "Perform an ordinary bounded task using represented evidence and "
                "return the useful work product while preserving provenance, "
                "recoverability, and a concise account of the observed result."
            ),
        }
        for index in range(200)
    ]
    raw = {
        "schema_version": "adaptive_turn_capability_catalogue.v1",
        "success": True,
        "delegation": "bounded_capabilities",
        "total": len(entries),
        "delegated_total": len(entries),
        "catalogue_scope": "query_frontier",
        "offset": 0,
        "next_offset": None,
        "purpose_index": {
            "schema_version": "adaptive_turn_capability_purpose_index.v1",
            "complete": True,
            "ordering": "name_ascending_unranked",
            "purpose_projection": "first_authored_sentence",
            "purpose_max_chars": 160,
            "exact_schema_hydration": {
                "tool": "turn_capabilities",
                "guidance": (
                    "Request all alternatives being compared in one names array."
                ),
                "arguments": {"names": ["<capability names>"], "limit": 50},
            },
            "entries": entries,
        },
        "capabilities": [
            {
                "name": entry["name"],
                "query_match": True,
                "selection": {"frontier_status": "candidate"},
            }
            for entry in entries[:50]
        ],
    }

    bounded = _bounded_capability_catalogue_output(raw, max_bytes=24_000)

    assert bounded
    assert len(_json_bytes(bounded)) <= 24_000
    purpose_index = bounded["purpose_index"]
    assert purpose_index["complete"] is True
    assert len(purpose_index["entries"]) == len(entries)
    assert purpose_index["purpose_text_compacted_for_model_context"] is True
    assert purpose_index["purpose_max_chars"] < 160
    assert [item["name"] for item in purpose_index["entries"]] == [
        item["name"] for item in entries
    ]


def test_single_oversized_capability_remains_visible_as_schema_reference(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: ToolDispatchSurfaceMetadata(
            surface_family="knowledge_base",
            evidence_surface_family="knowledge_base",
            external_surface=False,
        ),
    )
    name = "oversized_general_read"
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name=name,
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                optional={
                    "actor_id": (str, type(None)),
                    **{f"field_{index}": str for index in range(2_000)},
                },
                allow_unknown=False,
            ),
            category="read",
            description="Read a broad represented record.",
            ordinary_turn_trusted_argument_bindings={"actor_id": "actor"},
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    raw = _capability_catalogue(
        gateway,
        (name,),
        {"names": [name], "limit": 1},
    )
    assert len(_json_bytes(raw)) > 24_000
    bounded_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="oversized-schema",
                tool_name="turn_capabilities",
                output=raw,
                status="ok",
            )
        ]
    )

    assert bounded_results is not None
    bounded = bounded_results[0].output
    assert len(_json_bytes(bounded)) <= 24_000
    assert bounded["next_offset"] is None
    assert bounded["capability_page_projection"] == {
        "schema_version": "adaptive_turn_capability_page_projection.v1",
        "returned": 1,
        "full_schema_count": 0,
        "schema_reference_count": 1,
        "omitted_page_entry_count": 0,
        "reason": "model_context_budget",
    }
    assert len(bounded["capabilities"]) == 1
    compact = bounded["capabilities"][0]
    assert compact["name"] == name
    assert compact["description"] == "Read a broad represented record."
    assert compact["surface_family"] == "knowledge_base"
    assert compact["evidence_surface_family"] == "knowledge_base"
    assert compact["external_surface"] is False
    assert compact["server_bound_arguments"] == ["actor_id"]
    assert "input_schema" not in compact
    assert compact["input_schema_omitted_for_model_context"] is True
    assert "input_schema_hydration" not in compact
    assert compact["input_schema_unavailable_reason"] == (
        "schema_exceeds_model_context_budget"
    )
    assert compact["direct_invocation"] == {
        "tool": "turn_invoke_capability",
        "available_if_arguments_known": True,
    }


def test_bulky_capability_metadata_falls_back_without_hiding_the_schema(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: ToolDispatchSurfaceMetadata(
            surface_family="knowledge_base",
            evidence_surface_family="knowledge_base",
            external_surface=False,
        ),
    )
    name = "metadata_heavy_read"
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name=name,
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                optional={
                    "actor_id": (str, type(None)),
                    "query": str,
                },
                allow_unknown=False,
            ),
            category="read",
            description="Read represented records. " + ("metadata " * 4_000),
            ordinary_turn_trusted_argument_bindings={"actor_id": "actor"},
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    raw = _capability_catalogue(
        gateway,
        (name,),
        {"limit": 20},
    )
    bounded_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="bulky-metadata",
                tool_name="turn_capabilities",
                output=raw,
                status="ok",
            )
        ]
    )

    assert bounded_results is not None
    bounded = bounded_results[0].output
    assert len(_json_bytes(bounded)) <= 24_000
    assert bounded["next_offset"] is None
    assert "capability_page_projection" not in bounded
    assert len(bounded["capabilities"]) == 1
    compact = bounded["capabilities"][0]
    assert compact["name"] == name
    assert compact["selection"]["frontier_status"] == "candidate"
    assert "description" not in compact
    assert "input_schema" not in compact
    purpose = bounded["purpose_index"]["entries"][0]
    assert purpose["name"] == name
    assert purpose["purpose"].startswith("Read represented records.")
    assert purpose["purpose"].endswith("...")
    assert len(purpose["purpose"]) <= 160

    exact = _capability_catalogue(
        gateway,
        (name,),
        {"names": [name], "limit": 1},
    )
    exact_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="bulky-metadata-exact",
                tool_name="turn_capabilities",
                output=exact,
                status="ok",
            )
        ]
    )
    assert exact_results is not None
    exact_capability = exact_results[0].output["capabilities"][0]
    assert exact_capability["name"] == name
    assert exact_capability["capability_metadata_omitted_for_model_context"] is True
    assert exact_capability["server_bound_arguments"] == ["actor_id"]
    assert set(exact_capability["input_schema"]["properties"]) == {"query"}
    assert "input_schema_hydration" not in exact_capability


def test_fixed_ordinary_turn_arguments_narrow_only_unsafe_options() -> None:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="bounded_diagnostics",
            handler=lambda **kwargs: kwargs,
            input_schema=Schema(
                optional={
                    "query": str,
                    "reset": bool,
                    "bundle_path": (str, type(None)),
                },
                aliases={"path": "bundle_path"},
                allow_unknown=False,
            ),
            category="read",
            ordinary_turn_fixed_arguments={
                "reset": False,
                "bundle_path": None,
            },
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
    )
    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["bounded_diagnostics"]},
    )["capabilities"][0]
    assert set(capability["input_schema"]["properties"]) == {"query"}

    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="bounded_diagnostics",
        model_payload={
            "query": "slow queries",
            "reset": True,
            "path": "/tmp/private.json",
        },
        trusted_argument_values=None,
    )
    assert payload == {
        "query": "slow queries",
        "reset": False,
        "bundle_path": None,
    }


def test_evidence_index_omission_is_bounded_and_pageable() -> None:
    compact = _compact_evidence_index(
        [
            {
                "schema_version": "turn_evidence_envelope.v1",
                "evidence_id": f"ev-{index}",
                "tool_name": "general_read",
                "call_id": f"call-{index}",
                "status": "ok",
                "preview": "x" * 240,
                "preview_truncated": True,
            }
            for index in range(1_000)
        ]
    )

    assert (
        len(
            json.dumps(
                compact,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= 24_000
    )
    omission = compact[-1]
    assert omission["schema_version"] == "adaptive_turn_evidence_index_omission.v1"
    assert omission["omitted_count"] > 0
    assert omission["total_count"] == 1_000
    assert omission["next_offset"] > 0
    assert omission["list_tool"] == "turn_list_evidence"


def test_compact_evidence_envelope_keeps_source_diagnostics_before_preview() -> None:
    source_diagnostics = {
        "coverage_complete": False,
        "counts_are_lower_bounds": True,
        "has_more": True,
        "next_offset": 20,
        "offset": 0,
        "limit": 20,
        "total": 200,
        "total_hits_is_lower_bound": True,
    }
    compact = _compact_evidence_envelope(
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": "ev-source-diagnostics",
            "source_diagnostics": source_diagnostics,
            "tool_name": "bounded_canonical_read",
            "preview": "x" * 100_000,
            "unrelated_large_source_field": "y" * 100_000,
        },
        max_bytes=400,
    )

    assert len(_json_bytes(compact)) <= 400
    assert compact["evidence_id"] == "ev-source-diagnostics"
    assert compact["source_diagnostics"] == source_diagnostics
    assert "unrelated_large_source_field" not in compact
    assert len(compact.get("preview", "")) < 100_000


@pytest.mark.parametrize("selected_value", [None, False, 0, "", []])
def test_compact_evidence_envelope_preserves_explicit_falsey_content(
    selected_value: Any,
) -> None:
    compact = _compact_evidence_envelope(
        {
            "schema_version": "turn_evidence_slice.v1",
            "evidence_id": "ev-falsey-content",
            "content": selected_value,
            "projected_payload": {
                "status": "not_found",
                "resolved_concept_id": None,
            },
            "preview": "",
        },
        max_bytes=500,
    )

    assert len(_json_bytes(compact)) <= 500
    assert "content" in compact
    assert compact["content"] == selected_value
    assert compact["projected_payload"] == {
        "status": "not_found",
        "resolved_concept_id": None,
    }
    assert "preview" in compact
    assert compact["preview"] == ""


def test_compact_evidence_envelope_exposes_executable_omission_recovery() -> None:
    selector = {
        "json_pointer": "/records/12/resolved_concept_id",
        "offset": 0,
        "max_chars": 4_000,
    }
    compact = _compact_evidence_envelope(
        {
            "schema_version": "turn_evidence_slice.v1",
            "evidence_id": "ev-oversized-selected-content",
            "selector": selector,
            "content": "x" * 20_000,
            "projected_payload": {"summary": "y" * 20_000},
        },
        max_bytes=700,
    )

    assert len(_json_bytes(compact)) <= 700
    assert "content" not in compact
    assert "projected_payload" not in compact
    assert compact["selector"] == selector
    assert compact["model_context_omission"] == {
        "schema_version": "adaptive_turn_evidence_field_omission.v1",
        "fields": ["content", "projected_payload"],
        "reason": "model_context_budget",
        "recovery_tool": "turn_read_evidence",
        "recovery_arguments": {
            "evidence_id": "ev-oversized-selected-content",
            **selector,
        },
    }


@pytest.mark.parametrize("max_bytes", [250, 400, 500])
def test_evidence_selector_never_survives_without_its_omission_contract(
    max_bytes: int,
) -> None:
    compact = _compact_evidence_envelope(
        {
            "schema_version": "turn_evidence_slice.v1",
            "evidence_id": "ev-atomic-omission",
            "selector": {
                "json_pointer": "/records/12/resolved_concept_id",
                "offset": 0,
                "max_chars": 4_000,
            },
            "content": "x" * 20_000,
            "projected_payload": {"summary": "y" * 20_000},
        },
        max_bytes=max_bytes,
    )

    assert len(_json_bytes(compact)) <= max_bytes
    assert "content" not in compact
    assert "projected_payload" not in compact
    if "selector" in compact:
        assert "model_context_omission" in compact
    if omission := compact.get("model_context_omission"):
        assert omission["recovery_tool"] == "turn_read_evidence"
        assert omission["recovery_arguments"]["evidence_id"] == (
            "ev-atomic-omission"
        )


def test_evidence_page_resumes_at_first_globally_omitted_handle() -> None:
    full_page = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}-{'e' * 512}",
            "tool_name": "general_read",
            "call_id": f"call-{index}-{'c' * 512}",
            "status": "ok",
            "trust_boundary": "untrusted_tool_output",
            "sha256": "f" * 64,
            "size_bytes": 10_000,
            "char_count": 10_000,
            "content_type": "application/json",
            "value_kind": "object",
            "preview_format": "json",
            "preview": "x" * 1_000,
            "preview_truncated": True,
            "available_selectors": [f"/items/{item}" for item in range(20)],
            "turn_id": "turn-pageable",
        }
        for index in range(250, 300)
    ]

    compact = _compact_evidence_index(
        full_page,
        max_bytes=12_000,
        base_offset=250,
        total_count=400,
    )

    omission = compact[-1]
    assert omission["schema_version"] == "adaptive_turn_evidence_index_omission.v1"
    next_offset = omission["next_offset"]
    assert 250 < next_offset < 300
    assert omission["total_count"] == 400
    assert omission["omitted_count"] == 400 - next_offset
    emitted_ids = [
        item["evidence_id"]
        for item in compact
        if isinstance(item.get("evidence_id"), str)
    ]
    assert emitted_ids == [
        f"ev-{index}-{'e' * 512}" for index in range(250, next_offset)
    ]
    resumed = _compact_evidence_index(
        full_page[next_offset - 250 :],
        max_bytes=12_000,
        base_offset=next_offset,
        total_count=400,
    )
    assert resumed[0]["evidence_id"] == f"ev-{next_offset}-{'e' * 512}"


def test_fresh_evidence_projection_prioritises_hydrated_slices_mechanically() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}",
            "tool_name": "general_read",
            "call_id": f"call-{index}",
            "status": "ok",
            "sha256": f"{index:064x}",
            "provenance": {"source": f"source-{index}"},
            "preview": f"preview-{index}-" + ("p" * 1_100),
            "preview_truncated": True,
        }
        for index in range(16)
    ]
    envelope_views = [dict(item) for item in evidence_index]
    hydrated_slices = [
        {
            "schema_version": "turn_evidence_slice.v1",
            "success": True,
            "evidence_id": f"ev-{12 + index}",
            "tool_name": "general_read",
            "call_id": f"call-{12 + index}",
            "trust_boundary": "untrusted_tool_output",
            "source_sha256": f"{12 + index:064x}",
            "provenance": {"source": f"source-{12 + index}"},
            "selector": {
                "json_pointer": f"/records/{index}/summary",
                "offset": 0,
                "max_chars": 5_200,
            },
            "content": f"hydrated-{index}-" + ("h" * 5_000),
            "content_format": "text",
            "returned_chars": 5_011,
            "has_more": False,
        }
        for index in range(4)
    ]

    context = _final_synthesis_context(
        [],
        evidence_index,
        [
            *envelope_views,
            hydrated_slices[0],
            hydrated_slices[1],
            dict(hydrated_slices[0]),
            hydrated_slices[2],
            hydrated_slices[3],
        ],
    )

    assert len(context) == 1
    assert len(_json_bytes(context[0])) <= 24_000
    payload = json.loads(context[0]["content"])
    included_views = payload["evidence_views"]
    included_slices = [
        item
        for item in included_views
        if item["schema_version"] == "turn_evidence_slice.v1"
    ]
    assert included_slices == hydrated_slices
    first_envelope = next(
        (
            index
            for index, item in enumerate(included_views)
            if item["schema_version"] == "turn_evidence_envelope.v1"
        ),
        len(included_views),
    )
    assert all(
        item["schema_version"] == "turn_evidence_slice.v1"
        for item in included_views[:first_envelope]
    )
    assert included_slices[0]["selector"]["json_pointer"] == ("/records/0/summary")
    assert included_slices[0]["source_sha256"] == f"{12:064x}"
    assert included_slices[0]["provenance"] == {"source": "source-12"}

    projection = payload["evidence_view_projection"]
    assert projection["order"] == (
        "content_bearing_evidence_slices_first_stable_within_class"
    )
    assert projection["total_count"] == len(envelope_views) + len(hydrated_slices)
    assert projection["included_count"] == len(included_views)
    assert projection["omitted_count"] > 0
    assert projection["reason"] == "model_context_budget"
    assert "next_offset" not in projection
    assert payload["evidence"]
    assert projection["read_tool"] == "turn_read_evidence"
    assert projection["list_tool"] == "turn_list_evidence"


def test_oversized_early_slice_does_not_suppress_a_later_fitting_slice() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}",
            "tool_name": "general_read",
            "call_id": f"call-{index}",
            "status": "ok",
        }
        for index in range(2)
    ]
    oversized_slice = {
        "schema_version": "turn_evidence_slice.v1",
        "success": True,
        "evidence_id": "ev-0",
        "source_sha256": "a" * 64,
        "selector": {"json_pointer": "/large"},
        "content": "x" * 23_500,
    }
    later_slice = {
        "schema_version": "turn_evidence_slice.v1",
        "success": True,
        "evidence_id": "ev-1",
        "source_sha256": "b" * 64,
        "selector": {"json_pointer": "/corrective"},
        "content": "A later corrective slice.",
    }

    context = _final_synthesis_context(
        [],
        evidence_index,
        [oversized_slice, later_slice],
    )

    payload = json.loads(context[0]["content"])
    assert len(_json_bytes(context[0])) <= 24_000
    assert payload["evidence_views"] == [later_slice]
    projection = payload["evidence_view_projection"]
    assert projection["total_count"] == 2
    assert projection["included_count"] == 1
    assert projection["omitted_count"] == 1
    assert projection["reason"] == "model_context_budget"


def test_tool_result_correlation_shell_overflow_has_no_oversized_fallback() -> None:
    results = [
        ToolResult(
            call_id=f"call-{index}",
            tool_name="turn_invoke_capability",
            status="ok",
            output={"evidence_id": f"ev-{index}", "preview": "x" * 1_000},
        )
        for index in range(500)
    ]

    assert _bound_tool_results_for_model(results) is None


def test_tool_result_batch_preserves_full_outputs_when_aggregate_fits() -> None:
    results = [
        ToolResult(
            call_id=f"capability-{index}",
            tool_name="turn_capabilities",
            status="ok",
            output={
                "name": f"capability-{index}",
                "description": character * size,
                "planner_hint": f"planner-{index}",
            },
        )
        for index, (character, size) in enumerate(
            (("a", 6_700), ("b", 5_100), ("c", 5_400), ("d", 3_300))
        )
    ]
    complete_batch = [
        {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "status": result.status,
            "output": result.output,
        }
        for result in results
    ]
    assert len(_json_bytes(complete_batch)) < 24_000

    bounded = _bound_tool_results_for_model(results)

    assert bounded is not None
    assert [result.output for result in bounded] == [
        result.output for result in results
    ]
    assert bounded[0].output["planner_hint"] == "planner-0"


def test_tool_result_batch_preserves_29_selected_values_and_projections() -> None:
    larger_selected_value = "#V#" + ("resolved_long_identifier_" * 90)
    selected_values: list[Any] = [
        None,
        False,
        0,
        "",
        [],
        "null",
        "#V#neet_zkan_tan",
        "#V#zhu_yonghua",
        larger_selected_value,
        *[f"#V#resolved_person_{index}" for index in range(20)],
    ]
    assert len(selected_values) == 29
    results = [
        ToolResult(
            call_id=f"read-resolution-{index}",
            tool_name="turn_read_evidence",
            status="ok",
            output={
                "schema_version": "turn_evidence_slice.v1",
                "success": True,
                "evidence_id": f"ev-resolution-{index}",
                "tool_name": "resolve_concept_by_name",
                "call_id": f"resolve-person-{index}",
                "turn_id": "turn-many-person-resolutions",
                "status": "ok",
                "trust_boundary": "untrusted_tool_output",
                "source_sha256": f"{index:064x}",
                "source_size_bytes": 20_000,
                "provenance": {"source": "p" * 900},
                "selector": {
                    "json_pointer": "/resolved_concept_id",
                    "offset": 0,
                    "max_chars": 4_000,
                },
                "content": selected_value,
                "content_format": "json",
                "selected_value_kind": (
                    "null" if selected_value is None else "value"
                ),
                "returned_chars": len(str(selected_value)),
                "has_more": False,
                "next_offset": None,
                "projected_payload": {
                    "status": (
                        "not_found"
                        if selected_value in (None, "null")
                        else "resolved"
                    ),
                    "resolved_concept_id": selected_value,
                },
            },
        )
        for index, selected_value in enumerate(selected_values)
    ]
    complete_batch = [
        {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "status": result.status,
            "output": result.output,
        }
        for result in results
    ]
    assert len(_json_bytes(complete_batch)) > 24_000
    receipt_shells = [
        {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "status": result.status,
            "output": {
                "evidence_id": result.output["evidence_id"],
                "status": result.output["status"],
                "success": result.output["success"],
            },
        }
        for result in results
    ]
    equal_output_share = (24_000 - len(_json_bytes(receipt_shells))) // len(results)
    equally_compacted_large_result = _compact_evidence_envelope(
        results[8].output,
        max_bytes=equal_output_share,
    )
    assert "content" not in equally_compacted_large_result

    bounded = _bound_tool_results_for_model(results)

    assert bounded is not None
    assert len(
        _json_bytes(
            [
                {
                    "call_id": result.call_id,
                    "tool_name": result.tool_name,
                    "status": result.status,
                    "output": result.output,
                }
                for result in bounded
            ]
        )
    ) <= 24_000
    assert len(bounded) == 29
    for index, result in enumerate(bounded):
        assert "content" in result.output
        assert result.output["content"] == selected_values[index]
        assert result.output["projected_payload"] == {
            "status": (
                "not_found" if selected_values[index] in (None, "null") else "resolved"
            ),
            "resolved_concept_id": selected_values[index],
        }


def test_bounded_effect_result_preserves_exact_partial_receipt() -> None:
    result = ToolResult(
        call_id="partial-effect",
        tool_name="turn_invoke_capability",
        status="error",
        output={
            "evidence_id": "ev-partial",
            "status": "partial",
            "effect_status": "partial",
            "changed": True,
            "error_code": "derived_identity_write_failed",
            "mutation_outcome": "partial",
            "outcome_finality": "terminal_for_turn",
            "preview": "x" * 20_000,
        },
    )

    bounded = _bound_tool_results_for_model([result], max_bytes=450)

    assert bounded is not None
    assert bounded[0].status == "error"
    assert bounded[0].output["evidence_id"] == "ev-partial"
    assert bounded[0].output["effect_status"] == "partial"
    assert bounded[0].output["changed"] is True
    assert bounded[0].output["error_code"] == "derived_identity_write_failed"
    assert bounded[0].output["mutation_outcome"] == "partial"
    assert bounded[0].output["outcome_finality"] == "terminal_for_turn"


def test_model_can_invoke_any_delegated_read_without_a_prompt_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_tail = "z" * 50_000
    seen_actor: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "project_tool_payload_for_llm",
        lambda tool_name, payload, **_kwargs: (
            {
                "_llm_view": "test_projection.v1",
                "relationships": {
                    "has_file": ["#V#existing_file_copy"],
                },
            }
            if tool_name == "general_read" and payload.get("answer") == "found"
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "resolve_tool_projection_contract",
        lambda _tool_name: object(),
    )

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_actor.update(
            {
                "user": get_effective_user_concept_id(),
                "org": get_effective_organisation_concept_id(),
                "arguments": kwargs,
            }
        )
        return {"success": True, "answer": "found", "raw_tail": raw_tail}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-1",
                    payload={
                        "name": "general_read",
                        "arguments": {
                            "query": "anything",
                            "namespace": "#V#spoof@other",
                            "user_id": "#V#spoof",
                            "org_id": "#V#other",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The evidence says found."),
    )
    gateway = _gateway(handler)

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Use whatever read is useful.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-2",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The evidence says found."
    assert seen_actor["user"] == "#V#person"
    assert seen_actor["org"] == "#V#org"
    assert seen_actor["arguments"] == {
        "query": "anything",
        "namespace": "#V#spoof@other",
        "user_id": "#V#spoof",
        "org_id": "#V#other",
    }
    assert len(result.evidence_index) == 1
    envelope = result.evidence_index[0]
    assert envelope["preview_truncated"] is True
    assert envelope["sha256"]
    model_evidence_text = client.calls[1]["context"][-1]["content"]
    model_evidence = json.loads(model_evidence_text)
    assert model_evidence["projected_payload"]["relationships"]["has_file"] == [
        "#V#existing_file_copy"
    ]
    assert model_evidence["preview_truncated"] is True
    assert model_evidence["evidence_id"] == envelope["evidence_id"]
    assert "json_pointer" in model_evidence["available_selectors"]
    assert "z" * 10_000 not in model_evidence_text
    assert len(model_evidence_text) < 10_000
    assert "projected_payload" not in envelope
    assert "effective_payload" not in result.tool_invocations[0]
    assert raw_tail not in json.dumps(result.tool_invocations)
    assert all(
        invocation.get("tool") != "general_write"
        for invocation in result.tool_invocations
    )


def test_repeated_tool_results_resolve_projection_contract_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_contract = object()
    resolve_calls: list[str] = []
    resolve_actor_scopes: list[tuple[str | None, str | None]] = []
    projection_calls: list[tuple[str, object | None]] = []

    def resolve_contract(tool_name: str) -> object:
        resolve_calls.append(tool_name)
        resolve_actor_scopes.append(
            (
                get_effective_user_concept_id(),
                get_effective_organisation_concept_id(),
            )
        )
        return projection_contract

    def project_payload(
        tool_name: str,
        payload: Mapping[str, Any],
        *,
        contract: object | None = None,
    ) -> dict[str, Any]:
        projection_calls.append((tool_name, contract))
        return {"_llm_view": "test_projection.v1", "answer": payload["answer"]}

    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "resolve_tool_projection_contract",
        resolve_contract,
    )
    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "project_tool_payload_for_llm",
        project_payload,
    )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-projection-1",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "first"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-projection-2",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "second"},
                    },
                ),
            ],
        ),
        LLMResponse(text_response="Both reads completed."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **kwargs: {
                "success": True,
                "answer": str(kwargs.get("query") or ""),
            }
        ),
        prompt="Read both candidates.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-projection-cache",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Both reads completed."
    assert resolve_calls == ["general_read"]
    assert resolve_actor_scopes == [("#V#person", "#V#org")]
    assert projection_calls == [
        ("general_read", projection_contract),
        ("general_read", projection_contract),
    ]
    assert all(
        invocation["result_projection_duration_ms"] >= 0
        for invocation in result.tool_invocations
    )


def test_effect_batch_preserves_order_and_read_can_observe_prior_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    state = {"created": False}

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, dict(arguments)))
        if name == "create_concepts":
            state["created"] = True
            return {"success": True, "effect_status": "succeeded", "changed": True}
        if name == "general_read":
            return {"success": True, "created": state["created"]}
        return {"success": True, "effect_status": "succeeded", "changed": True}

    _stub_same_turn_ontology_delegation(monkeypatch)
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="ordered-create",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Ordered"}]},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="ordered-read",
                    payload={
                        "name": "general_read",
                        "arguments": {"concept_id": "#V#ordered"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="ordered-text",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": "#V#ordered",
                            "predicate": "hasDescription",
                            "text": "Observed after creation.",
                            "provenance": {"source": "model-spoof"},
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response="Canonical state was available to inspect."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent and inspect this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="ordered-effects",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert [name for name, _arguments in seen] == [
        "create_concepts",
        "general_read",
        "upsert_text_relation",
    ]
    assert seen[2][1]["provenance"] is None
    assert result.tool_invocations[1]["evidence"]["preview"].find('"created":true') >= 0


def test_relation_progress_emits_one_human_start_and_terminal_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_events: list[dict[str, Any]] = []
    _stub_same_turn_ontology_delegation(monkeypatch)

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="robert-amor-supervision",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": (
                                "#V#nathan_young_doctoral_candidature_situation"
                            ),
                            "predicate": "#V#has_doctoral_supervisor",
                            "target": "#V#robert_amor",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="That supervision relationship was already known."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, _arguments: {
                "success": True,
                "effect_status": "succeeded",
                "changed": False,
            }
        ),
        prompt="Re-assert the existing Robert Amor supervision relationship.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="semantic-relation-progress",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )

    relation_events = [
        event
        for event in progress_events
        if event.get("call_id") == "robert-amor-supervision"
    ]
    assert [event["event_kind"] for event in relation_events] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert [event["subtask"] for event in relation_events] == [
        "Add Relationship",
        "Add Relationship",
    ]
    assert relation_events[0]["result_summary"] == (
        "Add Relationship: Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor (in progress)."
    )
    assert relation_events[1]["success"] is True
    assert relation_events[1]["result_summary"] == (
        "Add Relationship reported that no change was needed: "
        "Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor."
    )
    assert relation_events[0]["semantic_operation"]["lifecycle_status"] == "running"
    assert relation_events[0]["semantic_operation"]["verification"] == {
        "status": "unknown",
        "canonical_read_back_present": False,
        "source": "none",
    }
    assert relation_events[1]["semantic_operation"]["outcome"]["changed"] is False
    assert relation_events[1]["semantic_operation"]["verification"] == {
        "status": "receipt_only",
        "canonical_read_back_present": False,
        "source": "effect_receipt",
    }
    assert result.tool_invocations[0]["changed"] is False


def test_concept_search_progress_emits_query_and_bounded_results() -> None:
    progress_events: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="search_concepts",
            handler=lambda **_kwargs: {
                "results": [
                    {
                        "concept_id": "#V#university_of_auckland",
                        "name": "University of Auckland",
                    },
                    {"concept_id": "#V#university", "name": "University"},
                ],
                "total_count": 4,
                "match_types_used": ["substring"],
                "query_info": {"query": "University of Auckland"},
            },
            input_schema=Schema(
                required={"query": str},
                allow_unknown=False,
                description="Search represented concepts.",
            ),
            category="read",
            ordinary_turn_public=True,
            description="Search represented concepts.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="search-auckland",
                    payload={
                        "name": "search_concepts",
                        "arguments": {"query": "University of Auckland"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="I found the represented university concepts."),
    )

    execute_adaptive_turn(
        gateway=gateway,
        prompt="Find the represented University of Auckland concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="semantic-search-progress",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )

    search_events = [
        event for event in progress_events if event.get("call_id") == "search-auckland"
    ]
    assert [event["event_kind"] for event in search_events] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert [event["subtask"] for event in search_events] == [
        "Search Concepts",
        "Search Concepts",
    ]
    assert search_events[0]["result_summary"] == (
        "Search Concepts: Query: “University of Auckland” (in progress)."
    )
    assert search_events[1]["result_summary"] == (
        "Search Concepts returned 4 concept matches: University of Auckland, "
        "University and 2 others for Query: “University of Auckland”."
    )
    assert search_events[1]["semantic_operation"]["observation"]["items"][0] == {
        "name": "University of Auckland",
        "identifier": "#V#university_of_auckland",
    }


def test_effect_evidence_preserves_success_partial_failure_and_unknown_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    seen_cases: list[str] = []
    progress_events: list[dict[str, Any]] = []
    release_timeout_handler = Event()

    def handler(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        case = str(arguments["case"])
        seen_cases.append(case)
        if case == "partial":
            return {
                "success": False,
                "effect_status": "partial",
                "changed": True,
                "partial_failures": [{"stage": "derived_inverse", "error": "late"}],
            }
        if case == "failed":
            return {
                "success": False,
                "effect_status": "failed",
                "changed": False,
                "error_code": "rejected",
            }
        if case == "timeout":
            while not internal_mcp_cancellation_requested():
                time.sleep(0.001)
            assert release_timeout_handler.wait(timeout=1.0)
        return {"success": True, "effect_status": "succeeded", "changed": True}

    calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"effect-{case}",
            payload={
                "name": "create_concepts",
                "arguments": {"case": case},
            },
        )
        for case in ("succeeded", "partial", "failed", "timeout")
    ]
    client = _SequenceClient(
        LLMResponse(text_response="", tool_calls=calls),
        LLMResponse(text_response="The bounded effects were reported truthfully."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            write_timeout_sec=0.02,
            hard_timeout_enabled=True,
        ),
        prompt="Exercise bounded effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-statuses",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )
    release_timeout_handler.set()

    assert seen_cases == ["succeeded", "partial", "failed", "timeout"]
    assert result.terminal_status == "effect_outcome_indeterminate"
    assert result.response_authority == "canonical_outcome"
    assert "The bounded effects were reported truthfully." in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text
    assert [item["effect_status"] for item in result.tool_invocations] == [
        "succeeded",
        "partial",
        "failed",
        "indeterminate",
    ]
    assert [item["status"] for item in result.tool_invocations] == [
        "ok",
        "error",
        "error",
        "error",
    ]
    assert len({item["effect_id"] for item in result.tool_invocations}) == 4
    assert result.tool_invocations[0]["changed"] is True
    assert result.tool_invocations[1]["changed"] is True
    assert "partial_failures" in result.tool_invocations[1]["evidence"]["preview"]
    assert result.tool_invocations[2]["changed"] is False
    assert result.tool_invocations[3]["changed"] is None
    assert all(
        item.get("evidence", {}).get("evidence_id") for item in result.tool_invocations
    )
    partial_progress = next(
        event
        for event in progress_events
        if event.get("status") == "tool_completed"
        and event.get("call_id") == "effect-partial"
    )
    assert partial_progress["success"] is False
    assert not partial_progress["result_summary"].startswith("Finished ")


def test_partial_effect_downgrades_nominal_model_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="partial-effect",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Only partly created"}]},
                    },
                )
            ],
        ),
        LLMResponse(text_response="Everything was created."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, _arguments: {
                "success": False,
                "effect_status": "partial",
                "changed": True,
                "error_code": "derived_relation_failed",
            }
        ),
        prompt="Create the represented concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="partial-effect-terminal-status",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert "status `partial`" in result.response_text
    assert "derived_relation_failed" in result.response_text
    assert "Everything was created." in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text


def test_identity_shaped_targets_are_not_globally_rewritten() -> None:
    captured: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True, "invites": []}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-actor-aliases",
                    payload={
                        "name": "shared_conversation_list_invites",
                        "arguments": {
                            "invitee_user_id": "#V#legitimate_target",
                            "user_concept_id": "#V#spoof",
                            "acting_user_concept_id": "#V#spoof",
                            "actor_user_id": "#V#spoof",
                            "on_behalf_of_user_concept_id": "#V#spoof",
                            "actor_concept_id": "#V#spoof_agent",
                            "agent_concept_id": "#V#spoof_agent",
                            "organisation_concept_id": "#V#other_org",
                            "org_id": "#V#other_org",
                            "namespace": "#V#spoof@other_org",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="No current invites were found."),
    )

    result = execute_adaptive_turn(
        gateway=_actor_alias_gateway(handler),
        prompt="List my current shared-conversation invites.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-actor-aliases",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "No current invites were found."
    assert captured["invitee_user_id"] == "#V#legitimate_target"
    for preserved_field in (
        "user_concept_id",
        "acting_user_concept_id",
        "actor_user_id",
        "on_behalf_of_user_concept_id",
        "actor_concept_id",
        "agent_concept_id",
        "organisation_concept_id",
        "org_id",
        "namespace",
    ):
        assert captured[preserved_field].startswith("#V#")


def test_context_limit_retries_only_after_a_smaller_different_request() -> None:
    context_error = StructuredToolContextLimitError(
        "context too large",
        decision={"provider_error_code": "context_length_exceeded"},
    )
    client = _SequenceClient(
        context_error,
        LLMResponse(text_response="Recovered from a smaller context."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Keep the task itself.",
        context=[
            {"role": "system", "content": "trusted instruction"},
            {"role": "assistant", "content": "old context " * 10_000},
        ],
        llm_client=client,
        model="test-model",
        turn_id="turn-3",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Recovered from a smaller context."
    assert len(client.calls) == 2
    before_size = len(str(client.calls[0]["context"]))
    after_size = len(str(client.calls[1]["context"]))
    assert after_size < before_size
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_context_limit_recovery"
    )
    assert recovery["changed"] is True
    assert recovery["after_bytes"] < recovery["before_bytes"]


def test_context_limit_recovery_keeps_a_pageable_evidence_reference() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}",
            "tool_name": "general_read",
            "call_id": f"call-{index}",
            "status": "ok",
            "preview": "evidence " * 80,
            "preview_truncated": True,
        }
        for index in range(100)
    ]

    compact = _compact_context_after_limit(
        prompt="Answer from evidence.",
        context=[],
        evidence_index=evidence_index,
        before_size=30_000,
    )

    assert compact is not None
    assert compact
    assert len(_json_bytes(compact)) < 15_000
    payload = json.loads(compact[-1]["content"])
    assert payload["type"] == "prior_tool_evidence_index"
    assert payload["evidence"]
    assert any(
        item.get("list_tool") == "turn_list_evidence"
        and item.get("total_count") == len(evidence_index)
        for item in payload["evidence"]
    )


def test_context_limit_recovery_keeps_exact_hydrated_evidence_view() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": "ev-hydrated",
            "tool_name": "general_read",
            "call_id": "call-hydrated",
            "status": "ok",
            "sha256": "a" * 64,
            "provenance": {"source": "represented-document"},
            "preview": "preview-only",
        }
    ]
    hydrated_slice = {
        "schema_version": "turn_evidence_slice.v1",
        "success": True,
        "evidence_id": "ev-hydrated",
        "tool_name": "general_read",
        "call_id": "call-hydrated",
        "trust_boundary": "untrusted_tool_output",
        "source_sha256": "a" * 64,
        "source_size_bytes": 50_000,
        "provenance": {"source": "represented-document"},
        "selector": {
            "json_pointer": "/project/summary",
            "offset": 0,
            "max_chars": 4_000,
        },
        "content": "The explicitly selected project summary.",
        "content_format": "text",
        "returned_chars": 40,
        "has_more": False,
    }

    compact = _compact_context_after_limit(
        prompt="Answer from the selected evidence.",
        context=[
            {
                "role": "tool",
                "content": "an obsolete provider-specific tool transcript",
            }
        ],
        evidence_index=evidence_index,
        evidence_views=[hydrated_slice],
        before_size=60_000,
    )

    assert compact is not None
    assert len(compact) == 1
    assert compact[0]["role"] == "user"
    assert len(_json_bytes(compact[0])) <= 24_000
    payload = json.loads(compact[0]["content"])
    assert payload["schema_version"] == "adaptive_turn_context_recovery.v1"
    assert payload["evidence_views"] == [hydrated_slice]
    assert payload["evidence"][0]["evidence_id"] == "ev-hydrated"
    assert payload["evidence_views"][0]["source_sha256"] == "a" * 64
    assert payload["evidence_views"][0]["selector"] == {
        "json_pointer": "/project/summary",
        "offset": 0,
        "max_chars": 4_000,
    }
    assert payload["evidence_views"][0]["provenance"] == {
        "source": "represented-document"
    }


def test_context_limit_recovery_refuses_to_drop_existing_evidence() -> None:
    compact = _compact_context_after_limit(
        prompt="x" * 20_000,
        context=[],
        evidence_index=[
            {
                "schema_version": "turn_evidence_envelope.v1",
                "evidence_id": "ev-1",
                "tool_name": "general_read",
                "call_id": "call-1",
                "status": "ok",
            }
        ],
        before_size=20_100,
    )

    assert compact is None


def test_context_limit_does_not_retry_an_unchanged_request() -> None:
    client = _SequenceClient(
        StructuredToolContextLimitError(
            "context too large",
            decision={"provider_error_code": "context_length_exceeded"},
        )
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="x" * 20_000,
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-4",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "context_limit_unrecoverable"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_context_limit_recovery"
    )
    assert recovery["changed"] is False


def _wrapped_model_timeout(message: str = "OpenAI call failed") -> ToolCallError:
    try:
        raise TimeoutError("provider request deadline exhausted")
    except TimeoutError as cause:
        try:
            raise ToolCallError(message) from cause
        except ToolCallError as wrapped:
            return wrapped


def test_model_timeout_retries_once_with_smaller_faithful_context() -> None:
    client = _SequenceClient(
        _wrapped_model_timeout(),
        LLMResponse(text_response="Recovered after the request timeout."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Keep the actual task.",
        context=[
            {"role": "system", "content": "trusted instruction"},
            {"role": "assistant", "content": "old context " * 10_000},
        ],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-timeout-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered after the request timeout."
    assert len(client.calls) == 2
    assert len(_json_bytes(client.calls[1]["context"])) < len(
        _json_bytes(client.calls[0]["context"])
    )
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_liveness_recovery"
    )
    assert recovery["changed"] is True
    assert recovery["reason"] == "fresh_smaller_faithful_context"
    assert recovery["after_bytes"] < recovery["before_bytes"]
    assert result.llm_calls[0]["status"] == "failed"


def test_model_timeout_recovery_retains_existing_tool_evidence() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-timeout",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "canonical record"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-before-timeout",
            ),
        ),
        _wrapped_model_timeout(),
        LLMResponse(text_response="Recovered from the retained evidence."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "record": {
                    "summary": "The exact canonical record.",
                    "raw": "z" * 50_000,
                },
            }
        ),
        prompt="Read the canonical record and answer.",
        context=[{"role": "assistant", "content": "old context " * 10_000}],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-timeout-evidence-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered from the retained evidence."
    assert len(client.calls) == 3
    assert client.calls[1]["continuation"].response_id == "response-before-timeout"
    assert "continuation" not in client.calls[-1]
    assert "tool_results" not in client.calls[-1]
    recovery_context = client.calls[-1]["context"]
    evidence_messages = [
        item
        for item in recovery_context
        if item.get("role") == "user"
        and isinstance(item.get("content"), str)
        and item["content"].startswith("{")
    ]
    assert len(evidence_messages) == 1
    evidence_payload = json.loads(evidence_messages[0]["content"])
    assert evidence_payload["schema_version"] == "adaptive_turn_context_recovery.v1"
    assert evidence_payload["evidence"][0]["call_id"] == "read-before-timeout"
    assert "z" * 5_000 not in evidence_messages[0]["content"]


def test_structured_transport_failure_after_evidence_gets_one_tool_free_answer() -> (
    None
):
    provider_failure = StructuredToolTransportError(
        "Gemini Interactions returned an unsuccessful provider status.",
        decision={
            "provider": "gemini",
            "effective_api_surface": "interactions",
            "model": "gemini-3.7-flash",
            "provider_status": "budget_exceeded",
            "provider_errors": [],
            "failure_kind": "provider_response_failed",
        },
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-provider-budget",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="gemini",
                api_surface="interactions",
                model="gemini-3.7-flash",
                state_mode="stateless",
                input_items=[{"type": "user_input", "content": []}],
                output_items=[
                    {
                        "type": "function_call",
                        "id": "read-before-provider-budget",
                        "name": "turn_invoke_capability",
                        "arguments": {},
                    }
                ],
            ),
        ),
        provider_failure,
        LLMResponse(text_response="Recovered answer from retained evidence."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "students": [{"name": "Student A", "expected_end": "2027"}],
            }
        ),
        prompt="Make a table of my current students.",
        context=[
            {
                "role": "assistant",
                "content": "old conversational context " * 20_000,
            }
        ],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-provider-budget-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered answer from retained evidence."
    assert len(client.calls) == 3
    assert client.calls[1]["continuation"].state_mode == "stateless"
    assert "continuation" not in client.calls[2]
    assert "tool_results" not in client.calls[2]
    assert client.calls[2]["available_tools"] == []
    final_context = client.calls[2]["context"]
    evidence_message = next(
        item
        for item in final_context
        if item.get("role") == "user"
        and "adaptive_turn_context_recovery.v1" in str(item.get("content"))
    )
    assert "read-before-provider-budget" in evidence_message["content"]
    assert len(_json_bytes(final_context)) <= 64_512
    failed_call = result.llm_calls[1]
    assert failed_call["failure_kind"] == "provider_response_failed"
    assert failed_call["provider_status"] == "budget_exceeded"
    assert failed_call["transport_decision"]["provider"] == "gemini"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_transport_recovery"
    )
    assert recovery["reason"] == "provider_budget_exhausted_after_evidence"
    assert recovery["action"] == "fresh_answer_without_tools"
    assert recovery["evidence_count"] == 1
    assert recovery["tool_invocation_count"] == 1
    assert recovery["context_compacted"] is True
    assert recovery["context_after_bytes"] < recovery["context_before_bytes"]
    assert recovery["context_max_bytes"] == 64_000


def test_typed_connection_failure_after_evidence_gets_one_tool_free_answer() -> (
    None
):
    connection_failure = StructuredToolTransportError(
        "Gemini could not be reached for the structured-tool request.",
        decision={
            "provider": "gemini",
            "effective_api_surface": "interactions",
            "model": "gemini-3.7-flash",
            "failure_kind": "provider_connection_failed",
            "provider_request_sent": False,
        },
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-connection-failure",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="gemini",
                api_surface="interactions",
                model="gemini-3.7-flash",
                state_mode="stateless",
                input_items=[{"type": "user_input", "content": []}],
                output_items=[
                    {
                        "type": "function_call",
                        "id": "read-before-connection-failure",
                        "name": "turn_invoke_capability",
                        "arguments": {},
                    }
                ],
            ),
        ),
        connection_failure,
        LLMResponse(text_response="Recovered after the connection failure."),
    )
    sleeps: list[float] = []

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "students": [{"name": "Student A", "expected_end": "2027"}],
            }
        ),
        prompt="Make a table of my current students.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-connection-failure-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered after the connection failure."
    assert len(client.calls) == 3
    assert sleeps == []
    assert client.calls[2]["available_tools"] == []
    assert "continuation" not in client.calls[2]
    assert "tool_results" not in client.calls[2]
    assert result.llm_calls[1]["failure_kind"] == "provider_connection_failed"
    assert result.llm_calls[1]["provider_request_sent"] is False
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_transport_recovery"
    )
    assert recovery["reason"] == "provider_connection_failed_after_evidence"


def test_typed_rate_limit_after_evidence_waits_then_recovers_without_tools() -> (
    None
):
    retry_after_seconds = 43.424416244
    rate_limit = StructuredToolTransportError(
        "Gemini temporarily rate-limited the structured-tool request.",
        decision={
            "provider": "gemini",
            "effective_api_surface": "interactions",
            "model": "gemini-3.7-flash",
            "provider_status": "RESOURCE_EXHAUSTED",
            "provider_status_code": 429,
            "provider_error_code": "too_many_requests",
            "failure_kind": "provider_rate_limited",
            "provider_request_sent": True,
            "retry_after_seconds": retry_after_seconds,
            "retry_after_source": "provider_message",
        },
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            usage={"input_tokens": 229_113, "output_tokens": 31},
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-rate-limit",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="gemini",
                api_surface="interactions",
                model="gemini-3.7-flash",
                state_mode="stateless",
                input_items=[{"type": "user_input", "content": []}],
                output_items=[
                    {
                        "type": "function_call",
                        "id": "read-before-rate-limit",
                        "name": "turn_invoke_capability",
                        "arguments": {},
                    }
                ],
            ),
        ),
        rate_limit,
        LLMResponse(text_response="Recovered after the provider-directed wait."),
    )
    clock = _ManualClock()
    sleep_slices: list[float] = []
    progress_events: list[dict[str, Any]] = []

    def sleep_and_advance(seconds: float) -> None:
        sleep_slices.append(seconds)
        clock.now += seconds

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "students": [{"name": "Student A", "expected_end": "2027"}],
            }
        ),
        prompt="Make a table of my current students.",
        context=[{"role": "assistant", "content": "old context " * 20_000}],
        llm_client=client,
        model="gemini-3.7-flash",
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="turn-rate-limit-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        clock=clock,
        sleeper=sleep_and_advance,
        provider_retry_wait_max_seconds=90,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered after the provider-directed wait."
    assert len(client.calls) == 3
    assert sum(sleep_slices) == pytest.approx(retry_after_seconds)
    assert max(sleep_slices) <= 1.0
    assert client.calls[2]["available_tools"] == []
    assert "continuation" not in client.calls[2]
    assert "tool_results" not in client.calls[2]
    assert len(_json_bytes(client.calls[2]["context"])) <= 64_512
    failed_call = result.llm_calls[1]
    assert failed_call["failure_kind"] == "provider_rate_limited"
    assert failed_call["provider_status_code"] == 429
    assert failed_call["provider_error_code"] == "too_many_requests"
    assert failed_call["provider_request_sent"] is True
    assert failed_call["retry_after_seconds"] == retry_after_seconds
    wait_receipt = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_provider_retry_wait"
    )
    assert wait_receipt["action"] == "wait_then_fresh_answer_without_tools"
    assert wait_receipt["wait_completed"] is True
    assert wait_receipt["observed_input_tokens"] == 229_113
    assert wait_receipt["retry_after_source"] == "provider_message"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_transport_recovery"
    )
    assert recovery["reason"] == "provider_rate_limited_after_evidence"
    assert [
        event["event_kind"]
        for event in progress_events
        if event.get("event_kind", "").startswith("provider_retry_wait_")
    ] == ["provider_retry_wait_start", "provider_retry_wait_end"]


def test_rate_limit_recovery_is_not_repeated_for_answer_only_failure() -> None:
    def rate_limit() -> StructuredToolTransportError:
        return StructuredToolTransportError(
            "Gemini temporarily rate-limited the structured-tool request.",
            decision={
                "provider": "gemini",
                "provider_status_code": 429,
                "provider_error_code": "too_many_requests",
                "failure_kind": "provider_rate_limited",
                "provider_request_sent": True,
                "retry_after_seconds": 0.0,
                "retry_after_source": "response_header",
            },
        )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-two-rate-limits",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
        ),
        rate_limit(),
        rate_limit(),
        LLMResponse(text_response="A fourth call must not happen."),
    )
    sleeps: list[float] = []

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True, "students": []}),
        prompt="Make a table of my current students.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-rate-limit-single-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    assert result.terminal_status == "model_error"
    assert len(client.calls) == 3
    assert sleeps == []
    assert "temporarily rate-limited" in result.response_text
    assert len(
        [
            item
            for item in result.aux_llm_calls
            if item.get("type") == "adaptive_turn_provider_retry_wait"
        ]
    ) == 1
    assert len(
        [
            item
            for item in result.aux_llm_calls
            if item.get("type") == "adaptive_turn_model_transport_recovery"
        ]
    ) == 1


@pytest.mark.parametrize("retry_after_seconds", [None, 120.0])
def test_rate_limit_after_evidence_without_safe_wait_returns_retry_later(
    retry_after_seconds: float | None,
) -> None:
    decision: dict[str, Any] = {
        "provider": "gemini",
        "provider_status_code": 429,
        "failure_kind": "provider_rate_limited",
        "provider_request_sent": True,
    }
    if retry_after_seconds is not None:
        decision["retry_after_seconds"] = retry_after_seconds
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-unsafe-rate-limit-wait",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
        ),
        StructuredToolTransportError(
            "Gemini temporarily rate-limited the structured-tool request.",
            decision=decision,
        ),
        LLMResponse(text_response="A third call must not happen."),
    )
    sleeps: list[float] = []

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True, "students": []}),
        prompt="Make a table of my current students.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-rate-limit-no-safe-wait",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        sleeper=lambda seconds: sleeps.append(seconds),
        provider_retry_wait_max_seconds=90,
    )

    assert result.terminal_status == "model_error"
    assert len(client.calls) == 2
    assert sleeps == []
    assert "I gathered evidence" in result.response_text
    wait_receipt = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_provider_retry_wait"
    )
    assert wait_receipt["action"] == "return_typed_retry_later_outcome"
    assert wait_receipt["wait_completed"] is False


def test_consecutive_incomplete_responses_deliver_visible_partial_answer() -> None:
    first_incomplete = StructuredToolTransportError(
        "Gemini Interactions returned an unsuccessful provider status.",
        decision={
            "provider": "gemini",
            "effective_api_surface": "interactions",
            "model": "gemini-3.7-flash",
            "provider_status": "incomplete",
            "provider_errors": [],
            "failure_kind": "provider_response_failed",
            "partial_response_available": False,
        },
    )
    partial_table = (
        "| Student | Expected end | Co-adviser | Topic |\n"
        "|---|---|---|---|\n"
        "| Student A | 2027 | Adviser B | Reliable agents |"
    )
    second_incomplete = StructuredToolTransportError(
        "Gemini Interactions returned an unsuccessful provider status.",
        decision={
            "provider": "gemini",
            "effective_api_surface": "interactions",
            "model": "gemini-3.7-flash",
            "provider_status": "incomplete",
            "provider_errors": [],
            "failure_kind": "provider_response_failed",
            "partial_response_available": True,
            "partial_response_char_count": len(partial_table),
        },
        partial_response=LLMResponse(
            text_response=partial_table,
            model="gemini-3.7-flash-20260815",
            usage={"input_tokens": 410, "output_tokens": 61, "total_tokens": 471},
            transport_metadata={
                "provider": "gemini",
                "provider_status": "incomplete",
                "response_complete": False,
            },
        ),
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-consecutive-incomplete",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="gemini",
                api_surface="interactions",
                model="gemini-3.7-flash",
                state_mode="stateless",
                input_items=[{"type": "user_input", "content": []}],
                output_items=[
                    {
                        "type": "function_call",
                        "id": "read-before-consecutive-incomplete",
                        "name": "turn_invoke_capability",
                        "arguments": {},
                    }
                ],
            ),
        ),
        first_incomplete,
        second_incomplete,
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "students": [{"name": "Student A", "expected_end": "2027"}],
            }
        ),
        prompt="Make a table of my current students.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-consecutive-incomplete-partial-delivery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "answer_partially_completed"
    assert result.response_text.startswith(
        "Partial answer — the model provider stopped before the response completed"
    )
    assert partial_table in result.response_text
    assert len(client.calls) == 3
    assert client.calls[2]["available_tools"] == []
    assert "continuation" not in client.calls[2]
    assert "tool_results" not in client.calls[2]
    assert "Produce a compact, complete answer" in client.calls[2][
        "system_message"
    ]
    assert len(_json_bytes(client.calls[2]["context"])) <= 64_512
    assert result.llm_usage == {
        "input_tokens": 410,
        "output_tokens": 61,
        "total_tokens": 471,
    }
    final_call = result.llm_calls[2]
    assert final_call["status"] == "failed"
    assert final_call["provider_request_sent"] is True
    assert final_call["partial_response_retained"] is True
    assert final_call["effective_model"] == "gemini-3.7-flash-20260815"
    assert final_call["usage"] == {
        "input_tokens": 410,
        "output_tokens": 61,
        "total_tokens": 471,
    }
    delivery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_partial_answer_delivery"
    )
    assert delivery["action"] == "deliver_visible_partial_response_with_caveat"
    assert delivery["provider_status"] == "incomplete"
    assert delivery["partial_response_char_count"] == len(partial_table)
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_transport_recovery"
    )
    assert recovery["context_compacted"] is False
    assert recovery["context_max_bytes"] == 64_000


def test_consecutive_incomplete_without_partial_response_remains_terminal() -> None:
    def incomplete() -> StructuredToolTransportError:
        return StructuredToolTransportError(
            "Gemini Interactions returned an unsuccessful provider status.",
            decision={
                "provider": "gemini",
                "provider_status": "incomplete",
                "failure_kind": "provider_response_failed",
                "partial_response_available": False,
            },
        )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-empty-incomplete",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current students"},
                    },
                )
            ],
        ),
        incomplete(),
        incomplete(),
        LLMResponse(text_response="A fourth call must not happen."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True, "students": []}),
        prompt="Make a table of my current students.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-consecutive-incomplete-without-partial",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_error"
    assert len(client.calls) == 3
    assert "StructuredToolTransportError" in result.response_text
    assert not any(
        item.get("type") == "adaptive_turn_partial_answer_delivery"
        for item in result.aux_llm_calls
    )


def test_structured_transport_failure_without_evidence_remains_terminal() -> None:
    client = _SequenceClient(
        StructuredToolTransportError(
            "Gemini Interactions returned an unsuccessful provider status.",
            decision={
                "provider": "gemini",
                "provider_status": "failed",
                "failure_kind": "provider_response_failed",
            },
        ),
        LLMResponse(text_response="This response must not be requested."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer without any prior evidence.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-provider-failure-without-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "model_error"
    assert result.llm_calls[0]["failure_kind"] == "provider_response_failed"
    assert result.llm_calls[0]["provider_status"] == "failed"
    assert not any(
        item.get("type") == "adaptive_turn_model_transport_recovery"
        for item in result.aux_llm_calls
    )


def test_rate_limit_without_evidence_does_not_wait_or_retry() -> None:
    client = _SequenceClient(
        StructuredToolTransportError(
            "Gemini temporarily rate-limited the structured-tool request.",
            decision={
                "provider": "gemini",
                "provider_status_code": 429,
                "failure_kind": "provider_rate_limited",
                "provider_request_sent": True,
                "retry_after_seconds": 43.424416244,
                "retry_after_source": "provider_message",
            },
        ),
        LLMResponse(text_response="This response must not be requested."),
    )
    sleeps: list[float] = []

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer without any prior evidence.",
        context=[],
        llm_client=client,
        model="gemini-3.7-flash",
        turn_id="turn-rate-limit-without-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    assert len(client.calls) == 1
    assert sleeps == []
    assert result.terminal_status == "model_error"
    assert "temporarily rate-limited" in result.response_text
    assert not any(
        item.get("type")
        in {
            "adaptive_turn_provider_retry_wait",
            "adaptive_turn_model_transport_recovery",
        }
        for item in result.aux_llm_calls
    )


def test_model_timeout_does_not_retry_without_changed_context() -> None:
    client = _SequenceClient(
        ToolCallError("OpenAI call failed: Request timed out."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="A short request.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-timeout-unchanged",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "model_error"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_liveness_recovery"
    )
    assert recovery["changed"] is False
    assert recovery["reason"] == "no_smaller_faithful_context"


def test_model_timeout_allows_only_one_changed_context_retry() -> None:
    client = _SequenceClient(
        _wrapped_model_timeout(),
        _wrapped_model_timeout(),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Keep the task while shedding old context.",
        context=[{"role": "assistant", "content": "old context " * 10_000}],
        llm_client=client,
        model="test-model",
        turn_id="turn-single-model-timeout-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 2
    assert result.terminal_status == "model_error"
    recoveries = [
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_liveness_recovery"
    ]
    assert [item["changed"] for item in recoveries] == [True, False]
    assert recoveries[-1]["reason"] == "single_fresh_context_retry_already_used"


@pytest.mark.parametrize(
    "error",
    [
        ToolCallError("OpenAI call failed: authentication rejected."),
        ValueError("request_timeout_seconds must be positive"),
        StructuredToolProtocolError(
            "Provider call lineage was unavailable after a deadline."
        ),
        ToolCallError("OpenAI call failed: provider returned 500."),
    ],
    ids=["authentication", "configuration", "protocol", "generic-provider"],
)
def test_non_liveness_model_failure_is_not_retried_after_compaction(
    error: Exception,
) -> None:
    client = _SequenceClient(
        error,
        LLMResponse(text_response="This response must not be requested."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Do not retry an authentication failure.",
        context=[{"role": "assistant", "content": "old context " * 10_000}],
        llm_client=client,
        model="test-model",
        turn_id="turn-non-liveness-model-failure",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "model_error"
    assert not any(
        item.get("type") == "adaptive_turn_model_liveness_recovery"
        for item in result.aux_llm_calls
    )


@pytest.mark.parametrize(
    "error",
    [
        ToolCallError(
            "OpenAI call failed: Error code: 429 - "
            "{'error': {'code': 'credit_balance_exhausted', "
            "'type': 'insufficient_quota'}}"
        ),
        ToolCallError("OpenAI call failed: quota_exhausted"),
    ],
)
def test_quota_exhaustion_returns_actionable_provider_message(
    error: Exception,
) -> None:
    result = execute_adaptive_turn(
        gateway=None,
        prompt="Represent this organisation.",
        context=[],
        llm_client=_SequenceClient(error),
        model="gpt-test",
        turn_id="turn-quota-exhausted",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_error"
    assert "credit_balance_exhausted / insufficient_quota" in result.response_text
    assert "Add credits" in result.response_text
    assert "choose another enabled model in Settings" in result.response_text
    assert "ToolCallError" not in result.response_text
    assert result.llm_calls[0]["failure_kind"] == "quota_exhausted"


def test_elapsed_thresholds_are_one_time_model_advisories_without_removing_tools() -> (
    None
):
    clock = _ManualClock()
    progress_events: list[dict[str, Any]] = []
    all_tool_names = {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    client = _TimedSequenceClient(
        clock,
        (
            6.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="after-research-advisory",
                        payload={},
                    )
                ],
            ),
        ),
        (
            8.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="after-answer-advisory",
                        payload={},
                    )
                ],
            ),
        ),
        (
            10.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="after-turn-advisory",
                        payload={},
                    )
                ],
            ),
        ),
        (10.2, LLMResponse(text_response="Completed after the advisory budget.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Keep working while progress is useful.",
        context=[],
        llm_client=client,
        model="test-model",
        model_parameters={"request_timeout_seconds": 7.0},
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="turn-elapsed-advisories",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Completed after the advisory budget."
    assert len(client.calls) == 4
    assert all(
        {tool.name for tool in call["available_tools"]} == all_tool_names
        for call in client.calls
    )
    assert all(
        "request_timeout_seconds" not in call["llm_params"] for call in client.calls
    )
    assert all("timeout_seconds" not in call["llm_params"] for call in client.calls)
    assert all(call["request_timeout_seconds"] is None for call in result.llm_calls)
    assert all(call["request_advisory_seconds"] >= 7.0 for call in result.llm_calls)
    advisory_events = [
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_elapsed_time_advisory"
    ]
    assert [item["advisory_kind"] for item in advisory_events] == [
        "research_interval",
        "answer_reserve",
        "turn_budget",
    ]
    assert all(item["capabilities_removed"] is False for item in advisory_events)
    system_messages = [call["system_message"] for call in client.calls]
    for notice in (
        "The planned research interval has elapsed.",
        "The planned final-answer reserve has begun.",
        "The planned turn budget has elapsed.",
    ):
        assert sum(notice in message for message in system_messages) == 1
    progress_advisories = [
        event
        for event in progress_events
        if event.get("stage") == "elapsed_time_advisory"
    ]
    assert len(progress_advisories) == 3
    assert [item["call_id"] for item in result.tool_invocations] == [
        "after-research-advisory",
        "after-answer-advisory",
        "after-turn-advisory",
    ]


def test_model_result_returned_after_turn_advisory_remains_usable() -> None:
    clock = _ManualClock()
    progress_events: list[dict[str, Any]] = []
    client = _LateResponseClient(
        clock,
        0.2,
        LLMResponse(text_response="A useful answer after the advisory."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer when ready.",
        context=[],
        llm_client=client,
        model="test-model",
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="turn-late-model-result",
        turn_budget_seconds=0.05,
        final_synthesis_reserve_seconds=0.01,
        clock=clock,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "A useful answer after the advisory."
    assert result.llm_calls[0]["status"] == "completed"
    allocation = next(
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_budget_allocation"
    )
    assert allocation["enforcement"] == "advisory"
    advisories = [
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_elapsed_time_advisory"
    ]
    assert [event["advisory_kind"] for event in advisories] == [
        "research_interval",
        "answer_reserve",
        "turn_budget",
    ]
    assert (
        len(
            [
                event
                for event in progress_events
                if event.get("stage") == "elapsed_time_advisory"
            ]
        )
        == 3
    )


def test_model_call_duration_is_advisory_and_late_result_is_retained() -> None:
    clock = _ManualClock()
    client = _LateResponseClient(
        clock,
        2.0,
        LLMResponse(text_response="Useful result after the model advisory."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer when the model has finished.",
        context=[],
        llm_client=client,
        model="test-model",
        model_parameters={"request_timeout_seconds": 1.0},
        turn_id="turn-model-call-advisory",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        clock=clock,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Useful result after the model advisory."
    assert "request_timeout_seconds" not in client.calls[0]["llm_params"]
    assert result.llm_calls[0]["request_timeout_seconds"] is None
    assert result.llm_calls[0]["request_advisory_seconds"] == 1.0
    assert result.llm_calls[0]["advisory_budget_exceeded"] is True
    advisory = next(
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_model_call_advisory"
    )
    assert advisory["result_retained"] is True


def test_elapsed_advisory_preserves_native_continuation_and_bounded_evidence() -> None:
    started = time.monotonic()
    clock = _ManualClock(started)
    raw_tail = "z" * 50_000

    def handler(**_kwargs: Any) -> dict[str, Any]:
        clock.now = started + 8.5
        return {
            "success": True,
            "answer": "usable evidence",
            "raw_tail": raw_tail,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-advisory-evidence",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "the useful thing"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-1",
            ),
        ),
        LLMResponse(text_response="The final answer uses usable evidence."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(handler),
        prompt="Research this and answer.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-advisory-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=0,
        clock=clock,
    )

    assert result.response_text == "The final answer uses usable evidence."
    assert len(client.calls) == 2
    final_call = client.calls[1]
    assert {tool.name for tool in final_call["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    assert final_call["continuation"].response_id == "response-1"
    assert len(final_call["tool_results"]) == 1
    tool_output = final_call["tool_results"][0].output
    assert tool_output["call_id"] == "call-advisory-evidence"
    assert "usable evidence" in tool_output["preview"]
    assert raw_tail not in json.dumps(tool_output)
    assert "The planned research interval has elapsed." in (
        final_call["system_message"]
    )


@pytest.mark.parametrize(
    "native_continuation",
    [True, False],
    ids=["native-continuation", "stateless-context"],
)
def test_hydrated_evidence_survives_research_advisory_for_provider_styles(
    native_continuation: bool,
) -> None:
    started = time.monotonic()
    research_advisory_at = started + 8.0
    clock = _ManualClock(started)
    client = _HydrationAdvisoryClient(
        clock,
        native_continuation=native_continuation,
        research_advisory_at=research_advisory_at,
    )
    raw_tail = "z" * 50_000

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "record": {
                    "a_raw": raw_tail,
                    "z_summary": (
                        "The specifically hydrated result remains available."
                    ),
                },
            }
        ),
        prompt="Research the record and answer.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"turn-hydration-{'native' if native_continuation else 'stateless'}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=0,
        clock=clock,
    )

    assert result.response_text == (
        "The final answer uses the explicitly hydrated evidence."
    )
    assert len(client.calls) == 3
    assert len(client.received_evidence_outputs) == 2
    delivered_envelope, delivered_slice = client.received_evidence_outputs
    assert delivered_envelope["schema_version"] == "turn_evidence_envelope.v1"
    assert delivered_slice["schema_version"] == "turn_evidence_slice.v1"
    assert delivered_slice["content"] == (
        "The specifically hydrated result remains available."
    )

    final_call = client.calls[-1]
    assert {tool.name for tool in final_call["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    assert "The planned research interval has elapsed." in (
        final_call["system_message"]
    )
    if native_continuation:
        assert final_call["continuation"].response_id == "response-hydration"
        assert final_call["tool_results"][0].output == delivered_slice
    else:
        assert "continuation" not in final_call
        tool_messages = [
            item for item in final_call["context"] if item.get("role") == "tool"
        ]
        assert json.loads(tool_messages[-1]["content"]) == delivered_slice
    assert raw_tail not in json.dumps(final_call, default=str)


def test_many_read_results_share_one_model_context_budget() -> None:
    calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"call-{index}",
            payload={
                "name": "general_read",
                "arguments": {"query": f"query-{index}"},
            },
        )
        for index in range(100)
    ]
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=calls,
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-many",
            ),
        ),
        LLMResponse(text_response="Synthesised from the bounded evidence handles."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **kwargs: {
                "success": True,
                "query": kwargs.get("query"),
                "raw": "x" * 10_000,
            }
        ),
        prompt="Use all of these independent reads.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-many-results",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Synthesised from the bounded evidence handles."
    model_results = client.calls[1]["tool_results"]
    serialised_results = json.dumps(
        [
            {
                "call_id": item.call_id,
                "tool_name": item.tool_name,
                "status": item.status,
                "output": item.output,
            }
            for item in model_results
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(serialised_results) <= 24_000
    assert len(model_results) == 100
    assert all(
        isinstance(item.output, dict) and item.output.get("evidence_id")
        for item in model_results
    )
    assert (
        len(
            json.dumps(
                list(result.evidence_index),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= 24_000
    )


def test_correlation_shell_overflow_switches_to_bounded_final_synthesis() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id=f"catalogue-call-{index}",
                    payload={"offset": index},
                )
                for index in range(500)
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-overflow",
            ),
        ),
        LLMResponse(
            text_response=(
                "I stopped the oversized continuation and answered from the "
                "bounded evidence context."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Exercise a mechanically oversized tool-call batch.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-correlation-shell-overflow",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert len(client.calls) == 2
    assert "continuation" not in client.calls[1]
    assert "tool_results" not in client.calls[1]
    assert {tool.name for tool in client.calls[1]["available_tools"]} == {
        "turn_list_evidence",
        "turn_read_evidence",
    }
    overflow = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_tool_result_batch_overflow"
    )
    assert overflow["tool_call_count"] == 500
    assert overflow["action"] == "fresh_final_synthesis_with_pageable_evidence_index"


def test_trusted_gmail_profile_overrides_model_profile_and_aliases() -> None:
    seen_arguments: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {"success": True, "messages": []}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-gmail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {
                            "profile": "model-chosen-profile",
                            "profile_id": "model-chosen-alias",
                            "identity": "another-model-alias",
                            "query": "newer_than:7d",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="There are no matching messages."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_gateway(handler),
        prompt="Check my recent mail.",
        context=[],
        llm_client=client,
        model="test-model",
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": "trusted-represented-profile",
        },
        turn_id="turn-gmail-profile",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "There are no matching messages."
    assert seen_arguments == {
        "profile": "trusted-represented-profile",
        "query": "newer_than:7d",
    }
    invocation = result.tool_invocations[0]
    assert invocation["payload"]["arguments"]["profile"] == "model-chosen-profile"
    assert invocation["effective_arguments"] == seen_arguments


def test_explicit_request_guard_allows_actor_bound_gmail_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import write_tool_request_evidence_vontology_service

    inferred: dict[str, Any] = {}
    seen_arguments: dict[str, Any] = {}

    def infer_request_evidence(**kwargs: Any):
        inferred.update(kwargs)
        return (
            {
                "gmail_send_message": {
                    "schema_version": "write_tool_request_evidence.v1",
                    "tool_name": "gmail_send_message",
                    "request_state": "explicit_request",
                    "confirmation_state": "low_confidence",
                    "denial_state": "low_confidence",
                    "rationale": "The current prompt explicitly asks Von to send email.",
                }
            },
            {
                "schema_version": "write_tool_request_evidence.v1",
                "status": "ok",
                "parse_mode": "json",
            },
        )

    monkeypatch.setattr(
        write_tool_request_evidence_vontology_service,
        "infer_write_tool_request_evidence",
        infer_request_evidence,
    )
    monkeypatch.setattr(
        "src.backend.workflows.write_tool_policy."
        "resolve_runtime_profile_write_tool_risk_class",
        lambda *_args, **_kwargs: "external_non_vontology",
    )
    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "resolve_tool_projection_contract",
        lambda _tool_name: None,
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service."
        "get_user_mutation_authority_level",
        lambda _user_id: "external_system_guarded",
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service."
        "get_global_mutation_authority_level",
        lambda: "external_system_guarded",
    )

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "message_id": "gmail-message-1",
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="send-gmail",
                    payload={
                        "name": "gmail_send_message",
                        "arguments": {
                            "profile": "#V#gmail_profile_zhan",
                            "profile_id": "foreign-runtime",
                            "to": "recipient@example.test",
                            "subject": "Requested note",
                            "body_text": "The requested body.",
                            "allow_send": False,
                            "allow_mutation": False,
                            "namespace": "#V#spoof@other",
                            "acting_user_concept_id": "#V#spoof",
                            "organisation_concept_id": "#V#other",
                            "request_id": "spoofed-request",
                            "idempotency_scope": "spoofed-idempotency-scope",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The requested email was submitted."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_send_gateway(handler),
        prompt="Send this email now.",
        context=[
            {"role": "user", "content": "Draft a short note first."},
            {"role": "assistant", "content": "Here is a draft."},
        ],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        trusted_argument_values={
            "gmail_profile": _gmail_profile_choices(
                default="#V#gmail_profile_zhan"
            ),
        },
        turn_id="turn-send-gmail",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert seen_arguments == {
        "profile": "zhan-runtime",
        "to": ["recipient@example.test"],
        "subject": "Requested note",
        "body_text": "The requested body.",
        "allow_send": True,
        "namespace": "#V#person@org",
        "acting_user_concept_id": "#V#person",
        "organisation_concept_id": "#V#org",
        "request_id": "turn-send-gmail",
        "idempotency_scope": "turn-send-gmail",
    }
    assert inferred["prompt"] == "Send this email now."
    assert inferred["recent_user_prompts"] == ["Draft a short note first."]
    assert inferred["requested_tools"] == ["gmail_send_message"]
    payload_summary = inferred["requested_tool_payloads"]["gmail_send_message"]
    assert set(payload_summary) == {"provided_fields"}
    assert "The requested body." not in json.dumps(payload_summary)
    invocation = result.tool_invocations[0]
    assert invocation["effect_status"] == "succeeded"
    assert invocation["effective_arguments"] == {
        **seen_arguments,
        "to": "recipient@example.test",
    }
    guardrail_event = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "ordinary_turn_effect_request_guardrail"
    )
    assert guardrail_event["status"] == "allowed"
    assert guardrail_event["request_state"] == "explicit_request"


def test_gmail_send_fails_closed_before_dispatch_when_request_evidence_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import (
        turn_execution_record_service,
        write_tool_request_evidence_vontology_service,
    )

    dispatches: list[dict[str, Any]] = []
    handler_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        lambda **kwargs: dispatches.append(dict(kwargs)) or {"updated": True},
    )
    monkeypatch.setattr(
        write_tool_request_evidence_vontology_service,
        "infer_write_tool_request_evidence",
        lambda **_kwargs: (
            {},
            {
                "schema_version": "write_tool_request_evidence.v1",
                "status": "llm_unavailable",
            },
        ),
    )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="blocked-gmail-send",
                    payload={
                        "name": "gmail_send_message",
                        "arguments": {
                            "to": "recipient@example.test",
                            "subject": "Not dispatched",
                            "body_text": "No message must be sent.",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="I could not verify permission to send."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_send_gateway(
            lambda **kwargs: handler_calls.append(dict(kwargs))
            or {"success": True}
        ),
        prompt="Please send this email.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        trusted_argument_values={
            "gmail_profile": _gmail_profile_choices(
                default="#V#gmail_profile_zhan"
            ),
        },
        turn_id="turn-blocked-gmail-send",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert handler_calls == []
    assert dispatches == []
    invocation = result.tool_invocations[0]
    assert invocation["effect_status"] == "not_started"
    denial = json.loads(invocation["evidence"]["preview"])
    assert denial["error_code"] == "ordinary_turn_request_evidence_unavailable"
    assert denial["changed"] is False
    guardrail_event = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "ordinary_turn_effect_request_guardrail"
    )
    assert guardrail_event["status"] == "blocked"
    assert guardrail_event["reason"] == "request_evidence_unavailable"


def test_explicit_gmail_request_does_not_override_actor_mutation_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import (
        adaptive_turn_service,
        write_tool_request_evidence_vontology_service,
    )

    monkeypatch.setattr(
        write_tool_request_evidence_vontology_service,
        "infer_write_tool_request_evidence",
        lambda **_kwargs: (
            {
                "gmail_send_message": {
                    "schema_version": "write_tool_request_evidence.v1",
                    "tool_name": "gmail_send_message",
                    "request_state": "explicit_request",
                    "confirmation_state": "low_confidence",
                    "denial_state": "low_confidence",
                    "rationale": "The current prompt asks to send email.",
                }
            },
            {"schema_version": "write_tool_request_evidence.v1", "status": "ok"},
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service."
        "get_user_mutation_authority_level",
        lambda _user_id: "read_only",
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service."
        "get_global_mutation_authority_level",
        lambda: "external_system_guarded",
    )
    monkeypatch.setattr(
        "src.backend.workflows.write_tool_policy."
        "resolve_runtime_profile_write_tool_risk_class",
        lambda *_args, **_kwargs: "external_non_vontology",
    )

    denial, event = adaptive_turn_service._ordinary_turn_effect_request_guardrail(
        definition=SimpleNamespace(
            write_guardrail={"ordinary_turn_explicit_request": True}
        ),
        capability_name="gmail_send_message",
        arguments={
            "profile": "zhan-runtime",
            "to": "recipient@example.test",
            "subject": "Requested note",
            "body_text": "Requested body",
            "allow_send": True,
        },
        prompt="Send this email now.",
        context=[],
        llm_client=object(),
        model="test-model",
        user_concept_id="#V#person",
    )

    assert denial is not None
    assert denial["error_code"] == "ordinary_turn_write_policy_blocked"
    assert denial["status"] == "not_started"
    assert denial["changed"] is False
    assert event is not None
    assert event["status"] == "blocked"
    assert event["effective_mutation_authority"] == "read_only"


def test_model_cannot_select_gmail_profile_without_a_trusted_binding() -> None:
    seen_arguments: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {"success": True, "messages": []}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="call-model-gmail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {
                            "user_id": "model-selected-profile",
                            "query": "newer_than:1d",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="No authorised Gmail profile was available."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_gateway(handler),
        prompt="Check the selected mail profile.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-gmail-profile",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "No authorised Gmail profile was available."
    assert seen_arguments == {}
    assert result.tool_invocations[0]["status"] == "error"
    assert (
        result.tool_invocations[0]["effective_payload"]["error_code"]
        == "capability_not_delegated"
    )


def test_current_conversation_history_read_cannot_be_redirected_by_model() -> None:
    seen_arguments: dict[str, Any] = {}
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="chat_history_get_segments",
            handler=lambda **kwargs: seen_arguments.update(kwargs)
            or {"success": True, "segments": []},
            input_schema=Schema(
                optional={
                    "conversation_ref": (dict, type(None)),
                    "session_id": (str, type(None)),
                    "namespace": (str, type(None)),
                    "user_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "include_debug": bool,
                    "segment_size": int,
                },
                allow_unknown=False,
            ),
            category="read",
            ordinary_turn_trusted_argument_bindings={
                "session_id": "conversation_id",
                "namespace": "turn_namespace",
                "user_concept_id": "actor_user_concept_id",
                "organisation_concept_id": "actor_organisation_concept_id",
            },
            ordinary_turn_fixed_arguments={
                "conversation_ref": None,
                "include_debug": False,
            },
            description="Read only the active conversation carrier.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-current-carrier",
                    payload={
                        "name": "chat_history_get_segments",
                        "arguments": {
                            "conversation_ref": {
                                "session_id": "foreign-conversation"
                            },
                            "session_id": "foreign-conversation",
                            "namespace": "#V#foreign@foreign_org",
                            "user_concept_id": "#V#foreign",
                            "organisation_concept_id": "#V#foreign_org",
                            "include_debug": True,
                            "segment_size": 10,
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The active conversation carrier was read."),
    )

    execute_adaptive_turn(
        gateway=gateway,
        prompt="Recover the exact table from this conversation.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-read-current-carrier",
        conversation_id="current-conversation",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert seen_arguments == {
        "conversation_ref": None,
        "session_id": "current-conversation",
        "namespace": "#V#person@org",
        "user_concept_id": "#V#person",
        "organisation_concept_id": "#V#org",
        "include_debug": False,
        "segment_size": 10,
    }


def test_represented_workflow_is_discovered_and_invoked_as_bound_capability(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    seen_arguments: dict[str, Any] = {}
    progress_events: list[dict[str, Any]] = []
    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_turn_test",
        workflow_id="#V#represented_test_workflow",
        display_name="Represented test workflow",
        description="Produce the represented test work product.",
        relevance_score=0.94,
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
        outcome_explanation_prompt_support={
            "schema_version": "workflow_outcome_explanation_support.v1",
            "workflow_id": "#V#represented_test_workflow",
            "source": "text_relation:#V#hasWorkflowOutcomeExplanationPromptMapJson",
            "prompts": {
                "succeeded": {
                    "prompt_concept_id": "#V#prompt_test_success_explanation",
                    "prompt_text": "Explain only the canonically verified result.",
                }
            },
        },
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                }
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    def _execute_workflow(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {
            "success": True,
            "instance_id": "workflow-instance-1",
            "created_new": True,
            "final_status": "completed",
            "workflow_execution": {
                "final_status": "completed",
                "current_state": "record_description",
                "latest_step_result_envelope": {
                    "schema_version": "workflow_step_result_envelope.v1",
                    "workflow_id": "#V#represented_test_workflow",
                    "state_id": "record_description",
                    "action_id": "upsert_research_description",
                    "action_status": "success",
                    "action_outcome": "success",
                    "state_attempt": 1,
                    "diagnostics": {"duration_ms": 125},
                    "progress_facts": [
                        {
                            "schema_version": "workflow_progress_projection.v1",
                            "fact_id": "student_name",
                            "label": "Student",
                            "status": "available",
                            "present": True,
                            "visibility": "default",
                            "value": "Nathan Doe",
                            "source_path": "context.student_name",
                        },
                        {
                            "schema_version": "workflow_progress_projection.v1",
                            "fact_id": "description_date",
                            "label": "Description date",
                            "status": "available",
                            "present": True,
                            "visibility": "default",
                            "value": "2026-08-05",
                            "source_path": "context.description_date",
                        },
                    ],
                },
            },
        }

    gateway = _workflow_gateway(
        _execute_workflow,
        hard_timeout_enabled=False,
    )
    assert "workflow_execute" not in ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#real_user",
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-workflow",
                    payload={"query": "produce the represented work product"},
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="workflow-discovery-response",
            ),
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {
                            "workflow_id": "#V#spoofed_workflow",
                            "user_id": "#V#spoofed_user",
                            "inputs": {
                                "record_id": "#V#record",
                                "user_concept_id": "#V#spoofed_user",
                            },
                            "timeout_seconds": 180.0,
                        },
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="workflow-invocation-response",
            ),
        ),
        LLMResponse(text_response="The represented work product was completed."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Produce the represented work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        workflow_launch_inputs={"authorised_record": "#V#request_record"},
        conversation_situation=(
            "The represented record currently under discussion is #V#record."
        ),
        turn_id="turn-represented-workflow",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )

    catalogue_result = client.calls[1]["tool_results"][0].output
    assert catalogue_result["represented_workflow_total"] == 1
    assert "Explain only the canonically verified result." not in json.dumps(
        catalogue_result
    )
    workflow_purpose = next(
        item
        for item in catalogue_result["purpose_index"]["entries"]
        if item["name"] == workflow_capability.name
    )
    assert workflow_purpose["shape"] == "represented_workflow"
    assert (
        catalogue_result["selection_policy"]["semantic_adequacy_owner"]
        == "adaptive_model"
    )
    assert seen_arguments["workflow_id"] == "#V#represented_test_workflow"
    assert seen_arguments["user_id"] == "#V#real_user"
    assert seen_arguments["org_id"] == "#V#real_org"
    assert seen_arguments["namespace"] == "#V#real_user@real_org"
    assert seen_arguments["inputs"]["user_concept_id"] == "#V#real_user"
    assert seen_arguments["inputs"]["record_id"] == "#V#record"
    assert seen_arguments["inputs"]["authorised_record"] == "#V#request_record"
    assert seen_arguments["inputs"]["conversation_situation"] == (
        "The represented record currently under discussion is #V#record."
    )
    assert seen_arguments["source_event_type"] == "conversation_turn"
    assert seen_arguments["source_event_id"] == "turn-represented-workflow"
    assert seen_arguments["timeout_seconds"] == 90.0
    assert seen_arguments["event_idempotency_key"].startswith(
        "conversation_turn_workflow:"
    )
    invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert invocation["tool"] == workflow_capability.name
    assert invocation["execution_method"] == "workflow_execute"
    assert invocation["capability_kind"] == "represented_workflow"
    assert invocation["capability_display_name"] == "Represented test workflow"
    assert invocation["represented_workflow_id"] == ("#V#represented_test_workflow")
    assert invocation["effect_status"] == "succeeded"
    assert invocation["changed"] is True
    assert invocation["instance_id"] == "workflow-instance-1"
    assert invocation["workflow_id"] == "#V#represented_test_workflow"
    assert invocation["plan_profile"]["shape"] == "represented_workflow"
    assert invocation["workflow_progress_evidence"]["facts"] == [
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "student_name",
            "label": "Student",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "source_path": "context.student_name",
            "payload_source_path": (
                "workflow_execution.latest_step_result_envelope.progress_facts"
            ),
            "visibility": "default",
            "value": "Nathan Doe",
        },
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "description_date",
            "label": "Description date",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "source_path": "context.description_date",
            "payload_source_path": (
                "workflow_execution.latest_step_result_envelope.progress_facts"
            ),
            "visibility": "default",
            "value": "2026-08-05",
        },
    ]
    invocation_model_result = client.calls[2]["tool_results"][0].output
    assert invocation_model_result["outcome_explanation_support"] == {
        "schema_version": "workflow_outcome_explanation_support.v1",
        "workflow_id": "#V#represented_test_workflow",
        "outcome_key": "succeeded",
        "matched_prompt_key": "succeeded",
        "prompt_concept_id": "#V#prompt_test_success_explanation",
        "prompt_text": "Explain only the canonically verified result.",
        "source": "text_relation:#V#hasWorkflowOutcomeExplanationPromptMapJson",
        "role": "supplemental_explanation_guidance",
        "evidence_boundary": (
            "This represented prompt is guidance for explaining independently "
            "observed execution evidence. It is not evidence that the workflow "
            "ran, succeeded, failed, changed canonical state, or has authority."
        ),
    }
    selection_trace = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_capability_selection"
    )
    assert selection_trace["capability_name"] == workflow_capability.name
    assert selection_trace["selection_policy"]["representedness_priority"] is False
    assert selection_trace["plan_profile"]["shape"] == ("represented_workflow")
    workflow_events = [
        event for event in progress_events if event.get("call_id") == "invoke-workflow"
    ]
    assert [event["event_kind"] for event in workflow_events] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert [event["subtask"] for event in workflow_events] == [
        "Represented test workflow",
        "Represented test workflow",
    ]
    assert workflow_events[0]["result_summary"] == ("Using Represented test workflow.")
    assert workflow_events[1]["result_summary"] == (
        "Finished Represented test workflow."
    )
    for event in workflow_events:
        semantic_operation = event["semantic_operation"]
        assert semantic_operation["capability"]["id"] == workflow_capability.name
        assert semantic_operation["capability"]["label"] == (
            "Represented test workflow"
        )
        assert semantic_operation["arguments"][0]["value"] == (
            "#V#represented_test_workflow"
        )
    start_execution = workflow_events[0]["selected_workflow_execution_event"]
    assert start_execution == {
        "schema_version": "selected_workflow_execution_event.v1",
        "status": "workflow_execution_start",
        "event_kind": "workflow_execution_start",
        "workflow_id": "#V#represented_test_workflow",
        "selected_workflow_id": "#V#represented_test_workflow",
        "selected_workflow_name": "Represented test workflow",
        "selected_execution_mode": "adaptive_turn_capability",
    }
    completed_execution = workflow_events[1]["selected_workflow_execution_event"]
    assert completed_execution["status"] == "workflow_execution_complete"
    assert completed_execution["state_id"] == "record_description"
    assert completed_execution["action_id"] == "upsert_research_description"
    assert completed_execution["action_outcome"] == "success"
    assert completed_execution["effect_status"] == "succeeded"
    assert completed_execution["semantic_effect"] is True
    assert [fact["label"] for fact in completed_execution["progress_facts"]] == [
        "Student",
        "Description date",
    ]
    assert workflow_events[1]["progress_facts"] == completed_execution["progress_facts"]
    assert result.response_text == "The represented work product was completed."


def test_represented_workflow_failure_progress_is_actionable() -> None:
    from src.backend.services.adaptive_turn_service import (
        _build_represented_workflow_execution_event,
    )

    event, progress_evidence = _build_represented_workflow_execution_event(
        payload={
            "final_status": "failed",
            "effect_status": "failed",
            "semantic_effect": True,
            "changed": False,
            "mutation_outcome": "partial",
            "outcome_finality": "terminal_for_turn",
            "recovery_affordances": [{"action_type": "inspect_workflow_instance"}],
            "workflow_execution": {
                "current_state": "extract_attachment",
                "error": "Attachment text extraction failed.",
                "latest_step_result_envelope": {
                    "state_id": "extract_attachment",
                    "action_id": "extract_pdf_text",
                    "action_status": "failed",
                    "action_outcome": "failure",
                    "diagnostics": {
                        "error": "The attached PDF could not be read.",
                        "duration_ms": 430,
                    },
                    "progress_facts": [
                        {
                            "schema_version": "workflow_progress_projection.v1",
                            "fact_id": "attachment_name",
                            "label": "Attachment",
                            "status": "available",
                            "present": True,
                            "visibility": "default",
                            "value": "research-description.pdf",
                            "source_path": "context.attachment_name",
                        }
                    ],
                },
            },
        },
        workflow_id="#V#student_research_description_workflow",
        workflow_name="Student research description workflow",
    )

    assert event["status"] == "workflow_execution_failed"
    assert event["state_id"] == "extract_attachment"
    assert event["action_id"] == "extract_pdf_text"
    assert event["action_outcome"] == "failure"
    assert event["error"] == "The attached PDF could not be read."
    assert event["effect_status"] == "failed"
    assert event["semantic_effect"] is True
    assert event["changed"] is False
    assert event["next_action"] == "Inspect workflow instance"
    assert event["progress_facts"][0]["label"] == "Attachment"
    assert progress_evidence is not None
    assert progress_evidence["facts"][0]["value"] == ("research-description.pdf")


def test_represented_workflow_failure_projects_typed_nested_cause() -> None:
    from src.backend.services.adaptive_turn_service import (
        _build_represented_workflow_execution_event,
    )

    event, _ = _build_represented_workflow_execution_event(
        payload={
            "final_status": "failed",
            "effect_status": "failed",
            "workflow_execution": {
                "latest_step_result_envelope": {
                    "output_payload": {
                        "mcp_result": {
                            "error_code": "arxiv_acquisition_unavailable",
                            "error": (
                                "RuntimeError: asyncio lock is bound to a "
                                "different event loop"
                            ),
                            "private_payload": "must not be projected",
                        }
                    }
                }
            },
        },
        workflow_id="#V#arxiv_paper_representation_workflow",
        workflow_name="ArXiv paper representation workflow",
    )

    assert event["error_code"] == "arxiv_acquisition_unavailable"
    assert event["error"] == (
        "RuntimeError: asyncio lock is bound to a different event loop"
    )
    assert "private_payload" not in event


def test_represented_workflow_nonfinite_wait_is_typed_not_started_feedback(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_invalid_wait_test",
        workflow_id="#V#represented_invalid_wait_workflow",
        display_name="Represented invalid-wait workflow",
        description="Produce a represented work product.",
        relevance_score=0.94,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    def _unexpected_execution(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid wait arguments must not submit a workflow")

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-invalid-wait-workflow",
                    payload={"query": "produce the represented work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-invalid-wait-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"timeout_seconds": float("inf")},
                    },
                )
            ],
        ),
        LLMResponse(text_response="I corrected the invalid wait without an effect."),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            _unexpected_execution,
            hard_timeout_enabled=False,
        ),
        prompt="Produce the represented work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-invalid-wait",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert invocation["error_code"] == ("invalid_workflow_capability_arguments")
    assert invocation["effect_status"] == "not_started"
    assert invocation["changed"] is False
    assert invocation["mutation_outcome"] == "not_started"
    assert result.terminal_status == "completed"


@pytest.mark.parametrize(
    (
        "read_workflow_id",
        "read_status",
        "expected_effect_status",
        "expected_terminal_status",
        "expected_fallback",
    ),
    [
        (
            "#V#represented_readback_workflow",
            "completed",
            "succeeded",
            "completed",
            False,
        ),
        (
            "#V#represented_readback_workflow",
            "running",
            "partial",
            "effect_partially_completed",
            True,
        ),
        (
            "#V#represented_readback_workflow",
            "failed",
            "failed",
            "effect_failed",
            True,
        ),
        (
            "#V#different_workflow",
            "completed",
            "partial",
            "effect_partially_completed",
            True,
        ),
    ],
    ids=["completed", "non-terminal", "failed", "workflow-mismatch"],
)
def test_workflow_instance_readback_reconciles_only_exact_terminal_effect(
    monkeypatch,
    read_workflow_id: str,
    read_status: str,
    expected_effect_status: str,
    expected_terminal_status: str,
    expected_fallback: bool,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_readback_test",
        workflow_id="#V#represented_readback_workflow",
        display_name="Represented read-back workflow",
        description="Produce a durable work product and verify its terminal state.",
        relevance_score=0.95,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {"type": "object", "additionalProperties": True},
                "timeout_seconds": {"type": "number"},
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    instance_reads: list[dict[str, Any]] = []

    def _execute_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": "workflow-instance-readback-1",
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "running",
            "timed_out": True,
            "workflow_execution": {
                "timeout_seconds": 90.0,
                "poll_interval_seconds": 0.5,
            },
        }

    def _read_instance(**kwargs: Any) -> dict[str, Any]:
        instance_reads.append(dict(kwargs))
        return {
            "success": True,
            "instance_id": "workflow-instance-readback-1",
            "workflow_id": read_workflow_id,
            "status": read_status,
            "outputs": {"verified": True},
            "timed_out": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-readback-workflow",
                    payload={"query": "produce and verify the durable work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-readback-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"timeout_seconds": 90.0},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="await-readback-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {
                            "instance_id": "workflow-instance-readback-1",
                            "await_terminal": True,
                            "timeout_seconds": 90.0,
                            "poll_interval_seconds": 0.5,
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="repeat-readback-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {
                            "instance_id": "workflow-instance-readback-1",
                            "await_terminal": True,
                            "timeout_seconds": 90.0,
                            "poll_interval_seconds": 0.5,
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The durable work product was verified."),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            _execute_workflow,
            hard_timeout_enabled=False,
            instance_handler=_read_instance,
        ),
        prompt="Produce and verify the durable work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-readback",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    expected_instance_read = {
        "instance_id": "workflow-instance-readback-1",
        "await_terminal": True,
        "timeout_seconds": 90.0,
        "poll_interval_seconds": 0.5,
    }
    assert instance_reads == [
        expected_instance_read,
        expected_instance_read,
    ]
    workflow_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert workflow_invocation["effect_status"] == expected_effect_status
    if expected_effect_status in {"succeeded", "failed"}:
        canonical_readback = workflow_invocation["canonical_readback"]
        assert {
            key: canonical_readback.get(key)
            for key in ("capability", "instance_id", "workflow_id", "status")
        } == {
            "capability": "workflow_get_instance",
            "instance_id": "workflow-instance-readback-1",
            "workflow_id": workflow_capability.workflow_id,
            "status": read_status,
        }
        assert isinstance(canonical_readback.get("evidence_id"), str)
    else:
        assert "canonical_readback" not in workflow_invocation
    assert result.terminal_status == expected_terminal_status
    expected_authority = "canonical_outcome" if expected_fallback else "model"
    assert result.response_authority == expected_authority
    if expected_authority == "canonical_outcome":
        assert result.response_text != "The durable work product was verified."
        assert "workflow-instance-readback-1" in result.response_text
        outcome_report = next(
            item
            for item in result.aux_llm_calls
            if item.get("type") == "adaptive_turn_effect_outcome_report"
        )
        workflow_fact = next(
            fact
            for fact in outcome_report["facts"]
            if fact.get("workflow_id") == workflow_capability.workflow_id
        )
        exact_failed_readback = (
            read_workflow_id == workflow_capability.workflow_id
            and read_status == "failed"
        )
        assert (
            workflow_fact["workflow_instance_readback_verified"]
            is exact_failed_readback
        )
        if exact_failed_readback:
            authoritative_screen = result.response_text.split(
                "\n\n### Model draft (non-authoritative)",
                maxsplit=1,
            )[0]
            assert workflow_fact["target_ids"] == []
            assert workflow_fact["workflow_instance_terminal_status"] == "failed"
            assert workflow_fact["evidence_id"] == canonical_readback["evidence_id"]
            assert authoritative_screen.count("workflow-instance-readback-1") == 1
            assert "target `workflow-instance-readback-1`" not in authoritative_screen
            assert "the current target was read back exactly" not in authoritative_screen
            assert "confirms only its operational status" in authoritative_screen
            assert "not the requested work product" in authoritative_screen
            assert result.canonical_outcome_spoken_text is not None
            assert "workflow instance" in result.canonical_outcome_spoken_text
            assert "failed" in result.canonical_outcome_spoken_text
            assert "confirms only its operational status" in (
                result.canonical_outcome_spoken_text
            )
            assert "not the requested work product" in (
                result.canonical_outcome_spoken_text
            )
    else:
        assert result.response_text == "The durable work product was verified."


def test_completed_workflow_instance_report_does_not_verify_domain_targets() -> None:
    instance_id = "workflow-instance-completed-report-1"
    workflow_id = "#V#scholarly_article_metadata_representation_workflow"
    domain_target_id = "#V#article_domain_target"
    screen_text, report = _build_effect_outcome_report(
        terminal_status="model_error",
        effect_snapshot={
            "effect-workflow-completed": {
                "turn_finality_required": True,
                "effect_status": "succeeded",
                "changed": True,
                "initial_effect_status": "partial",
                "current_outcome_status": "succeeded",
                "outcome_resolved": True,
                "reconciliation_status": "canonically_verified",
                "reconciliation_basis": "workflow_instance_terminal_read",
                "reconciliation_evidence_id": "evidence-workflow-readback",
                "canonical_readback": {
                    "capability": "workflow_get_instance",
                    "instance_id": instance_id,
                    "workflow_id": workflow_id,
                    "status": "completed",
                    "evidence_id": "evidence-workflow-readback",
                },
                "workflow_id": workflow_id,
                "instance_id": instance_id,
                "result_target_ids": [instance_id, domain_target_id],
            }
        },
        tool_invocations=[
            {
                "effect_id": "effect-workflow-completed",
                "tool": "scholarly_article_metadata_representation",
                "capability_display_name": (
                    "Scholarly Article Metadata Representation Workflow"
                ),
                "result_target_ids": [instance_id, domain_target_id],
                "evidence": {"evidence_id": "evidence-workflow-launch"},
            }
        ],
        trusted_scope=TrustedTurnScope(
            user_concept_id="#V#real_user",
            organisation_concept_id="#V#real_org",
            namespace="#V#real_user@real_org",
        ),
    )

    assert len(report["facts"]) == 1
    fact = report["facts"][0]
    assert fact["workflow_instance_operational_readback"] is True
    assert fact["workflow_instance_readback_verified"] is True
    assert fact["workflow_instance_terminal_status"] == "completed"
    assert fact["canonical_readback_verified"] is False
    assert fact["target_ids"] == []
    assert fact["evidence_id"] == "evidence-workflow-readback"
    assert "### Resolved, succeeded, recovered, or handler-reported" in screen_text
    assert screen_text.count(instance_id) == 1
    assert domain_target_id not in screen_text
    assert "terminal status as `completed`" in screen_text
    assert "confirms only its operational status" in screen_text
    assert "not the requested work product" in screen_text
    assert "the current target was read back exactly" not in screen_text
    assert "workflow instance" in report["spoken_text"]
    assert "completed" in report["spoken_text"]
    assert "confirms only its operational status" in report["spoken_text"]
    assert "not the requested work product" in report["spoken_text"]


@pytest.mark.parametrize(
    "canonical_readback_override",
    (
        {"instance_id": "different-workflow-instance"},
        {"status": "running"},
    ),
)
def test_unverified_workflow_instance_readback_never_verifies_domain_targets(
    canonical_readback_override: Mapping[str, str],
) -> None:
    instance_id = "workflow-instance-unverified-report-1"
    workflow_id = "#V#scholarly_article_metadata_representation_workflow"
    domain_target_id = "#V#article_domain_target"
    canonical_readback = {
        "capability": "workflow_get_instance",
        "instance_id": instance_id,
        "workflow_id": workflow_id,
        "status": "failed",
        "evidence_id": "evidence-workflow-readback",
        **canonical_readback_override,
    }
    screen_text, report = _build_effect_outcome_report(
        terminal_status="effect_failed",
        effect_snapshot={
            "effect-workflow-unverified": {
                "turn_finality_required": True,
                "effect_status": "failed",
                "changed": True,
                "current_outcome_status": "failed",
                "outcome_resolved": True,
                "reconciliation_status": "canonically_verified",
                "reconciliation_basis": "workflow_instance_terminal_read",
                "reconciliation_evidence_id": "evidence-workflow-readback",
                "canonical_readback": canonical_readback,
                "workflow_id": workflow_id,
                "instance_id": instance_id,
                "result_target_ids": [instance_id, domain_target_id],
            }
        },
        tool_invocations=[
            {
                "effect_id": "effect-workflow-unverified",
                "tool": "scholarly_article_metadata_representation",
                "capability_display_name": (
                    "Scholarly Article Metadata Representation Workflow"
                ),
                "result_target_ids": [instance_id, domain_target_id],
                "evidence": {"evidence_id": "evidence-workflow-launch"},
            }
        ],
        trusted_scope=TrustedTurnScope(
            user_concept_id="#V#real_user",
            organisation_concept_id="#V#real_org",
            namespace="#V#real_user@real_org",
        ),
    )

    assert len(report["facts"]) == 1
    fact = report["facts"][0]
    assert fact["workflow_instance_operational_readback"] is True
    assert fact["workflow_instance_readback_verified"] is False
    assert fact["canonical_readback_verified"] is False
    assert fact["target_ids"] == []
    assert fact["evidence_id"] == "evidence-workflow-readback"
    assert domain_target_id not in screen_text
    assert "### Resolved, succeeded, recovered, or handler-reported" in screen_text
    assert "### Verified, observed, recovered, or handler-reported" not in screen_text
    assert "workflow-instance operational read-back" in screen_text
    assert "did not exactly verify a consistent terminal status" in screen_text
    assert "does not verify the requested work product" in screen_text
    assert "the current target was read back exactly" not in screen_text
    assert "did not exactly verify a consistent terminal status" in (
        report["spoken_text"]
    )
    assert "does not verify the requested work product" in report["spoken_text"]


def test_domain_effect_report_preserves_readback_and_receipt_semantics() -> None:
    domain_target_id = "#V#article_domain_target"
    screen_text, report = _build_effect_outcome_report(
        terminal_status="model_error",
        effect_snapshot={
            "effect-domain-create": {
                "turn_finality_required": True,
                "effect_status": "succeeded",
                "changed": True,
                "canonical_readback": {
                    "verified": True,
                    "evidence_id": "evidence-domain-readback",
                },
                "reconciliation_evidence_id": "evidence-domain-readback",
                "result_target_ids": [domain_target_id],
            }
        },
        tool_invocations=[
            {
                "effect_id": "effect-domain-create",
                "tool": "create_concepts",
                "result_target_ids": [domain_target_id],
                "evidence": {"evidence_id": "evidence-domain-invocation"},
            }
        ],
        trusted_scope=TrustedTurnScope(
            user_concept_id="#V#real_user",
            organisation_concept_id="#V#real_org",
            namespace="#V#real_user@real_org",
        ),
    )

    assert len(report["facts"]) == 1
    fact = report["facts"][0]
    assert fact["workflow_instance_operational_readback"] is False
    assert fact["workflow_instance_readback_verified"] is False
    assert "workflow_instance_terminal_status" not in fact
    assert fact["canonical_readback_verified"] is True
    assert fact["outcome_resolved"] is True
    assert fact["target_ids"] == [domain_target_id]
    assert fact["evidence_id"] == "evidence-domain-invocation"
    assert "### Verified, observed, recovered, or handler-reported" in screen_text


@pytest.mark.parametrize(
    ("read_back_trip", "expected_status", "expected_fallback"),
    [
        (True, "effect_partially_completed", False),
        (False, "effect_failed", True),
    ],
    ids=["exact-direct-readback", "unverified-direct-success"],
)
def test_failed_workflow_and_later_direct_trip_effects_preserve_only_verified_answer(
    monkeypatch: pytest.MonkeyPatch,
    read_back_trip: bool,
    expected_status: str,
    expected_fallback: bool,
) -> None:
    """Regress request 95c16c12: a failed route must not erase verified recovery."""

    from src.backend.services import ontology_mutation_command_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    original_is_ontology_mutation_method = (
        ontology_mutation_command_service.is_ontology_mutation_method
    )
    monkeypatch.setattr(
        ontology_mutation_command_service,
        "is_ontology_mutation_method",
        lambda method: (
            False
            if method in {"create_concepts", "add_relationship"}
            else original_is_ontology_mutation_method(method)
        ),
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_trip_creation_test",
        workflow_id="#V#represented_trip_creation_workflow",
        display_name="Represented trip creation workflow",
        description="Create one trip and connect its existing flight components.",
        relevance_score=0.99,
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
        input_schema={"type": "object", "properties": {}},
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    instance_id = "workflow-trip-failed-before-domain-mutation"
    trip_id = "#V#american_airlines_confirmation_trip_gmail_derived"
    leg_ids = [
        f"#V#flight_trip_component_gmail_19febb3feda7b024_leg_0{index}"
        for index in (1, 2, 3)
    ]

    def execute_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "failed",
            "workflow_execution": {
                "final_status": "failed",
                "current_state": "initialise_from_item_request",
                "error": "metadata validation failed before domain mutation",
            },
        }

    def read_instance(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "status": "failed",
        }

    gateway = _workflow_gateway(
        execute_workflow,
        hard_timeout_enabled=False,
        instance_handler=read_instance,
    )

    def direct_effect(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "create_concepts":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "created_concept_ids": [trip_id],
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "source_id": arguments["source_id"],
            "target": arguments["target"],
        }

    for capability_name in ("create_concepts", "add_relationship"):
        gateway._catalogue.register(
            MethodDefinition(
                name=capability_name,
                handler=lambda _name=capability_name, **kwargs: direct_effect(
                    _name,
                    kwargs,
                ),
                input_schema=Schema(allow_unknown=True),
                output_schema=Schema(required={"success": bool}, allow_unknown=True),
                category="write",
                ordinary_turn_effect=True,
            )
        )
        gateway.register_metrics_if_missing(capability_name)
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": True,
                "concept_id": concept_id,
                "relations": [
                    {
                        "source_id": trip_id,
                        "predicate": "#V#has_trip_component",
                        "target_id": leg_id,
                    }
                    for leg_id in leg_ids
                ],
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("fetch_concept")

    responses = [
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-trip-workflow",
                    payload={"query": "create and connect the trip"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-trip-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-failed-trip-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {"instance_id": instance_id},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-trip-directly",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"concept_id": trip_id}]},
                    },
                ),
                *[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id=f"link-trip-leg-{index}",
                        payload={
                            "name": "add_relationship",
                            "arguments": {
                                "source_id": trip_id,
                                "predicate": "#V#has_trip_component",
                                "target": leg_id,
                            },
                        },
                    )
                    for index, leg_id in enumerate(leg_ids, start=1)
                ],
            ],
        ),
    ]
    if read_back_trip:
        responses.append(
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="read-trip-direct-result",
                        payload={
                            "name": "fetch_concept",
                            "arguments": {"concept_id": trip_id},
                        },
                    )
                ],
            )
        )
    useful_answer = (
        f"The neutral trip {trip_id} and its three component links persist; "
        f"the earlier workflow instance {instance_id} failed."
    )
    responses.append(LLMResponse(text_response=useful_answer))

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create the agreed trip and tell me what actually persisted.",
        context=[],
        llm_client=_SequenceClient(*responses),
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"trip-mixed-finality-{read_back_trip}",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == expected_status
    assert result.response_authority == "canonical_outcome"
    workflow_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert workflow_invocation["effect_status"] == "failed"
    assert workflow_invocation["changed"] is True
    assert workflow_invocation["canonical_readback"]["status"] == "failed"
    assert useful_answer in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text
    assert trip_id in result.response_text
    assert instance_id in result.response_text
    assert "metadata validation failed before domain mutation" in result.response_text


def test_failed_workflows_and_recovered_denials_preserve_verified_scoped_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regress c8df245c: exact scoped recoveries must survive mixed failures."""

    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    delegation_calls: list[dict[str, Any]] = []

    def deny_global_publication_delegation(**kwargs: Any) -> dict[str, Any]:
        delegation_calls.append(dict(kwargs))
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "global_ontology_admin_authority_required",
            "error": "Semantic ontology authority is required for this effect.",
            "recovery_affordances": [
                {"action_type": "create_scoped_assertion"},
                {"action_type": "request_ontology_administrator_delegation"},
            ],
        }

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        deny_global_publication_delegation,
    )
    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_file_copy_interpretation_test",
        workflow_id="#V#file_copy_interpretation_test_workflow",
        display_name="File-copy interpretation test workflow",
        description="Interpret one durable file copy.",
        relevance_score=0.99,
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
        input_schema={
            "type": "object",
            "properties": {"inputs": {"type": "object", "additionalProperties": True}},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    instance_by_file_copy = {
        "#V#file_copy_student_a": "workflow-file-copy-student-a-failed",
        "#V#file_copy_student_b": "workflow-file-copy-student-b-failed",
    }

    def execute_workflow(**kwargs: Any) -> dict[str, Any]:
        file_copy_id = kwargs["inputs"]["file_copy_concept_id"]
        return {
            "success": True,
            "instance_id": instance_by_file_copy[file_copy_id],
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "failed",
            "workflow_execution": {
                "final_status": "failed",
                "error": ("metadata_read_context_key_missing:concept_id"),
            },
        }

    def read_instance(**kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": kwargs["instance_id"],
            "workflow_id": workflow_capability.workflow_id,
            "status": "failed",
        }

    gateway = _workflow_gateway(
        execute_workflow,
        hard_timeout_enabled=False,
        instance_handler=read_instance,
    )
    denied_handler_calls: list[dict[str, Any]] = []

    def should_be_denied(**kwargs: Any) -> dict[str, Any]:
        denied_handler_calls.append(dict(kwargs))
        return {"success": True, "effect_status": "succeeded", "changed": True}

    gateway._catalogue.register(
        MethodDefinition(
            name="upsert_text_relation",
            handler=should_be_denied,
            input_schema=Schema(allow_unknown=True),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="write",
            ordinary_turn_effect=True,
            ordinary_turn_mutation_subject_argument="concept_id",
            ordinary_turn_trusted_argument_bindings={
                "namespace": "turn_namespace",
            },
            ordinary_turn_fixed_arguments={"provenance": None},
        )
    )

    def persist_scoped_assertion(**kwargs: Any) -> dict[str, Any]:
        subject_id = kwargs["subject_concept_id"]
        assertion_id = f"ska_{subject_id.removeprefix('#V#')}"
        readback = {
            "schema_version": "scoped_knowledge_assertion.v1",
            "assertion_id": assertion_id,
            "subject_concept_id": subject_id,
            "predicate": kwargs["predicate"],
            "object_kind": "text",
            "object_text": {
                "text": kwargs["target_text"],
                "language": kwargs.get("language") or "en-NZ",
            },
            "object_concept_id": None,
            "scope": {
                "mode": kwargs["scope_mode"],
                "user_concept_id": kwargs["acting_user_concept_id"],
                "organisation_concept_id": kwargs["organisation_concept_id"],
                "namespace": kwargs["namespace"],
                "audience_keys": [
                    f"org:{kwargs['organisation_concept_id']}"
                ],
            },
            "provenance": {
                "asserted_by_user_concept_id": kwargs["acting_user_concept_id"],
                "organisation_concept_id": kwargs["organisation_concept_id"],
                "namespace": kwargs["namespace"],
            },
            "canonical_publication": False,
            "status": "asserted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": assertion_id,
            "assertion": readback,
            "canonical_read_back": readback,
            "canonical_publication": False,
            "storage_surface": "scoped_knowledge_assertions",
        }

    gateway._catalogue.register(
        MethodDefinition(
            name="upsert_scoped_assertion",
            handler=persist_scoped_assertion,
            input_schema=Schema(allow_unknown=True),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="write",
            ordinary_turn_effect=True,
            ordinary_turn_trusted_argument_bindings={
                "acting_user_concept_id": "actor_user_concept_id",
                "organisation_concept_id": "actor_organisation_concept_id",
                "namespace": "turn_namespace",
            },
            ordinary_turn_fixed_arguments={"canonical_publication": False},
        )
    )
    gateway.register_metrics_if_missing("upsert_text_relation")
    gateway.register_metrics_if_missing("upsert_scoped_assertion")

    descriptions = {
        "#V#student_a": "Studies robust multimodal learning.",
        "#V#student_b": "Studies gradient formation and reshaping.",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-file-copy-workflow",
                    payload={"query": "interpret the two durable file copies"},
                )
            ],
        ),
        *[
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id=f"interpret-{index}",
                        payload={
                            "name": workflow_capability.name,
                            "arguments": {
                                "inputs": {
                                    "file_copy_concept_id": file_copy_id,
                                }
                            },
                        },
                    )
                ],
            )
            for index, file_copy_id in enumerate(instance_by_file_copy, start=1)
        ],
        *[
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id=f"read-failed-workflow-{index}",
                        payload={
                            "name": "workflow_get_instance",
                            "arguments": {"instance_id": instance_id},
                        },
                    )
                ],
            )
            for index, instance_id in enumerate(
                instance_by_file_copy.values(),
                start=1,
            )
        ],
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=f"deny-canonical-description-{index}",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": subject_id,
                            "predicate": "hasDescription",
                            "text": description,
                            "language": "en-NZ",
                        },
                    },
                )
                for index, (subject_id, description) in enumerate(
                    descriptions.items(),
                    start=1,
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=f"persist-scoped-description-{index}",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": subject_id,
                            "predicate": "hasDescription",
                            "target_text": description,
                            "language": "en-NZ",
                            "scope_mode": "organisation",
                        },
                    },
                )
                for index, (subject_id, description) in enumerate(
                    descriptions.items(),
                    start=1,
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "Both research descriptions were durably read back as "
                "organisation-scoped assertions."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Add research descriptions for those two students.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-c8df245c-mixed-recovery",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert denied_handler_calls == []
    assert len(delegation_calls) == 2
    assert all(
        call["method_name"] == "upsert_text_relation"
        for call in delegation_calls
    )
    assert {
        call["arguments"]["concept_id"] for call in delegation_calls
    } == set(descriptions)
    assert all(call["actor_concept_id"] == "#V#person" for call in delegation_calls)
    assert all(
        call["organisation_concept_id"] == "#V#org"
        for call in delegation_calls
    )
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert "Both research descriptions were durably read back" in (
        result.response_text
    )
    assert "### Canonical scope" in result.response_text
    effects = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("effect_id")
    ]
    assert len(effects) == 6
    workflows = [
        invocation
        for invocation in effects
        if invocation.get("capability_kind") == "represented_workflow"
    ]
    assert len(workflows) == 2
    assert all(invocation["effect_status"] == "failed" for invocation in workflows)
    assert all(invocation["changed"] is True for invocation in workflows)
    denials = [
        invocation
        for invocation in effects
        if invocation.get("tool") == "upsert_text_relation"
    ]
    recoveries = [
        invocation
        for invocation in effects
        if invocation.get("tool") == "upsert_scoped_assertion"
    ]
    assert len(denials) == len(recoveries) == 2
    assert all(invocation["effect_status"] == "not_started" for invocation in denials)
    assert all(
        invocation["error_code"] == "global_ontology_admin_authority_required"
        for invocation in denials
    )
    assert all(invocation["changed"] is False for invocation in denials)
    assert all(invocation["recovery_status"] == "succeeded" for invocation in denials)
    assert {invocation["recovered_by_effect_id"] for invocation in denials} == {
        invocation["effect_id"] for invocation in recoveries
    }
    assert all(invocation["changed"] is True for invocation in recoveries)
    assert all(
        invocation["canonical_readback"]["scope_mode"] == "organisation"
        for invocation in recoveries
    )
    reports = [
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_effect_outcome_report"
    ]
    assert len(reports) == 1


def test_pending_durable_response_uses_canonical_report_with_exact_handle(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_pending_test",
        workflow_id="#V#represented_pending_workflow",
        display_name="Represented pending workflow",
        description="Produce a durable work product that may continue in background.",
        relevance_score=0.95,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {"type": "object", "additionalProperties": True},
                "timeout_seconds": {"type": "number"},
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    instance_id = "workflow-instance-pending-1"
    instance_reads: list[dict[str, Any]] = []

    def _execute_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "running",
            "timed_out": True,
            "workflow_execution": {
                "timeout_seconds": 90.0,
                "poll_interval_seconds": 0.5,
            },
        }

    def _read_instance(**kwargs: Any) -> dict[str, Any]:
        instance_reads.append(dict(kwargs))
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "status": "running",
            "timed_out": False,
        }

    final_text = (
        "The durable work is still running. Its exact workflow instance is "
        f"{instance_id}."
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-pending-workflow",
                    payload={"query": "produce the durable work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-pending-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"timeout_seconds": 90.0},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="inspect-pending-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {
                            "instance_id": instance_id,
                            "await_terminal": False,
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response=final_text),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            _execute_workflow,
            hard_timeout_enabled=False,
            instance_handler=_read_instance,
        ),
        prompt="Produce the durable work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-pending",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert instance_reads == [{"instance_id": instance_id, "await_terminal": False}]
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert result.response_text != final_text
    assert final_text in result.response_text
    assert "### Model draft (non-authoritative)" in result.response_text
    assert instance_id in result.response_text
    assert "status `partial`" in result.response_text


def test_workflow_instance_readback_does_not_reconcile_unrelated_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    instance_id = "shared-looking-instance-id"
    workflow_id = "#V#shared-looking-workflow-id"

    def _effect_handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": False,
            "effect_status": "partial",
            "changed": True,
            "instance_id": instance_id,
            "workflow_id": workflow_id,
        }

    gateway = _effect_gateway(_effect_handler)
    gateway._catalogue.register(
        MethodDefinition(
            name="workflow_get_instance",
            handler=lambda **_kwargs: {
                "success": True,
                "instance_id": instance_id,
                "workflow_id": workflow_id,
                "status": "completed",
            },
            input_schema=Schema(required={"instance_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("workflow_get_instance")
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-unrelated-effect",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Partial result"}]},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-lookalike-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {"instance_id": instance_id},
                    },
                )
            ],
        ),
        LLMResponse(text_response="Everything completed."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Complete the bounded effect.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-unrelated-workflow-lookalike",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    effect_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == "create_concepts"
    )
    assert effect_invocation["capability_kind"] == "registered_tool"
    assert effect_invocation["effect_status"] == "partial"
    assert "canonical_readback" not in effect_invocation
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert result.response_text != "Everything completed."


def test_read_only_workflow_not_started_preserves_successful_direct_recovery(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_mail_review_test",
        workflow_id="#V#mail_review_test_workflow",
        display_name="Mail review test workflow",
        description="Review recent messages through bounded read capabilities.",
        relevance_score=0.96,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {"type": "object", "properties": {}},
            },
            "additionalProperties": False,
        },
        component_capability_names=("general_read",),
        declared_component_count=1,
        declared_step_count=3,
        semantic_effect=None,
        semantic_effect_source="insufficient_declared_component_evidence",
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-mail-workflow",
                    payload={"query": "summarise recent messages"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-mail-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"inputs": {}},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="recover-with-direct-read",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "recent messages"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The recent messages were summarised successfully."),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            lambda **_kwargs: {
                "success": False,
                "error_code": "workflow_not_runnable",
                "status": "rejected_preflight",
            }
        ),
        prompt="Summarise my recent messages.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-read-only-workflow-direct-recovery",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == ("The recent messages were summarised successfully.")
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    workflow_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert workflow_invocation["effect_status"] == "not_started"
    assert workflow_invocation["changed"] is False
    assert workflow_invocation["semantic_effect"] is None
    assert workflow_invocation["turn_finality_required"] is False
    assert "instance_id" not in workflow_invocation
    assert workflow_invocation["evidence"]["semantic_effect"] is None
    assert workflow_invocation["evidence"]["turn_finality_required"] is False
    direct_invocation = next(
        item for item in result.tool_invocations if item.get("tool") == "general_read"
    )
    assert direct_invocation["status"] == "ok"


def test_represented_workflow_retry_reuses_same_turn_idempotency_key(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_retry_test",
        workflow_id="#V#represented_retry_workflow",
        display_name="Represented retry workflow",
        description="Produce one durable work product.",
        relevance_score=0.95,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                }
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    idempotency_keys: list[str] = []

    def _execute_workflow(**kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["event_idempotency_key"])
        idempotency_keys.append(key)
        return {
            "success": True,
            "instance_id": "workflow-instance-reused",
            "created_new": len(idempotency_keys) == 1,
            "final_status": "running",
            "timed_out": True,
        }

    repeated_call = {
        "name": workflow_capability.name,
        "arguments": {"inputs": {"record_id": "#V#record"}},
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-retry-workflow",
                    payload={"query": "produce the durable work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-retry-workflow-1",
                    payload=repeated_call,
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-retry-workflow-2",
                    payload=repeated_call,
                )
            ],
        ),
        LLMResponse(
            text_response="The durable workflow is still running.",
        ),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(_execute_workflow),
        prompt="Produce the durable work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-retry",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] == idempotency_keys[1]
    workflow_invocations = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == workflow_capability.name
    ]
    assert [item["instance_id"] for item in workflow_invocations] == [
        "workflow-instance-reused",
        "workflow-instance-reused",
    ]
    assert [item["changed"] for item in workflow_invocations] == [True, False]
    assert [item["effect_status"] for item in workflow_invocations] == [
        "partial",
        "partial",
    ]
    assert len(client.calls) == 4
    assert all(
        "requested outcome and effect cardinality" in call["system_message"]
        and "must not create, update, or otherwise act on more"
        in call["system_message"]
        for call in client.calls
    )


def test_unchanged_terminally_failed_effect_is_not_dispatched_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_calls: list[dict[str, Any]] = []
    _stub_same_turn_ontology_delegation(monkeypatch)

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "add_relationship"
        handler_calls.append(arguments)
        return {
            "success": False,
            "effect_status": "failed",
            "changed": False,
            "error_code": "invalid_predicate_format",
            "retryable": False,
        }

    effect_arguments = {
        "source_id": "#V#source",
        "predicate": "unresolved predicate",
        "target": "#V#target",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invalid-effect-1",
                    payload={
                        "name": "add_relationship",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invalid-effect-2",
                    payload={
                        "name": "add_relationship",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The relationship was not changed; a different typed predicate "
                "reference is required."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, effect_admission_window_sec=0.01),
        prompt="Add this relationship.",
        context=[],
        llm_client=client,
        model="test-model",
        user_concept_id="#V#user",
        turn_id="turn-repeat-terminal-failure",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert handler_calls == [effect_arguments]
    assert len(result.tool_invocations) == 2
    assert result.tool_invocations[0]["error_code"] == ("invalid_predicate_format")
    assert result.tool_invocations[1]["error_code"] == (
        "effect_request_unchanged_after_terminal_failure"
    )
    assert result.tool_invocations[1]["effect_status"] == "not_started"
    assert result.tool_invocations[1]["changed"] is False


@pytest.mark.parametrize(
    (
        "missing_dependency_kind",
        "created_concept_id",
        "create_changed",
        "canonical_readback_exists",
        "retry_runs",
    ),
    [
        ("source", "#V#missing_source", True, False, True),
        ("source", "#V#missing_source", False, True, True),
        ("source", "#V#unrelated_source", True, False, False),
        ("predicate", "#V#missing_predicate", True, False, True),
    ],
    ids=[
        "exact-created-dependency",
        "exact-idempotent-canonical-readback",
        "unrelated-created-concept",
        "exact-created-predicate-dependency",
    ],
)
def test_terminal_failure_retry_requires_exact_materialised_dependency(
    monkeypatch: pytest.MonkeyPatch,
    missing_dependency_kind: str,
    created_concept_id: str,
    create_changed: bool,
    canonical_readback_exists: bool,
    retry_runs: bool,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    missing_source_id = "#V#missing_source"
    missing_predicate_id = "#V#missing_predicate"
    relationship_predicate = (
        missing_predicate_id
        if missing_dependency_kind == "predicate"
        else "#V#affiliated_with"
    )
    relationship_handler_calls = 0

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal relationship_handler_calls
        if name == "create_concepts":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": create_changed,
                "created_concept_ids": ([created_concept_id] if create_changed else []),
                "canonical_read_back": {
                    "concepts": [
                        {
                            "concept_id": created_concept_id,
                            "exists": canonical_readback_exists,
                        }
                    ]
                },
            }
        assert name == "add_relationship"
        relationship_handler_calls += 1
        if relationship_handler_calls == 1:
            if missing_dependency_kind == "predicate":
                return {
                    "success": False,
                    "effect_status": "failed",
                    "changed": False,
                    "retryable": False,
                    "error_code": "predicate_concept_not_found",
                    "error_details": {
                        "source_id": missing_source_id,
                        "predicate": missing_predicate_id,
                        "target": "#V#school_of_computer_science",
                        "details": {
                            "success": False,
                            "error": "predicate_concept_not_found",
                            "predicate": missing_predicate_id,
                            "suggestion": "Create it as an instance of #V#predicate",
                        },
                    },
                }
            return {
                "success": False,
                "effect_status": "failed",
                "changed": False,
                "retryable": False,
                "error_code": "source_not_found",
                "error_details": {
                    "source_id": missing_source_id,
                    "predicate": relationship_predicate,
                    "target": "#V#school_of_computer_science",
                    "details": {
                        "success": False,
                        "error": "source_not_found",
                        "concept_id": missing_source_id,
                    },
                },
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "source_id": missing_source_id,
            "predicate": relationship_predicate,
            "target": "#V#school_of_computer_science",
        }

    relationship_arguments = {
        "source_id": missing_source_id,
        "predicate": relationship_predicate,
        "target": "#V#school_of_computer_science",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="relationship-before-dependency",
                    payload={
                        "name": "add_relationship",
                        "arguments": relationship_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-relationship-dependency",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "parent_id": (
                                "#V#predicate"
                                if missing_dependency_kind == "predicate"
                                else "#V#person"
                            ),
                            "concepts": [
                                {
                                    "concept_id": created_concept_id,
                                    "name": "Relationship dependency",
                                    "kind": (
                                        "predicate"
                                        if missing_dependency_kind == "predicate"
                                        else "instance"
                                    ),
                                }
                            ],
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="relationship-after-dependency",
                    payload={
                        "name": "add_relationship",
                        "arguments": relationship_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The relationship succeeded after its exact dependency was "
                "materialised."
                if retry_runs
                else (
                    "The unrelated create did not make the failed relationship "
                    "retryable."
                )
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Create the missing dependency and retry this exact relationship.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=(
            "turn-exact-dependency-retry-"
            f"{missing_dependency_kind}-{created_concept_id}-{create_changed}"
        ),
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert relationship_handler_calls == (2 if retry_runs else 1)
    relationship_invocations = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == "add_relationship"
    ]
    assert len(relationship_invocations) == 2
    first, retried = relationship_invocations
    assert first["error_code"] == (
        "predicate_concept_not_found"
        if missing_dependency_kind == "predicate"
        else "source_not_found"
    )
    if retry_runs:
        assert retried["effect_status"] == "succeeded"
        assert first["recovery_status"] == "succeeded"
        assert first["recovered_by_effect_id"] == retried["effect_id"]
        assert result.terminal_status == "completed"
        assert result.response_authority == "model"
    else:
        assert retried["error_code"] == (
            "effect_request_unchanged_after_terminal_failure"
        )
        assert retried["effect_status"] == "not_started"
        assert retried["turn_finality_required"] is False


def test_predelegation_target_denial_retries_after_exact_source_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_source_id = "#V#missing_source"
    relationship_arguments = {
        "source_id": missing_source_id,
        "predicate": "#V#affiliated_with",
        "target": "#V#school_of_computer_science",
    }
    delegation_calls: list[dict[str, Any]] = []
    relationship_delegation_count = 0

    def issue_delegation(**kwargs: Any) -> dict[str, Any]:
        nonlocal relationship_delegation_count
        delegation_calls.append(dict(kwargs))
        if kwargs["method_name"] == "add_relationship":
            relationship_delegation_count += 1
            if relationship_delegation_count == 1:
                return {
                    "success": False,
                    "effect_status": "not_started",
                    "mutation_outcome": "not_started",
                    "changed": False,
                    "retryable": False,
                    "error_code": "ontology_mutation_target_not_accessible",
                    "error": (
                        "The requested ontology target is not accessible in this "
                        "context."
                    ),
                }
        return {"delegation_id": f"delegation-{kwargs['effect_id']}"}

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        issue_delegation,
    )
    handler_calls: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler_calls.append((name, arguments))
        if name == "create_concepts":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "created_concept_ids": [missing_source_id],
                "canonical_read_back": {
                    "concepts": [{"concept_id": missing_source_id, "exists": True}]
                },
            }
        assert name == "add_relationship"
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            **relationship_arguments,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="relationship-before-governed-create",
                    payload={
                        "name": "add_relationship",
                        "arguments": relationship_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-governed-source",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "parent_id": "#V#person",
                            "concepts": [
                                {
                                    "concept_id": missing_source_id,
                                    "name": "Missing source",
                                    "kind": "instance",
                                }
                            ],
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="relationship-after-governed-create",
                    payload={
                        "name": "add_relationship",
                        "arguments": relationship_arguments,
                    },
                )
            ],
        ),
        LLMResponse(text_response="The missing source and relationship now exist."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Create the missing source and retry the relationship.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-governed-predelegation-dependency-retry",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert [call["method_name"] for call in delegation_calls] == [
        "add_relationship",
        "create_concepts",
        "add_relationship",
    ]
    assert [name for name, _arguments in handler_calls] == [
        "create_concepts",
        "add_relationship",
    ]
    relationship_invocations = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == "add_relationship"
    ]
    assert len(relationship_invocations) == 2
    denied, retried = relationship_invocations
    assert denied["error_code"] == "ontology_mutation_target_not_accessible"
    denial_receipt = json.loads(denied["evidence"]["preview"])
    assert "error_details" not in denial_receipt
    assert "source_id" not in denial_receipt
    assert "target" not in denial_receipt
    assert retried["effect_status"] == "succeeded"
    assert denied["recovery_status"] == "succeeded"
    assert denied["recovered_by_effect_id"] == retried["effect_id"]
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"


def test_predelegation_create_failure_is_suppressed_but_corrected_call_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegation_calls: list[dict[str, Any]] = []
    handler_calls: list[dict[str, Any]] = []

    def issue_delegation(**kwargs: Any) -> dict[str, Any]:
        delegation_calls.append(dict(kwargs))
        concept = kwargs["arguments"]["concepts"][0]
        if concept.get("external_identifiers"):
            return {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "error_code": "complex_create_requires_typed_effects",
                "error": (
                    "This governed create path currently supports one exact core "
                    "concept only. Rejected fields: external_identifiers."
                ),
                "error_details": {"rejected_fields": ["external_identifiers"]},
            }
        return {"delegation_id": f"delegation-{kwargs['effect_id']}"}

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        issue_delegation,
    )

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "create_concepts"
        handler_calls.append(arguments)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#profiled_person"],
        }

    rejected_arguments = {
        "parent_id": "#V#person",
        "concepts": [
            {
                "name": "Profiled person",
                "kind": "instance",
                "external_identifiers": [
                    {
                        "scheme": "profiles.example",
                        "canonical_value": "person-42",
                        "role": "identity",
                    }
                ],
            }
        ],
    }
    corrected_arguments = {
        "parent_id": "#V#person",
        "concepts": [{"name": "Profiled person", "kind": "instance"}],
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="complex-create-1",
                    payload={
                        "name": "create_concepts",
                        "arguments": rejected_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="complex-create-2",
                    payload={
                        "name": "create_concepts",
                        "arguments": rejected_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="core-create-corrected",
                    payload={
                        "name": "create_concepts",
                        "arguments": corrected_arguments,
                    },
                )
            ],
        ),
        LLMResponse(text_response="The corrected core concept was created."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Create this profiled person without repeating failed effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#user@org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        turn_id="turn-predelegation-create-repair",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert len(delegation_calls) == 2
    assert delegation_calls[0]["arguments"]["concepts"] == (
        rejected_arguments["concepts"]
    )
    assert delegation_calls[1]["arguments"]["concepts"] == (
        corrected_arguments["concepts"]
    )
    assert len(handler_calls) == 1
    assert handler_calls[0]["concepts"] == corrected_arguments["concepts"]

    assert len(result.tool_invocations) == 3
    first, repeated, corrected = result.tool_invocations
    assert first["error_code"] == "complex_create_requires_typed_effects"
    first_receipt = json.loads(first["evidence"]["preview"])
    assert first_receipt["error_details"] == {
        "rejected_fields": ["external_identifiers"]
    }
    assert "recovery_affordances" not in first_receipt
    assert repeated["error_code"] == "effect_request_unchanged_after_terminal_failure"
    repeated_receipt = json.loads(repeated["evidence"]["preview"])
    assert repeated_receipt["prior_error_code"] == (
        "complex_create_requires_typed_effects"
    )
    assert repeated["effect_status"] == "not_started"
    assert repeated["changed"] is False
    assert "recovery_affordances" not in repeated_receipt
    assert corrected["effect_status"] == "succeeded"
    assert corrected["changed"] is True


@pytest.mark.parametrize(
    (
        "recovery_concept_id",
        "receipt_concept_id",
        "origin_error_code",
        "recovery_succeeds",
        "extra_readback_concept",
        "original_omits_concept_id",
        "expected_recovered",
        "expected_terminal_status",
    ),
    (
        (
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "ontology_create_concept_id_conflict",
            True,
            False,
            False,
            True,
            "completed",
        ),
        (
            "#V#scoped_referent_ava_example_fedcba9876543210fedcba98",
            "#V#scoped_referent_ava_example_fedcba9876543210fedcba98",
            "ontology_create_concept_id_conflict",
            True,
            False,
            False,
            False,
            "effect_partially_completed",
        ),
        (
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "#V#scoped_referent_ava_example_badbadbadbadbadbadbadbad",
            "ontology_create_concept_id_conflict",
            True,
            False,
            False,
            False,
            "effect_partially_completed",
        ),
        (
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "ontology_create_concept_id_conflict",
            False,
            False,
            False,
            False,
            "effect_failed",
        ),
        (
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "unrelated_create_failure",
            True,
            False,
            False,
            False,
            "effect_partially_completed",
        ),
        (
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "ontology_create_concept_id_conflict",
            True,
            True,
            False,
            False,
            "effect_partially_completed",
        ),
        (
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "#V#scoped_referent_ava_example_0123456789abcdef01234567",
            "ontology_create_concept_id_conflict",
            True,
            False,
            True,
            True,
            "completed",
        ),
    ),
    ids=[
        "exact-advertised-recovery",
        "different-unadvertised-referent",
        "wrong-success-readback",
        "exact-recovery-failed",
        "unrelated-origin-cannot-advertise-recovery",
        "extra-readback-concept",
        "name-only-original-create",
    ],
)
def test_actor_scoped_referent_retires_only_exact_advertised_collision(
    monkeypatch: pytest.MonkeyPatch,
    recovery_concept_id: str,
    receipt_concept_id: str,
    origin_error_code: str,
    recovery_succeeds: bool,
    extra_readback_concept: bool,
    original_omits_concept_id: bool,
    expected_recovered: bool,
    expected_terminal_status: str,
) -> None:
    from src.backend.services.actor_scoped_referent_identity_service import (
        ACTOR_SCOPED_REFERENT_COLLISION_MODE,
        ACTOR_SCOPED_REFERENT_RECOVERY_ACTION,
        ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
        ACTOR_SCOPED_REFERENT_SCOPE_MODE,
    )

    requested_concept_id = "#V#ava_example"
    advertised_referent_id = (
        "#V#scoped_referent_ava_example_0123456789abcdef01234567"
    )
    core_concept = {
        "concept_id": advertised_referent_id,
        "name": "Ava Example",
        "kind": "instance",
        "description": "A represented person used by this test.",
    }
    advertised_arguments = {
        "parent_id": "#V#person",
        "concepts": [core_concept],
        "duplicate_resolution_mode": "canonical_id_only",
        "collision_resolution_mode": ACTOR_SCOPED_REFERENT_COLLISION_MODE,
        "requested_concept_id": requested_concept_id,
        "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
    }
    attempted_arguments = {
        **advertised_arguments,
        "concepts": [
            {
                **core_concept,
                "concept_id": recovery_concept_id,
            }
        ],
    }
    delegation_calls: list[dict[str, Any]] = []

    def issue_delegation(**kwargs: Any) -> dict[str, Any]:
        delegation_calls.append(dict(kwargs))
        if not kwargs["arguments"].get("collision_resolution_mode"):
            return {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "retryable": False,
                "error_code": origin_error_code,
                "error": "The requested concept ID cannot be created.",
                "recovery_affordances": [
                    {
                        "action_type": ACTOR_SCOPED_REFERENT_RECOVERY_ACTION,
                        "tool": "create_concepts",
                        "recovery_contract": (
                            ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT
                        ),
                        "arguments": advertised_arguments,
                    }
                ],
            }
        return {"delegation_id": f"delegation-{kwargs['effect_id']}"}

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        issue_delegation,
    )
    handler_calls: list[dict[str, Any]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "create_concepts"
        handler_calls.append(arguments)
        if not recovery_succeeds:
                return {
                    "success": False,
                    "effect_status": "failed",
                    "mutation_outcome": "failed",
                "changed": False,
                "error_code": "actor_scoped_referent_create_failed",
            }
        readback_concepts = [
            {
                "concept_id": receipt_concept_id,
                "exists": True,
                "publication_context": {
                    "kind": "user",
                    "concept_id": "#V#person",
                },
            }
        ]
        if extra_readback_concept:
            readback_concepts.append(
                {
                    "concept_id": "#V#unexpected_extra_concept",
                    "exists": True,
                    "publication_context": {
                        "kind": "user",
                        "concept_id": "#V#person",
                    },
                }
            )
        return {
            "success": True,
            "effect_status": "succeeded",
            "mutation_outcome": "succeeded",
            "changed": True,
            "created_concept_ids": [receipt_concept_id],
            "actor_scoped_referent": {
                "schema_version": ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
                "effective_concept_id": receipt_concept_id,
                "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
                "identity_status": "unreconciled_actor_scoped_referent",
                "equivalence_asserted": False,
                "alias_created": False,
            },
            "canonical_read_back": {"concepts": readback_concepts},
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="colliding-person-create",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "parent_id": "#V#person",
                            "concepts": [
                                {
                                    **(
                                        {}
                                        if original_omits_concept_id
                                        else {"concept_id": requested_concept_id}
                                    ),
                                    "name": "Ava Example",
                                    "kind": "instance",
                                    "description": (
                                        "A represented person used by this test."
                                    ),
                                }
                            ],
                            "duplicate_resolution_mode": "canonical_id_only",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="actor-scoped-person-recovery",
                    payload={
                        "name": "create_concepts",
                        "arguments": attempted_arguments,
                    },
                )
            ],
        ),
        LLMResponse(text_response="The person referent was represented."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent this person despite an inaccessible ID collision.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=(
            "actor-scoped-referent-"
            f"{origin_error_code}-{recovery_succeeds}-"
            f"{receipt_concept_id[-8:]}-{extra_readback_concept}-"
            f"{original_omits_concept_id}"
        ),
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert len(delegation_calls) == 2
    assert len(handler_calls) == 1
    assert handler_calls[0]["concepts"][0]["concept_id"] == recovery_concept_id
    collision, recovery = result.tool_invocations
    assert collision["error_code"] == origin_error_code
    assert recovery["effect_status"] == (
        "succeeded" if recovery_succeeds else "failed"
    )
    assert result.terminal_status == expected_terminal_status
    if expected_recovered:
        assert collision["recovery_status"] == "succeeded"
        assert collision["recovered_by_effect_id"] == recovery["effect_id"]
        assert result.response_text == "The person referent was represented."
    else:
        assert "recovery_status" not in collision
        assert "recovered_by_effect_id" not in collision
        assert result.response_text != "The person referent was represented."


def test_one_actor_scoped_referent_recovery_does_not_retire_another_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services.actor_scoped_referent_identity_service import (
        ACTOR_SCOPED_REFERENT_COLLISION_MODE,
        ACTOR_SCOPED_REFERENT_RECOVERY_ACTION,
        ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
        ACTOR_SCOPED_REFERENT_SCOPE_MODE,
    )

    people = {
        "#V#ava_example": {
            "name": "Ava Example",
            "referent_id": (
                "#V#scoped_referent_ava_example_0123456789abcdef01234567"
            ),
        },
        "#V#ben_example": {
            "name": "Ben Example",
            "referent_id": (
                "#V#scoped_referent_ben_example_0123456789abcdef01234567"
            ),
        },
    }

    def recovery_arguments(requested_id: str) -> dict[str, Any]:
        person = people[requested_id]
        return {
            "parent_id": "#V#person",
            "concepts": [
                {
                    "concept_id": person["referent_id"],
                    "name": person["name"],
                    "kind": "instance",
                }
            ],
            "duplicate_resolution_mode": "canonical_id_only",
            "collision_resolution_mode": ACTOR_SCOPED_REFERENT_COLLISION_MODE,
            "requested_concept_id": requested_id,
            "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
        }

    def issue_delegation(**kwargs: Any) -> dict[str, Any]:
        arguments = kwargs["arguments"]
        if not arguments.get("collision_resolution_mode"):
            requested_id = arguments["concepts"][0]["concept_id"]
            return {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "retryable": False,
                "error_code": "ontology_create_concept_id_conflict",
                "error": "The requested concept ID cannot be created.",
                "recovery_affordances": [
                    {
                        "action_type": ACTOR_SCOPED_REFERENT_RECOVERY_ACTION,
                        "tool": "create_concepts",
                        "recovery_contract": (
                            ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT
                        ),
                        "arguments": recovery_arguments(requested_id),
                    }
                ],
            }
        return {"delegation_id": f"delegation-{kwargs['effect_id']}"}

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "issue_same_turn_method_delegation",
        issue_delegation,
    )

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "create_concepts"
        referent_id = arguments["concepts"][0]["concept_id"]
        return {
            "success": True,
            "effect_status": "succeeded",
            "mutation_outcome": "succeeded",
            "changed": True,
            "created_concept_ids": [referent_id],
            "actor_scoped_referent": {
                "schema_version": ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
                "effective_concept_id": referent_id,
                "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
                "identity_status": "unreconciled_actor_scoped_referent",
                "equivalence_asserted": False,
                "alias_created": False,
            },
            "canonical_read_back": {
                "concepts": [
                    {
                        "concept_id": referent_id,
                        "exists": True,
                        "publication_context": {
                            "kind": "user",
                            "concept_id": "#V#person",
                        },
                    }
                ]
            },
        }

    initial_calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"collide-{requested_id.removeprefix('#V#')}",
            payload={
                "name": "create_concepts",
                "arguments": {
                    "parent_id": "#V#person",
                    "concepts": [
                        {
                            "concept_id": requested_id,
                            "name": person["name"],
                            "kind": "instance",
                        }
                    ],
                },
            },
        )
        for requested_id, person in people.items()
    ]
    client = _SequenceClient(
        LLMResponse(text_response="", tool_calls=initial_calls),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="recover-only-ava",
                    payload={
                        "name": "create_concepts",
                        "arguments": recovery_arguments("#V#ava_example"),
                    },
                )
            ],
        ),
        LLMResponse(text_response="Ava was represented."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent Ava and Ben despite inaccessible ID collisions.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="two-actor-scoped-referent-collisions",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    ava_collision, ben_collision, ava_recovery = result.tool_invocations
    assert ava_collision["recovery_status"] == "succeeded"
    assert ava_collision["recovered_by_effect_id"] == ava_recovery["effect_id"]
    assert "recovery_status" not in ben_collision
    assert "recovered_by_effect_id" not in ben_collision
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_text != "Ava was represented."


def test_successful_core_create_continues_with_scoped_semantic_postconditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    handler_calls: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler_calls.append((name, arguments))
        if name == "create_concepts":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "created_concept_ids": ["#V#burkhard_wuensche"],
                "canonical_read_back": {
                    "status": "verified",
                    "verified": True,
                    "concept_id": "#V#burkhard_wuensche",
                },
            }
        assert name == "upsert_scoped_assertion"
        assertion_id = (
            "ska_role"
            if arguments["predicate"] == "#V#is_an_instance_of"
            else "ska_affiliation"
        )
        readback = {
            "schema_version": "scoped_knowledge_assertion.v1",
            "assertion_id": assertion_id,
            "subject_concept_id": arguments["subject_concept_id"],
            "predicate": arguments["predicate"],
            "object_kind": "concept",
            "object_concept_id": arguments["target_concept_id"],
            "object_text": None,
            "scope": {
                "mode": arguments["scope_mode"],
                "user_concept_id": arguments["acting_user_concept_id"],
                "organisation_concept_id": arguments[
                    "organisation_concept_id"
                ],
                "namespace": arguments["namespace"],
                "audience_keys": [
                    f"org:{arguments['organisation_concept_id']}"
                ],
            },
            "provenance": {
                "asserted_by_user_concept_id": arguments[
                    "acting_user_concept_id"
                ],
                "organisation_concept_id": arguments[
                    "organisation_concept_id"
                ],
                "namespace": arguments["namespace"],
                "evidence": arguments["evidence"],
            },
            "canonical_publication": False,
            "status": "asserted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": assertion_id,
            "assertion": readback,
            "canonical_read_back": readback,
            "canonical_publication": False,
        }

    source_evidence = {
        "source_id": "staff-page-source",
        "claim_text": "Burkhard Wuensche is academic staff at the School.",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-wuensche-core",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "parent_id": "#V#person",
                            "concepts": [
                                {
                                    "name": "Burkhard Wuensche",
                                    "kind": "instance",
                                }
                            ],
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="assert-wuensche-role",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#burkhard_wuensche",
                            "predicate": "#V#is_an_instance_of",
                            "target_concept_id": "#V#academic_staff",
                            "scope_mode": "organisation",
                            "evidence": source_evidence,
                        },
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="assert-wuensche-affiliation",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#burkhard_wuensche",
                            "predicate": "#V#affiliated_with",
                            "target_concept_id": "#V#school_of_computer_science",
                            "scope_mode": "organisation",
                            "evidence": source_evidence,
                        },
                    },
                ),
            ],
        ),
        LLMResponse(
            text_response=(
                "The Person core and both organisation-scoped semantic facts "
                "were recorded and read back."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt=(
            "Represent Burkhard Wuensche as a Person, academic staff, and "
            "affiliated with the School, preserving the source evidence."
        ),
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-core-then-scoped-postconditions",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert [name for name, _arguments in handler_calls] == [
        "create_concepts",
        "upsert_scoped_assertion",
        "upsert_scoped_assertion",
    ]
    create_arguments = handler_calls[0][1]
    assert len(create_arguments["concepts"]) == 1
    assert create_arguments["concepts"][0] == {
        "name": "Burkhard Wuensche",
        "kind": "instance",
    }
    for _name, assertion_arguments in handler_calls[1:]:
        assert assertion_arguments["acting_user_concept_id"] == "#V#person"
        assert assertion_arguments["organisation_concept_id"] == "#V#org"
        assert assertion_arguments["namespace"] == "#V#person@org"
        assert assertion_arguments["canonical_publication"] is False
        assert assertion_arguments["evidence"] == source_evidence
    assert "core concept establishes only" in client.calls[1]["system_message"]
    assert "entity_representation_coverage=core_only" in client.calls[1][
        "system_message"
    ]
    scoped_invocations = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == "upsert_scoped_assertion"
    ]
    assert len(scoped_invocations) == 2
    assert all(
        invocation.get("canonical_readback", {}).get("object_kind") == "concept"
        for invocation in scoped_invocations
    )
    assert {
        invocation["canonical_readback"]["object_concept_id"]
        for invocation in scoped_invocations
    } == {"#V#academic_staff", "#V#school_of_computer_science"}
    assert result.terminal_status == "completed"
    assert result.response_text == (
        "The Person core and both organisation-scoped semantic facts were "
        "recorded and read back."
    )


@pytest.mark.parametrize(
    ("inspection_target", "concepts"),
    [
        (
            None,
            [{"concept_id": "#V#unknown_create", "name": "Unknown"}],
        ),
        (
            "#V#wrong_target_create",
            [{"concept_id": "#V#unknown_create", "name": "Unknown"}],
        ),
        (
            "#V#unknown_create",
            [
                {"concept_id": "#V#unknown_create", "name": "Unknown"},
                {"name": "Possibly created without an explicit ID"},
            ],
        ),
    ],
)
def test_unchanged_indeterminate_create_requires_exact_inspection_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    inspection_target: str | None,
    concepts: list[dict[str, str]],
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    handler_calls: list[dict[str, Any]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "create_concepts"
        handler_calls.append(arguments)
        return {
            "success": False,
            "effect_status": "indeterminate",
            "changed": None,
            "error_code": "tool_timeout_outcome_unknown",
            "mutation_outcome": "unknown",
        }

    effect_arguments = {"concepts": concepts}
    responses = [
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="unknown-create-1",
                    payload={
                        "name": "create_concepts",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
    ]
    if inspection_target is not None:
        responses.append(
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="inspect-wrong-create-target",
                        payload={
                            "name": "fetch_concept",
                            "arguments": {"concept_id": inspection_target},
                        },
                    )
                ],
            )
        )
    responses.extend(
        [
            LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="unknown-create-2",
                    payload={
                        "name": "create_concepts",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(text_response="The create outcome still needs inspection."),
        ]
    )

    gateway = _effect_gateway(handler)
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": False,
                "error_code": "concept_not_found",
                "error_details": {
                    "concept_id": concept_id,
                    "status": "not_found",
                },
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("fetch_concept")
    client = _SequenceClient(*responses)

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create this concept once.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#user@org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        turn_id="turn-repeat-indeterminate-create",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert len(handler_calls) == 1
    assert len(result.tool_invocations) == (3 if inspection_target else 2)
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert "current_outcome_status" not in result.tool_invocations[0]
    blocked_invocation = result.tool_invocations[-1]
    assert blocked_invocation["error_code"] == (
        "effect_request_reconciliation_required"
    )
    assert blocked_invocation["effect_status"] == "not_started"


@pytest.mark.parametrize("rich_receipt", [True, False])
def test_exact_absence_allows_retry_of_indeterminate_create(
    monkeypatch: pytest.MonkeyPatch,
    rich_receipt: bool,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    concept_id = "#V#absent_then_created"
    create_calls = 0

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service."
        "reconcile_governed_ontology_postcondition",
        lambda **_kwargs: {"success": False, "verified": False},
    )

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal create_calls
        assert name == "create_concepts"
        create_calls += 1
        if create_calls == 1:
            failure = {
                "success": False,
                "effect_status": "indeterminate",
                "changed": None,
                "error_code": (
                    "tool_timeout_outcome_unknown"
                    if rich_receipt
                    else "effect_outcome_unknown"
                ),
                "mutation_outcome": "unknown",
            }
            if rich_receipt:
                failure.update(
                    {
                        "outcome_finality": "requires_canonical_reconciliation",
                        "created_concept_ids": [concept_id],
                        "postcondition_reconciliation": {
                            "schema_version": (
                                "ontology_mutation_postcondition_reconciliation.v1"
                            )
                        },
                    }
                )
            return failure
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": [concept_id],
        }

    gateway = _effect_gateway(handler)
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": False,
                "error_code": "concept_not_found",
                "error": f"Concept {concept_id} was not found.",
                "error_details": {
                    "concept_id": concept_id,
                    "status": "not_found",
                },
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("fetch_concept")
    effect_arguments = {
        "concepts": [{"concept_id": concept_id, "name": "Absent then created"}]
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-before-absence-read",
                    payload={
                        "name": "create_concepts",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-exact-absence",
                    payload={
                        "name": "fetch_concept",
                        "arguments": {"concept_id": concept_id},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-after-absence-read",
                    payload={
                        "name": "create_concepts",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(text_response=f"The concept {concept_id} now exists."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create this concept, inspecting an unknown outcome before retry.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#user@org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        turn_id="turn-retry-after-exact-absence",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert create_calls == 2
    create_invocations = [
        item
        for item in result.tool_invocations
        if item.get("tool") == "create_concepts"
    ]
    assert [item["effect_status"] for item in create_invocations] == [
        "indeterminate",
        "succeeded",
    ]
    assert create_invocations[0]["current_outcome_status"] == "target_absent"
    assert create_invocations[0]["outcome_resolved"] is True
    assert create_invocations[0]["initial_effect_status"] == "indeterminate"
    assert create_invocations[0]["recovery_status"] == "succeeded"
    assert create_invocations[0]["recovered_by_effect_id"] == _effect_id(
        turn_id="turn-retry-after-exact-absence",
        call_id="create-after-absence-read",
        capability_name="create_concepts",
    )
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    assert result.response_text == f"The concept {concept_id} now exists."


def test_model_call_progress_reports_cumulative_usage_cost_and_exact_identity() -> None:
    progress_events: list[dict[str, Any]] = []
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_list_evidence",
                    call_id="list-evidence",
                    payload={},
                )
            ],
            model="gpt-test-effective",
            usage={"prompt_tokens": 100, "completion_tokens": 10},
            transport_metadata={
                "effective_service_tier": "default",
                "effective_connection_id": "#V#openai_provider",
            },
        ),
        LLMResponse(
            text_response="Done.",
            model="gpt-test-effective",
            usage={"prompt_tokens": 50, "completion_tokens": 5},
            transport_metadata={
                "effective_service_tier": "default",
                "effective_connection_id": "#V#openai_provider",
            },
        ),
    )
    client.config = SimpleNamespace(provider="openai")
    registry = {
        "models": [
            {
                "provider": "openai",
                "model_id": "gpt-test-effective",
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "test-v1",
                    "source": "test",
                    "effective_at_utc": "2026-08-05T00:00:00Z",
                    "model_id": "gpt-test-effective",
                    "currency": "USD",
                    "unit_tokens": 1_000,
                    "rates": {
                        "input_tokens": 1.0,
                        "output_tokens": 2.0,
                    },
                },
            }
        ]
    }

    execute_adaptive_turn(
        gateway=None,
        prompt="Use evidence if useful, then answer.",
        context=[],
        llm_client=client,
        model="gpt-test-requested",
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="usage-cost-progress",
        model_registry_snapshot=registry,
    )

    model_end_events = [
        event
        for event in progress_events
        if event.get("event_kind") == "llm_call_end"
        and str(event.get("call_id") or "").startswith("usage-cost-progress:llm:")
    ]
    assert [event["call_id"] for event in model_end_events] == [
        "usage-cost-progress:llm:1",
        "usage-cost-progress:llm:2",
    ]
    assert model_end_events[0]["requested_model"] == "gpt-test-requested"
    assert model_end_events[0]["selected_model"] == "gpt-test-requested"
    assert model_end_events[0]["effective_model"] == "gpt-test-effective"
    assert model_end_events[0]["model_identity_source"] == "provider_response"
    first_summary = model_end_events[0]["llm_usage_cost_summary"]
    second_summary = model_end_events[1]["llm_usage_cost_summary"]
    assert first_summary["usage"]["total_tokens"] == 110
    assert first_summary["estimated_cost"]["amount"] == 0.12
    assert second_summary["usage"]["total_tokens"] == 165
    assert second_summary["estimated_cost"]["amount"] == 0.18
    assert second_summary["model_identities"] == [
        {
            "provider": "openai",
            "requested_model": "gpt-test-requested",
            "selected_model": "gpt-test-requested",
            "effective_model": "gpt-test-effective",
            "model_identity_source": "provider_response",
            "provider_request_sent": True,
            "effective_service_tier": "default",
            "connection_id": "#V#openai_provider",
            "call_count": 2,
        }
    ]


def _run_explicit_scoped_recovery_replay(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mismatched_index: int | None,
    mismatch_kind: str = "target",
    emit_exact_affordances: bool = True,
) -> tuple[Any, list[dict[str, Any]]]:
    _stub_same_turn_ontology_delegation(monkeypatch)
    facts = [
        ("is_an_instance_of", "#V#academic_staff"),
        ("affiliated_with", "#V#school_of_computer_science"),
        ("has_job_title", "#V#associate_professor"),
        ("employed_by", "#V#university_of_auckland"),
        ("member_of", "#V#computer_science_staff"),
        ("works_at", "#V#university_of_auckland_city_campus"),
    ]
    seen_arguments: list[dict[str, Any]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "upsert_scoped_assertion"
        seen_arguments.append(dict(arguments))
        predicate = str(arguments["predicate"])
        if not predicate.startswith("#V#"):
            failure = {
                "success": False,
                "effect_status": "failed",
                "changed": False,
                "error_code": "invalid_scoped_assertion",
                "error": "predicate must be a represented concept ID",
            }
            if emit_exact_affordances:
                failure["recovery_affordances"] = [
                    {
                        "action_type": (
                            "retry_exact_scoped_assertion_with_canonical_predicate_id"
                        ),
                        "tool": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": arguments[
                                "subject_concept_id"
                            ],
                            "predicate": f"#V#{predicate}",
                            "target_concept_id": arguments[
                                "target_concept_id"
                            ],
                            "scope_mode": arguments["scope_mode"],
                            "evidence": arguments["evidence"],
                        },
                    }
                ]
            return failure

        assertion_id = f"ska_recovery_{len(seen_arguments)}"
        scope_mode = str(arguments["scope_mode"])
        organisation_id = arguments["organisation_concept_id"]
        readback = {
            "schema_version": "scoped_knowledge_assertion.v1",
            "assertion_id": assertion_id,
            "subject_concept_id": arguments["subject_concept_id"],
            "predicate": predicate,
            "object_kind": "concept",
            "object_concept_id": arguments["target_concept_id"],
            "object_text": None,
            "scope": {
                "mode": scope_mode,
                "user_concept_id": arguments["acting_user_concept_id"],
                "organisation_concept_id": organisation_id,
                "namespace": arguments["namespace"],
                "audience_keys": [f"org:{organisation_id}"],
            },
            "provenance": {
                "asserted_by_user_concept_id": arguments[
                    "acting_user_concept_id"
                ],
                "organisation_concept_id": organisation_id,
                "namespace": arguments["namespace"],
            },
            "canonical_publication": False,
            "status": "asserted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": assertion_id,
            "canonical_read_back": readback,
        }

    malformed_calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"malformed-scoped-fact-{index + 1}",
            payload={
                "name": "upsert_scoped_assertion",
                "arguments": {
                    "subject_concept_id": "#V#marc_example",
                    "predicate": predicate,
                    "target_concept_id": target,
                    "scope_mode": "organisation",
                    "evidence": {"source": "official staff page"},
                },
            },
        )
        for index, (predicate, target) in enumerate(facts)
    ]
    corrected_calls = []
    for index, (predicate, target) in enumerate(facts):
        corrected_arguments = {
            "subject_concept_id": "#V#marc_example",
            "predicate": f"#V#{predicate}",
            "target_concept_id": target,
            "scope_mode": "organisation",
            "evidence": {"source": "official staff page"},
        }
        if index == mismatched_index:
            if mismatch_kind == "target":
                corrected_arguments["target_concept_id"] = (
                    "#V#valid_but_wrong_target"
                )
            elif mismatch_kind == "scope":
                corrected_arguments["scope_mode"] = "user"
            elif mismatch_kind == "evidence":
                corrected_arguments["evidence"] = {"source": "different page"}
            else:
                raise AssertionError(f"Unsupported mismatch kind: {mismatch_kind}")
        corrected_calls.append(
            ToolCall(
                tool_name="turn_invoke_capability",
                call_id=f"corrected-scoped-fact-{index + 1}",
                payload={
                    "name": "upsert_scoped_assertion",
                    "arguments": corrected_arguments,
                },
            )
        )

    client = _SequenceClient(
        LLMResponse(text_response="", tool_calls=malformed_calls),
        LLMResponse(text_response="", tool_calls=corrected_calls),
        LLMResponse(text_response="The six requested scoped facts are now present."),
    )
    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Represent Marc Example with these six organisation-scoped facts.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"turn-explicit-scoped-recovery-{mismatched_index}",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    invoke_tool = next(
        tool
        for tool in client.calls[0]["available_tools"]
        if tool.name == "turn_invoke_capability"
    )
    assert "satisfies_obligation_id" not in invoke_tool.input_schema["properties"]
    assert all(
        set(call.payload) == {"name", "arguments"}
        for call in malformed_calls + corrected_calls
    )
    assert all(
        "satisfies_obligation_id" not in arguments
        for arguments in seen_arguments
    )
    return result, seen_arguments


def test_all_explicit_exact_scoped_recoveries_complete_without_linkage_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, seen_arguments = _run_explicit_scoped_recovery_replay(
        monkeypatch,
        mismatched_index=None,
    )

    assert len(seen_arguments) == 12
    failures = [
        invocation
        for invocation in result.tool_invocations
        if str(invocation.get("call_id") or "").startswith("malformed-scoped-fact-")
    ]
    recoveries = [
        invocation
        for invocation in result.tool_invocations
        if str(invocation.get("call_id") or "").startswith("corrected-scoped-fact-")
    ]
    assert len(failures) == len(recoveries) == 6
    assert all(
        invocation["effect_status"] == "failed"
        and invocation["changed"] is False
        and invocation["recovery_status"] == "succeeded"
        and invocation.get("recovered_by_effect_id")
        for invocation in failures
    )
    assert all(
        invocation["effect_status"] == "succeeded"
        and invocation["changed"] is True
        and invocation.get("canonical_readback", {}).get("status") == "asserted"
        for invocation in recoveries
    )
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    assert result.response_text == "The six requested scoped facts are now present."


@pytest.mark.parametrize("mismatch_kind", ["target", "scope", "evidence"])
def test_one_valid_but_mismatched_scoped_recovery_keeps_turn_partial(
    monkeypatch: pytest.MonkeyPatch,
    mismatch_kind: str,
) -> None:
    result, seen_arguments = _run_explicit_scoped_recovery_replay(
        monkeypatch,
        mismatched_index=5,
        mismatch_kind=mismatch_kind,
    )

    failures = [
        invocation
        for invocation in result.tool_invocations
        if str(invocation.get("call_id") or "").startswith("malformed-scoped-fact-")
    ]
    assert sum(
        invocation.get("recovery_status") == "succeeded"
        for invocation in failures
    ) == 5
    mismatched = next(
        invocation
        for invocation in failures
        if invocation.get("recovery_status") == "mismatched"
    )
    wrong_recovery = next(
        invocation
        for invocation in result.tool_invocations
        if invocation.get("call_id") == "corrected-scoped-fact-6"
    )
    assert mismatched["attempted_recovery_effect_id"] == wrong_recovery["effect_id"]
    assert "recovered_by_effect_id" not in mismatched
    recovery_arguments = seen_arguments[-1]
    if mismatch_kind == "target":
        assert wrong_recovery["canonical_readback"]["object_concept_id"] == (
            "#V#valid_but_wrong_target"
        )
    elif mismatch_kind == "scope":
        assert recovery_arguments["scope_mode"] == "user"
    else:
        assert recovery_arguments["evidence"] == {"source": "different page"}
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"
    assert result.response_text != "The six requested scoped facts are now present."


def test_malformed_scoped_failures_without_exact_affordances_are_not_fuzzily_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _seen_arguments = _run_explicit_scoped_recovery_replay(
        monkeypatch,
        mismatched_index=None,
        emit_exact_affordances=False,
    )

    failures = [
        invocation
        for invocation in result.tool_invocations
        if str(invocation.get("call_id") or "").startswith("malformed-scoped-fact-")
    ]
    assert len(failures) == 6
    assert all("recovery_status" not in invocation for invocation in failures)
    assert all("recovered_by_effect_id" not in invocation for invocation in failures)
    assert result.terminal_status == "effect_partially_completed"
    assert result.response_authority == "canonical_outcome"


def test_catalogue_exact_predicate_affordances_reconcile_concept_and_text_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.integrations.internal_mcp.catalogue import (
        build_default_catalogue,
    )
    from src.backend.services.text_relation_predicate_validation_service import (
        TextRelationPredicateResolutionError,
    )

    _stub_same_turn_ontology_delegation(monkeypatch)
    monkeypatch.setattr(
        "src.backend.security.access_control.can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service."
        "resolve_existing_predicate_value_kind",
        lambda predicate_id: (
            "text" if predicate_id == "#V#has_email" else "concept"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service."
        "maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: {"success": True, "skipped": True},
    )

    service_calls: list[dict[str, Any]] = []

    def upsert(**arguments: Any) -> dict[str, Any]:
        service_calls.append(dict(arguments))
        predicate = str(arguments["predicate"])
        if predicate == "affiliated_with":
            raise ValueError("predicate must be an exact #V# concept ID")
        if predicate == "has_email":
            raise TextRelationPredicateResolutionError(
                "unsupported_text_relation_predicate",
                "Unsupported text-relation predicate 'has_email'.",
                details={"predicate": "has_email"},
            )
        object_kind = "text" if arguments.get("target_text") else "concept"
        assertion_id = f"ska_catalogue_recovery_{len(service_calls)}"
        readback = {
            "schema_version": "scoped_knowledge_assertion.v1",
            "assertion_id": assertion_id,
            "subject_concept_id": arguments["subject_concept_id"],
            "predicate": predicate,
            "object_kind": object_kind,
            "object_concept_id": arguments.get("target_concept_id"),
            "object_text": (
                {
                    "text": arguments["target_text"],
                    "language": arguments.get("language") or "en-NZ",
                }
                if object_kind == "text"
                else None
            ),
            "scope": {
                "mode": arguments["scope_mode"],
                "user_concept_id": arguments["acting_user_concept_id"],
                "organisation_concept_id": arguments[
                    "organisation_concept_id"
                ],
                "namespace": arguments["namespace"],
            },
            "provenance": {
                "asserted_by_user_concept_id": arguments[
                    "acting_user_concept_id"
                ],
                "evidence": arguments["evidence"],
            },
            "canonical_publication": False,
            "status": "asserted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": assertion_id,
            "assertion": readback,
            "canonical_read_back": readback,
            "canonical_publication": False,
        }

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        upsert,
    )
    common = {
        "subject_concept_id": "#V#marc_example",
        "scope_mode": "organisation",
        "evidence": {"source": "official staff page"},
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="catalogue-bare-concept-predicate",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            **common,
                            "predicate": "affiliated_with",
                            "target_concept_id": "#V#school_of_computer_science",
                        },
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="catalogue-bare-text-predicate",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            **common,
                            "predicate": "has_email",
                            "target_text": "marc@example.test",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="catalogue-exact-concept-predicate",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            **common,
                            "predicate": "#V#affiliated_with",
                            "target_concept_id": "#V#school_of_computer_science",
                        },
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="catalogue-exact-text-predicate",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            **common,
                            "predicate": "#V#has_email",
                            "target_text": "marc@example.test",
                            "language": "en-NZ",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response="Both exact assertions are represented."),
    )
    result = execute_adaptive_turn(
        gateway=InternalMCPGateway(
            catalogue=build_default_catalogue(),
            transport=InternalMCPTransport(
                read_timeout_sec=1.0,
                write_timeout_sec=1.0,
            ),
            enabled=True,
        ),
        prompt="Represent the exact School affiliation and email assertions.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-catalogue-exact-predicate-recovery",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    failures = result.tool_invocations[:2]
    recoveries = result.tool_invocations[2:]
    assert all(item["effect_status"] == "not_started" for item in failures)
    assert all(item["recovery_status"] == "succeeded" for item in failures)
    assert all(item.get("recovered_by_effect_id") for item in failures)
    assert all(item["effect_status"] == "succeeded" for item in recoveries)
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    assert result.response_text == "Both exact assertions are represented."


def test_existing_scoped_fact_canonical_read_completes_without_a_write() -> None:
    seen_calls: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        seen_calls.append((name, dict(arguments)))
        assert name == "general_read"
        return {
            "success": True,
            "found": True,
            "canonical_read_back": {
                "schema_version": "scoped_knowledge_assertion.v1",
                "assertion_id": "ska_existing_affiliation",
                "subject_concept_id": "#V#marc_example",
                "predicate": "#V#affiliated_with",
                "object_kind": "concept",
                "object_concept_id": "#V#school_of_computer_science",
                "scope_mode": "organisation",
                "status": "asserted",
            },
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-existing-scoped-fact",
                    payload={
                        "name": "general_read",
                        "arguments": {
                            "subject_concept_id": "#V#marc_example",
                            "predicate": "#V#affiliated_with",
                            "target_concept_id": (
                                "#V#school_of_computer_science"
                            ),
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The existing scoped fact was verified."),
    )
    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt=(
            "Ensure Marc Example's School affiliation is represented; read first "
            "and do not write if it already exists."
        ),
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-existing-scoped-fact-read",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert seen_calls == [
        (
            "general_read",
            {
                "subject_concept_id": "#V#marc_example",
                "predicate": "#V#affiliated_with",
                "target_concept_id": "#V#school_of_computer_science",
                "scope_mode": "organisation",
            },
        )
    ]
    assert all(
        invocation.get("effect_id") is None
        for invocation in result.tool_invocations
    )
    assert result.terminal_status == "completed"
    assert result.response_authority == "model"
    assert result.response_text == "The existing scoped fact was verified."


@pytest.mark.parametrize(
    ("recovery_language", "expected_recovery_status", "expected_terminal_status"),
    (
        ("en-NZ", "succeeded", "completed"),
        ("fr-FR", "mismatched", "effect_partially_completed"),
    ),
)
def test_scoped_text_recovery_uses_exact_defaulted_language(
    monkeypatch: pytest.MonkeyPatch,
    recovery_language: str,
    expected_recovery_status: str,
    expected_terminal_status: str,
) -> None:
    _stub_same_turn_ontology_delegation(monkeypatch)
    handler_calls: list[dict[str, Any]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "upsert_scoped_assertion"
        handler_calls.append(dict(arguments))
        if len(handler_calls) == 1:
            return {
                "success": False,
                "effect_status": "failed",
                "changed": False,
                "error_code": "scoped_assertion_write_failed",
                "recovery_affordances": [
                    {
                        "action_type": (
                            "retry_exact_scoped_assertion_with_canonical_predicate_id"
                        ),
                        "tool": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#marc_example",
                            "predicate": "#V#has_note",
                            "target_text": "Research note",
                            "scope_mode": "organisation",
                        },
                    }
                ],
            }

        assertion_id = "ska_text_language_recovery"
        organisation_id = arguments["organisation_concept_id"]
        readback = {
            "schema_version": "scoped_knowledge_assertion.v1",
            "assertion_id": assertion_id,
            "subject_concept_id": arguments["subject_concept_id"],
            "predicate": arguments["predicate"],
            "object_kind": "text",
            "object_concept_id": None,
            "object_text": {
                "text": arguments["target_text"],
                "language": arguments["language"],
            },
            "scope": {
                "mode": arguments["scope_mode"],
                "user_concept_id": arguments["acting_user_concept_id"],
                "organisation_concept_id": organisation_id,
                "namespace": arguments["namespace"],
                "audience_keys": [f"org:{organisation_id}"],
            },
            "provenance": {
                "asserted_by_user_concept_id": arguments[
                    "acting_user_concept_id"
                ],
                "organisation_concept_id": organisation_id,
                "namespace": arguments["namespace"],
            },
            "canonical_publication": False,
            "status": "asserted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": assertion_id,
            "canonical_read_back": readback,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="failed-default-language-assertion",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#marc_example",
                            "predicate": "#V#has_note",
                            "target_text": "Research note",
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="text-language-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": "#V#marc_example",
                            "predicate": "#V#has_note",
                            "target_text": "Research note",
                            "language": recovery_language,
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The text assertion was recovered."),
    )
    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Record this organisation-scoped research note.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"turn-text-language-recovery-{recovery_language}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    failure, recovery = result.tool_invocations
    assert failure["recovery_status"] == expected_recovery_status
    if expected_recovery_status == "succeeded":
        assert failure["recovered_by_effect_id"] == recovery["effect_id"]
    else:
        assert "recovered_by_effect_id" not in failure
        assert failure["attempted_recovery_effect_id"] == recovery["effect_id"]
    assert recovery["canonical_readback"]["object_text_identity_sha256"]
    assert result.terminal_status == expected_terminal_status
