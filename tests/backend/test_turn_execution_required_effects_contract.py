from __future__ import annotations

from src.backend.services.turn_execution_record_service import build_turn_execution_record


def _build_record(**overrides):
    payload = {
        "request_id": "req-contract-1",
        "session_id": "session-contract-1",
        "namespace": "#V#test_user@test_org",
        "actor_concept_id": None,
        "user_id": "#V#test_user",
        "org_id": "#V#test_org",
        "prompt_text": "Fully represent the corresponding paper.",
        "response_text": "Representation done.",
        "interaction_timestamp_utc": "2026-03-05T00:00:00Z",
        "workflow_discovery": None,
        "workflow_routing": {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
            "source": "default",
        },
        "tool_invocations": [],
        "turn_execution_diagnostics": None,
        "aux_llm_calls": [],
    }
    payload.update(overrides)
    return build_turn_execution_record(**payload)


def test_paper_representation_contract_emitted_with_required_effects() -> None:
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


def test_person_company_meeting_profiles_generate_non_empty_effects() -> None:
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


def test_representation_contract_is_idempotent_for_same_prompt_and_context() -> None:
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


def test_status_question_does_not_emit_representation_contract() -> None:
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
