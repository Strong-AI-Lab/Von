from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from representation_intent_regression_helpers import (
    assert_low_risk_default_policy,
)
from representation_intent_regression_helpers import (
    build_turn_record,
)
from representation_intent_regression_helpers import (
    patch_representation_profile_loader,
)


@dataclass(frozen=True)
class TurnExecutionScenario:
    domain_id: str
    effect_type: str
    prompt_text: str
    required_tool: str
    artefact_target_id: str
    unresolved_payload: dict[str, Any]
    verified_payload: dict[str, Any]
    unresolved_failure_code: str
    verified_follow_up_invocations: tuple[dict[str, Any], ...] = ()


TURN_EXECUTION_SCENARIOS: tuple[TurnExecutionScenario, ...] = (
    TurnExecutionScenario(
        domain_id="paper",
        effect_type="scholarly_representation",
        prompt_text=(
            "Download and represent metadata on this scientific paper "
            "https://arxiv.org/abs/2502.14996"
        ),
        required_tool="download_paper",
        artefact_target_id="#V#uploaded_file_copy_2502_14996",
        unresolved_payload={"success": False, "error": "arxiv_proxy_timeout"},
        verified_payload={
            "success": True,
            "arxiv_id": "2502.14996",
            "computer_file_copy_concept_id": "#V#uploaded_file_copy_2502_14996",
        },
        unresolved_failure_code="paper_representation_tool_failed",
        verified_follow_up_invocations=(
            {
                "tool": "fetch_concept",
                "payload": {
                    "success": True,
                    "concept_id": "#V#paper_on_arxiv_2502_14996",
                },
            },
        ),
    ),
    TurnExecutionScenario(
        domain_id="person",
        effect_type="representation_person",
        prompt_text=(
            "Represent this person profile from this CV file "
            "#V#uploaded_file_copy_person_1."
        ),
        required_tool="interpret_file_copy",
        artefact_target_id="#V#uploaded_file_copy_person_1",
        unresolved_payload={
            "success": False,
            "error": "person_identity_unresolved",
            "person_representation": {
                "attempted": True,
                "verified": False,
                "reason": "person_identity_unresolved",
            },
        },
        verified_payload={
            "success": True,
            "person_representation": {
                "attempted": True,
                "verified": True,
                "person_concept_id": "#V#person_jane_doe_1234abcd",
            },
        },
        unresolved_failure_code="person_representation_tool_failed",
    ),
    TurnExecutionScenario(
        domain_id="company",
        effect_type="representation_company",
        prompt_text=(
            "Represent this company from this web page file "
            "#V#uploaded_file_copy_company_1."
        ),
        required_tool="interpret_file_copy",
        artefact_target_id="#V#uploaded_file_copy_company_1",
        unresolved_payload={
            "success": False,
            "error": "company_identity_unresolved",
            "company_representation": {
                "attempted": True,
                "verified": False,
                "reason": "company_identity_unresolved",
            },
        },
        verified_payload={
            "success": True,
            "company_representation": {
                "attempted": True,
                "verified": True,
                "company_concept_id": "#V#company_example_labs_ltd_1234abcd",
            },
        },
        unresolved_failure_code="company_representation_tool_failed",
    ),
    TurnExecutionScenario(
        domain_id="meeting",
        effect_type="representation_meeting",
        prompt_text=(
            "Represent this meeting from this transcript file "
            "#V#uploaded_file_copy_meeting_1."
        ),
        required_tool="interpret_file_copy",
        artefact_target_id="#V#uploaded_file_copy_meeting_1",
        unresolved_payload={
            "success": False,
            "error": "meeting_identity_unresolved",
            "meeting_representation": {
                "attempted": True,
                "verified": False,
                "reason": "meeting_identity_unresolved",
            },
        },
        verified_payload={
            "success": True,
            "meeting_representation": {
                "attempted": True,
                "verified": True,
                "meeting_concept_id": "#V#meeting_weekly_research_sync_1234abcd",
            },
        },
        unresolved_failure_code="meeting_representation_tool_failed",
    ),
)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _payload_for_required_target(
    scenario: TurnExecutionScenario,
    payload: dict[str, Any],
) -> dict[str, Any]:
    resolved_payload = dict(payload)
    if scenario.required_tool != "interpret_file_copy":
        return resolved_payload
    # interpret_file_copy success only satisfies the effect if it identifies the
    # same artefact target the contract derived from the prompt/context.
    resolved_payload.setdefault("concept_id", scenario.artefact_target_id)
    resolved_payload.setdefault("file_copy_concept_id", scenario.artefact_target_id)
    return resolved_payload


