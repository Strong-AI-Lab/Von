from __future__ import annotations

from representation_intent_regression_helpers import (
    build_turn_record as _build_record,
)
from representation_intent_regression_helpers import (
    patch_representation_profile_loader as _patch_representation_profile_loader,
)
from representation_intent_regression_helpers import (
    representation_profiles as _representation_profiles,
)


def test_paper_representation_contract_emitted_with_required_effects(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        aux_llm_calls=[
            {
                "type": "prompt_tool_requirements",
                "required_scholarly_representation_for_file_copy_ids": [
                    "#V#uploaded_file_copy_abc123"
                ],
            }
        ]
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("schema_version") == "required_effects_contract.v1"
    assert contract.get("intent_class") == "representation"
    assert contract.get("domain_profile_id") == "paper"
    assert contract.get("target_entity_class") == "scholarly_paper"
    assert isinstance(contract.get("contract_id"), str)
    assert contract.get("contract_id")

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "scholarly_representation"
    assert effect.get("required_tools") == ["interpret_file_copy"]
    assert effect.get("targets") == ["#V#uploaded_file_copy_abc123"]


def test_paper_url_representation_contract_requires_download_paper(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text=(
            "Download and represent metadata on this scientific paper "
            "https://arxiv.org/abs/2502.14996"
        ),
        response_text="Observed required tool execution.",
        tool_invocations=[
            {
                "tool": "download_paper",
                "payload": {
                    "success": True,
                    "arxiv_id": "2502.14996",
                    "computer_file_copy_concept_id": "#V#uploaded_file_copy_2502_14996",
                },
            },
            {
                "tool": "fetch_concept",
                "payload": {"success": True, "concept_id": "#V#paper_on_arxiv_2502_14996"},
            },
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract") or {}
    assert contract.get("artefact_source") == "url"

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("required_tools") == ["download_paper"]
    assert effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_representation_tool_success_false_marks_effect_unresolved(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text=(
            "Download and represent metadata on this scientific paper "
            "https://arxiv.org/abs/2502.14996"
        ),
        tool_invocations=[
            {
                "tool": "download_paper",
                "payload": {
                    "success": False,
                    "error": "arxiv_proxy_timeout",
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "paper_representation_tool_failed"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_person_representation_blocks_completion_when_identity_unresolved(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text="Represent this person profile from this CV file #V#uploaded_file_copy_person_1.",
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": False,
                    "error": "person_identity_unresolved",
                    "person_representation": {
                        "attempted": True,
                        "verified": False,
                        "reason": "person_identity_unresolved",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_person"
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "person_representation_tool_failed"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_person_representation_marks_completion_when_verified(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text="Represent this person profile from this CV file #V#uploaded_file_copy_person_1.",
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": True,
                    "person_representation": {
                        "attempted": True,
                        "verified": True,
                        "person_concept_id": "#V#person_jane_doe_1234abcd",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_person"
    assert effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_company_representation_blocks_completion_when_identity_unresolved(
    monkeypatch,
) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text="Represent this company from this web page file #V#uploaded_file_copy_company_1.",
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": False,
                    "error": "company_identity_unresolved",
                    "company_representation": {
                        "attempted": True,
                        "verified": False,
                        "reason": "company_identity_unresolved",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_company"
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "company_representation_tool_failed"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_company_representation_marks_completion_when_verified(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text="Represent this company from this web page file #V#uploaded_file_copy_company_1.",
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": True,
                    "company_representation": {
                        "attempted": True,
                        "verified": True,
                        "company_concept_id": "#V#company_example_labs_ltd_1234abcd",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_company"
    assert effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_meeting_representation_blocks_completion_when_identity_unresolved(
    monkeypatch,
) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text="Represent this meeting from this transcript file #V#uploaded_file_copy_meeting_1.",
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": False,
                    "error": "meeting_identity_unresolved",
                    "meeting_representation": {
                        "attempted": True,
                        "verified": False,
                        "reason": "meeting_identity_unresolved",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_meeting"
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "meeting_representation_tool_failed"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_meeting_representation_marks_completion_when_verified(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text="Represent this meeting from this transcript file #V#uploaded_file_copy_meeting_1.",
        response_text="Progress note.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": True,
                    "meeting_representation": {
                        "attempted": True,
                        "verified": True,
                        "meeting_concept_id": "#V#meeting_weekly_research_sync_1234abcd",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_meeting"
    assert effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_person_company_meeting_profiles_generate_non_empty_effects(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    person_record = _build_record(
        prompt_text="Create a person profile from this CV file #V#uploaded_file_copy_person_1."
    )
    person_contract = (
        person_record.get("execution", {}).get("required_effects_contract") or {}
    )
    person_effects = person_record.get("required_effects") or []
    assert person_contract.get("domain_profile_id") == "person"
    assert person_effects
    assert person_effects[0].get("effect_type") == "representation_person"

    company_record = _build_record(
        prompt_text=(
            "Represent this company from the web page "
            "https://example.org/about-us and store key metadata."
        )
    )
    company_contract = (
        company_record.get("execution", {}).get("required_effects_contract") or {}
    )
    company_effects = company_record.get("required_effects") or []
    assert company_contract.get("domain_profile_id") == "company"
    assert company_contract.get("artefact_source") == "url"
    assert company_effects
    assert company_effects[0].get("effect_type") == "representation_company"
    assert "extract_url" in list(company_effects[0].get("required_tools") or [])

    meeting_record = _build_record(
        prompt_text=(
            "Represent a meeting from this transcript file "
            "#V#uploaded_file_copy_meeting_1."
        )
    )
    meeting_contract = (
        meeting_record.get("execution", {}).get("required_effects_contract") or {}
    )
    meeting_effects = meeting_record.get("required_effects") or []
    assert meeting_contract.get("domain_profile_id") == "meeting"
    assert meeting_effects
    assert meeting_effects[0].get("effect_type") == "representation_meeting"


def test_representation_contract_is_idempotent_for_same_prompt_and_context(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    common_payload = {
        "request_id": "req-contract-idempotent",
        "session_id": "session-contract-idempotent",
        "prompt_text": (
            "Represent this company from the web page "
            "https://example.org/about-us."
        ),
        "aux_llm_calls": [],
    }

    record_a = _build_record(**common_payload)
    record_b = _build_record(**common_payload)

    contract_a = record_a.get("execution", {}).get("required_effects_contract")
    contract_b = record_b.get("execution", {}).get("required_effects_contract")
    assert contract_a == contract_b


def test_status_question_does_not_emit_representation_contract(monkeypatch) -> None:
    _patch_representation_profile_loader(
        monkeypatch, profiles=_representation_profiles()
    )
    record = _build_record(
        prompt_text=(
            "Did you make a scientific paper concept for "
            "#V#uploaded_file_copy_abc123 yet?"
        ),
        workflow_routing={
            "workflow_id": "#V#chat_assistant_workflow",
            "verdict": "plain_response",
            "source": "default",
        },
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None


def test_representation_contract_fails_closed_when_profile_catalogue_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_representation_domain_profiles_from_vontology",
        lambda: (
            [],
            {
                "representation_profile_source": "vontology_concept_text_relations",
                "requested_concept_ids": [
                    "#V#representation_contract_profile_paper",
                    "#V#representation_contract_profile_person",
                ],
                "loaded_concept_ids": [],
                "loaded_profile_count": 0,
                "profile_version_hash": None,
            },
        ),
    )

    record = _build_record(
        prompt_text=(
            "Fully represent the corresponding paper from "
            "#V#uploaded_file_copy_abc123."
        )
    )

    execution = record.get("execution") or {}
    contract = execution.get("required_effects_contract") or {}
    assert contract.get("domain_profile_id") == "representation"
    assert contract.get("profile_source") == "vontology_concept_text_relations"
    profile_resolution = contract.get("profile_resolution") or {}
    assert profile_resolution.get("fail_closed") is True
    assert profile_resolution.get("fail_closed_reason") == "profile_catalogue_unavailable"

    required_effects = record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_contract_guard"
    assert effect.get("status") == "not_executed"
    assert effect.get("failure_code") == "representation_profile_catalogue_unavailable"
