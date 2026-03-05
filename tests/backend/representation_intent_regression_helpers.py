from __future__ import annotations

from typing import Any

from src.backend.services.turn_execution_record_service import build_turn_execution_record

DEFAULT_REPRESENTATION_DECISION_POLICY: dict[str, bool] = {
    "completion_block_on_unresolved_effects": True,
    "fail_closed_on_missing_requirements": True,
    "auto_apply_low_risk_defaults": True,
    "requires_explicit_user_decision_for_high_risk": True,
}


def representation_profiles() -> list[dict[str, Any]]:
    return [
        {
            "profile_id": "paper",
            "profile_concept_id": "#V#representation_contract_profile_paper",
            "target_entity_class": "scholarly_paper",
            "effect_type": "scholarly_representation",
            "description": "Represent scholarly paper metadata.",
            "intent_patterns": [
                r"\bpaper\s+representation\b",
                r"\bcorresponding\s+paper\b",
            ],
            "domain_terms": ["paper", "arxiv", "abstract", "metadata"],
            "required_tools_by_source": {
                "file_copy": ["interpret_file_copy"],
                "url": ["download_paper"],
                "mixed": ["download_paper", "interpret_file_copy"],
                "unknown": ["interpret_file_copy"],
            },
            "required_predicates": [
                "#V#computer_file_for_propositional_information_thing",
                "#V#propositional_information_thing_has_computer_file",
            ],
            "default_decision_policy": dict(DEFAULT_REPRESENTATION_DECISION_POLICY),
        },
        {
            "profile_id": "person",
            "profile_concept_id": "#V#representation_contract_profile_person",
            "target_entity_class": "person",
            "effect_type": "representation_person",
            "description": "Represent person information from artefacts.",
            "intent_patterns": [
                r"\b(?:business\s+card|cv|curriculum\s+vitae|resume)\b.*\b(?:person|profile|contact)"
            ],
            "domain_terms": ["person", "business card", "cv", "resume", "contact"],
            "required_tools_by_source": {
                "file_copy": ["interpret_file_copy"],
                "url": ["extract_url"],
                "mixed": ["interpret_file_copy", "extract_url"],
                "unknown": ["interpret_file_copy"],
            },
            "required_predicates": ["#V#person"],
            "default_decision_policy": dict(DEFAULT_REPRESENTATION_DECISION_POLICY),
        },
        {
            "profile_id": "company",
            "profile_concept_id": "#V#representation_contract_profile_company",
            "target_entity_class": "company",
            "effect_type": "representation_company",
            "description": "Represent company information from artefacts.",
            "intent_patterns": [
                r"\b(?:company|organisation|organization|business|startup)\b.*\b(?:web\s?page|website|url)"
            ],
            "domain_terms": ["company", "organisation", "business", "website", "url"],
            "required_tools_by_source": {
                "file_copy": ["interpret_file_copy"],
                "url": ["extract_url"],
                "mixed": ["interpret_file_copy", "extract_url"],
                "unknown": ["extract_url"],
            },
            "required_predicates": ["#V#organisation"],
            "default_decision_policy": dict(DEFAULT_REPRESENTATION_DECISION_POLICY),
        },
        {
            "profile_id": "meeting",
            "profile_concept_id": "#V#representation_contract_profile_meeting",
            "target_entity_class": "meeting",
            "effect_type": "representation_meeting",
            "description": "Represent meeting information from artefacts.",
            "intent_patterns": [
                r"\b(?:meeting|calendar\s+event)\b.*\b(?:transcript|calendar|minutes|agenda)"
            ],
            "domain_terms": ["meeting", "transcript", "calendar", "minutes", "agenda"],
            "required_tools_by_source": {
                "file_copy": ["interpret_file_copy"],
                "url": ["extract_url"],
                "mixed": ["interpret_file_copy", "extract_url"],
                "unknown": ["interpret_file_copy"],
            },
            "required_predicates": ["#V#meeting"],
            "default_decision_policy": dict(DEFAULT_REPRESENTATION_DECISION_POLICY),
        },
    ]


def patch_representation_profile_loader(
    monkeypatch,
    *,
    profiles: list[dict[str, Any]] | None = None,
    profile_version_hash: str = "hash-test-profiles",
) -> list[dict[str, Any]]:
    resolved_profiles = profiles if profiles is not None else representation_profiles()
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_representation_domain_profiles_from_vontology",
        lambda: (
            resolved_profiles,
            {
                "representation_profile_source": "vontology_concept_text_relations",
                "requested_concept_ids": [
                    profile.get("profile_concept_id")
                    for profile in resolved_profiles
                    if isinstance(profile, dict)
                ],
                "loaded_concept_ids": [
                    profile.get("profile_concept_id")
                    for profile in resolved_profiles
                    if isinstance(profile, dict)
                ],
                "loaded_profile_count": len(resolved_profiles),
                "profile_version_hash": profile_version_hash,
            },
        ),
    )
    return resolved_profiles


def build_turn_record(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
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


def assert_low_risk_default_policy(contract: dict[str, Any]) -> None:
    policy = contract.get("default_decision_policy")
    assert isinstance(policy, dict)
    assert policy.get("completion_block_on_unresolved_effects") is True
    assert policy.get("fail_closed_on_missing_requirements") is True
    assert policy.get("auto_apply_low_risk_defaults") is True
    assert policy.get("requires_explicit_user_decision_for_high_risk") is True