@pytest.mark.parametrize(
    "scenario",
    TURN_EXECUTION_SCENARIOS,
    ids=[scenario.domain_id for scenario in TURN_EXECUTION_SCENARIOS],
)
def test_cross_domain_unresolved_required_effects_block_completion(
    monkeypatch, scenario: TurnExecutionScenario
) -> None:
    patch_representation_profile_loader(monkeypatch)

    record = build_turn_record(
        prompt_text=scenario.prompt_text,
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": scenario.required_tool,
                "payload": _payload_for_required_target(
                    scenario,
                    scenario.unresolved_payload,
                ),
            }
        ],
    )

    execution = record.get("execution") or {}
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("domain_profile_id") == scenario.domain_id
    assert_low_risk_default_policy(contract)

    required_effects = record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == scenario.effect_type
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == scenario.unresolved_failure_code

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert scenario.unresolved_failure_code in list(
        completion_gate.get("blocking_failure_codes") or []
    )

    unresolved_preconditions = (
        (completion_gate.get("evidence_payload") or {}).get("unresolved_preconditions")
        or []
    )
    assert any(
        scenario.unresolved_failure_code in list(row.get("failure_codes") or [])
        for row in unresolved_preconditions
        if isinstance(row, dict)
    )


@pytest.mark.parametrize(
    "scenario",
    TURN_EXECUTION_SCENARIOS,
    ids=[scenario.domain_id for scenario in TURN_EXECUTION_SCENARIOS],
)
def test_cross_domain_verified_required_effects_allow_completion(
    monkeypatch, scenario: TurnExecutionScenario
) -> None:
    patch_representation_profile_loader(monkeypatch)

    record = build_turn_record(
        prompt_text=scenario.prompt_text,
        response_text="Progress note.",
        tool_invocations=(
            [
                {
                    "tool": scenario.required_tool,
                    "payload": _payload_for_required_target(
                        scenario,
                        scenario.verified_payload,
                    ),
                }
            ]
            + list(scenario.verified_follow_up_invocations)
        ),
    )

    required_effects = record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == scenario.effect_type
    assert effect.get("status") == "satisfied"
    assert effect.get("failure_codes") == []

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True
    assert completion_gate.get("blocking_failure_codes") == []


@pytest.mark.parametrize(
    "scenario",
    TURN_EXECUTION_SCENARIOS,
    ids=[scenario.domain_id for scenario in TURN_EXECUTION_SCENARIOS],
)
def test_cross_domain_contract_policy_and_idempotence(
    monkeypatch, scenario: TurnExecutionScenario
) -> None:
    patch_representation_profile_loader(monkeypatch)

    record_a = build_turn_record(
        request_id=f"req-{scenario.domain_id}-1",
        session_id=f"session-{scenario.domain_id}",
        prompt_text=scenario.prompt_text,
        tool_invocations=(
            [
                {
                    "tool": scenario.required_tool,
                    "payload": _payload_for_required_target(
                        scenario,
                        scenario.verified_payload,
                    ),
                }
            ]
            + list(scenario.verified_follow_up_invocations)
        ),
    )
    record_b = build_turn_record(
        request_id=f"req-{scenario.domain_id}-2",
        session_id=f"session-{scenario.domain_id}",
        prompt_text=scenario.prompt_text,
        tool_invocations=(
            [
                {
                    "tool": scenario.required_tool,
                    "payload": _payload_for_required_target(
                        scenario,
                        scenario.verified_payload,
                    ),
                }
            ]
            + list(scenario.verified_follow_up_invocations)
        ),
    )

    contract_a = ((record_a.get("execution") or {}).get("required_effects_contract")) or {}
    contract_b = ((record_b.get("execution") or {}).get("required_effects_contract")) or {}
    assert contract_a.get("domain_profile_id") == scenario.domain_id
    assert contract_b.get("domain_profile_id") == scenario.domain_id
    assert_low_risk_default_policy(contract_a)
    assert_low_risk_default_policy(contract_b)
    assert contract_a.get("contract_id") == contract_b.get("contract_id")
    assert record_a.get("required_effects") == record_b.get("required_effects")
    assert record_a.get("completion_gate") == record_b.get("completion_gate")


@pytest.mark.parametrize(
    "scenario",
    tuple(
        scenario
        for scenario in TURN_EXECUTION_SCENARIOS
        if scenario.required_tool == "interpret_file_copy"
    ),
    ids=[
        scenario.domain_id
        for scenario in TURN_EXECUTION_SCENARIOS
        if scenario.required_tool == "interpret_file_copy"
    ],
)
def test_cross_domain_verified_effect_requires_matching_target(
    monkeypatch, scenario: TurnExecutionScenario
) -> None:
    patch_representation_profile_loader(monkeypatch)

    record = build_turn_record(
        prompt_text=scenario.prompt_text,
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": scenario.required_tool,
                "payload": dict(scenario.verified_payload),
            }
        ],
    )

    required_effects = record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    assert effect.get("status") == "not_executed"
    assert effect.get("failure_code") == f"{scenario.domain_id}_representation_wrong_target"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False


@dataclass(frozen=True)
class GatewayScenario:
    domain_id: str
    original_filename: str
    content_text: str
    representation_key: str
    verification_predicate: str
    verification_error: str
    unresolved_reason: str


GATEWAY_SCENARIOS: tuple[GatewayScenario, ...] = (
    GatewayScenario(
        domain_id="paper",
        original_filename="paper-notes.txt",
        content_text="Research note draft with no arXiv identifier.",
        representation_key="scholarly_representation",
        verification_predicate="#V#scholarly_representation_verification",
        verification_error="scholarly_representation_not_verified",
        unresolved_reason="paper_identity_unresolved",
    ),
    GatewayScenario(
        domain_id="person",
        original_filename="candidate-cv.txt",
        content_text="Candidate CV details with role history.",
        representation_key="person_representation",
        verification_predicate="#V#person_representation_verification",
        verification_error="person_representation_not_verified",
        unresolved_reason="person_identity_unresolved",
    ),
    GatewayScenario(
        domain_id="company",
        original_filename="company-webpage.txt",
        content_text="Company profile and product page summary.",
        representation_key="company_representation",
        verification_predicate="#V#company_representation_verification",
        verification_error="company_representation_not_verified",
        unresolved_reason="company_identity_unresolved",
    ),
    GatewayScenario(
        domain_id="meeting",
        original_filename="meeting-transcript.txt",
        content_text="Meeting transcript with attendees and decisions.",
        representation_key="meeting_representation",
        verification_predicate="#V#meeting_representation_verification",
        verification_error="meeting_representation_not_verified",
        unresolved_reason="meeting_identity_unresolved",
    ),
)


def _patch_interpret_file_copy_for_domain_failure(
    monkeypatch, scenario: GatewayScenario
) -> None:
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": scenario.content_text,
            "content_type": "text/plain",
            "original_filename": scenario.original_filename,
            "size_bytes": 96,
            "byte_length": 96,
            "blob": {
                "backend": "local",
                "key": f"imports/user/hash/{scenario.original_filename}",
            },
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": f"Document text extracted: {scenario.content_text}",
            "subject_tags": ["document"],
            "content_text": scenario.content_text,
            "content_length": len(scenario.content_text),
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-cross-domain"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_arxiv_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": True,
            "verified": True,
            "paper_concept_id": "#V#paper_gateway_cross_domain",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": True,
            "verified": True,
            "paper_concept_id": "#V#paper_gateway_cross_domain",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.person_file_representation_service.materialise_person_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": False,
            "verified": False,
            "reason": "not_applicable",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.materialise_company_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": False,
            "verified": False,
            "reason": "not_applicable",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.meeting_file_representation_service.materialise_meeting_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": False,
            "verified": False,
            "reason": "not_applicable",
        },
    )

    unresolved_payload = {
        "success": False,
        "attempted": True,
        "verified": False,
        "reason": scenario.unresolved_reason,
    }
    if scenario.domain_id == "paper":
        monkeypatch.setattr(
            "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_arxiv_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
        monkeypatch.setattr(
            "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    elif scenario.domain_id == "person":
        monkeypatch.setattr(
            "src.backend.services.person_file_representation_service.materialise_person_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    elif scenario.domain_id == "company":
        monkeypatch.setattr(
            "src.backend.services.company_file_representation_service.materialise_company_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    elif scenario.domain_id == "meeting":
        monkeypatch.setattr(
            "src.backend.services.meeting_file_representation_service.materialise_meeting_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    else:
        raise AssertionError(f"Unknown gateway scenario domain: {scenario.domain_id}")


@pytest.mark.parametrize(
    "scenario",
    GATEWAY_SCENARIOS,
    ids=[scenario.domain_id for scenario in GATEWAY_SCENARIOS],
)
def test_gateway_cross_domain_fail_closed_reason_codes(
    monkeypatch, scenario: GatewayScenario
) -> None:
    gateway = _build_gateway()
    _patch_interpret_file_copy_for_domain_failure(monkeypatch, scenario)

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {
            "concept_id": "#V#imported_file_gateway",
            "namespace": "#V#user@org",
        },
    ).payload

    assert interpreted.get("success") is False
    representation_payload = interpreted.get(scenario.representation_key) or {}
    assert representation_payload.get("attempted") is True
    assert representation_payload.get("verified") is False
    assert representation_payload.get("reason") == scenario.unresolved_reason

    persist_errors = interpreted.get("persist_errors") or []
    assert any(
        row.get("predicate") == scenario.verification_predicate
        and row.get("error") == scenario.verification_error
        for row in persist_errors
        if isinstance(row, dict)
    )
