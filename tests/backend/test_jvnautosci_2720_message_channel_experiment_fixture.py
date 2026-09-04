from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_PATH = (
    _REPO_ROOT
    / "scripts"
    / "fixtures"
    / "jvnautosci_2720_message_channel_experiment.json"
)
_SESSION_ID = "1eec920c-0be0-4fcf-a045-6733768c8667"
_USER_ID = "#V#michael_witbrock"
_ORGANISATION_ID = "#V#the_lu_witbrock_household"
_NAMESPACE = "#V#michael_witbrock@the_lu_witbrock_household"
_HISTORY_INDICES = [49, 57, 58, 69, 70, 74, 75, 76, 77, 81, 82, 85]
_CONTRACT_KEYS = {
    "allowed_first_capabilities",
    "required_success_capability",
    "after_capability_failure",
    "focused_clarification_verdict",
    "unsolicited_alternative_policy",
    "read_only",
}


def _fixture() -> dict[str, Any]:
    value = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _all_mapping_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_all_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_mapping_keys(item))
    return keys


def test_fixture_binds_the_exact_source_without_embedding_a_candidate() -> None:
    fixture = _fixture()
    source = fixture["source_conversation"]
    reference = source["conversation_ref"]

    assert fixture["schema_version"] == (
        "jvnautosci_2720_message_channel_experiment_fixture.v1"
    )
    assert reference == {
        "schema_version": "conversation_reference.v1",
        "binding_kind": "explicit_session_id",
        "session_id": _SESSION_ID,
        "user_concept_id": _USER_ID,
        "namespace": _NAMESPACE,
        "organisation_concept_id": _ORGANISATION_ID,
        "include_legacy": False,
    }
    assert source["chat_history_lookup"] == {
        "user_id": _USER_ID,
        "session_id": _SESSION_ID,
        "namespace": _NAMESPACE,
        "include_legacy": False,
    }
    assert source["history_indices"] == _HISTORY_INDICES
    assert source["include_execution_evidence"] is True
    assert source["exclude_from_acting_trials"] is True
    assert source["exclude_from_blind_evaluator"] is True

    formation = fixture["candidate_formation"]
    instruction = formation["instruction"]
    assert all(
        phrase in instruction
        for phrase in (
            "Inspect only the designated source-conversation messages",
            "author one concise and reusable learning candidate",
            "capture it as non-active",
            "return no_durable_lesson",
        )
    )
    assert formation["capture_source"] == {
        "kind": "conversation",
        "session_id": _SESSION_ID,
        "namespace": _NAMESPACE,
        "include_legacy": False,
        "include_execution_evidence": True,
        "history_indices": _HISTORY_INDICES,
    }
    assert set(formation["projection_policy"]["withhold"]) >= {
        "cases",
        "blind_evaluator",
        "global_rubric",
        "decision_rule",
    }
    assert not any(case["prompt"] in instruction for case in fixture["cases"])
    assert "message_list_direct" not in instruction
    assert "gmail_list_messages" not in instruction

    forbidden_keys = {
        "advice_body",
        "candidate_body",
        "candidate_text",
        "draft_candidate",
        "retrieval_reason",
    }
    assert not (_all_mapping_keys(fixture) & forbidden_keys)


def test_candidate_formation_uses_only_bounded_semantic_reference_allowlists() -> None:
    formation = _fixture()["candidate_formation"]
    references = formation["allowed_semantic_references"]

    assert references == {
        "target_concept_ids": [
            "#V#communicating",
        ],
        "contributor_concept_ids": [_USER_ID, "#V#von_system"],
        "audience_concept_ids": [_ORGANISATION_ID],
        "beneficiary_concept_ids": [_USER_ID, _ORGANISATION_ID],
        "purpose_concept_ids": [],
    }
    assert all(
        concept_id.startswith("#V#")
        for values in references.values()
        for concept_id in values
    )
    output = formation["output_contract"]
    assert output["allowed_outcomes"] == [
        "capture_one_candidate",
        "no_durable_lesson",
    ]
    assert output["maximum_candidates"] == 1
    assert output["required_lifecycle_state"] == "non_active"
    assert output["visibility_scope"] == "actor"
    assert output["efficacy_not_yet_established"] is True


def test_fixture_freezes_six_cases_and_evaluator_only_generic_contracts() -> None:
    fixture = _fixture()
    cases = fixture["cases"]

    assert [(case["case_id"], case["kind"], case["prompt"]) for case in cases] == [
        (
            "app_household_von_catchup",
            "applicable",
            (
                "I’m catching up on the Lu–Witbrock household conversation in Von. "
                "Please summarise the recent messages for me."
            ),
        ),
        (
            "app_task_switch_from_gmail",
            "applicable",
            (
                "We can leave the Gmail access problem for later. Right now I need to "
                "catch up on the household conversation in Von. What have people "
                "messaged me about recently?"
            ),
        ),
        (
            "app_channel_neutral_digest",
            "applicable",
            "Give me a concise digest of the latest messages I received.",
        ),
        (
            "app_channel_neutral_brief",
            "applicable",
            "Brief me on new messages addressed to me.",
        ),
        (
            "control_explicit_gmail",
            "control",
            "Summarise the five most recent emails in my Gmail inbox.",
        ),
        (
            "control_explicit_von_direct",
            "control",
            "Summarise my five most recent Von direct messages.",
        ),
    ]
    assert sum(case["kind"] == "applicable" for case in cases) == 4
    assert sum(case["kind"] == "control" for case in cases) == 2
    assert all(set(case["evaluation_contract"]) == _CONTRACT_KEYS for case in cases)
    assert all(case["evaluation_contract"]["read_only"] is True for case in cases)

    by_id = {case["case_id"]: case["evaluation_contract"] for case in cases}
    for case_id in ("app_household_von_catchup", "app_task_switch_from_gmail"):
        assert by_id[case_id] == {
            "allowed_first_capabilities": ["message_list_direct"],
            "required_success_capability": "message_list_direct",
            "after_capability_failure": "report_specific_failure",
            "focused_clarification_verdict": "fail",
            "unsolicited_alternative_policy": "fail",
            "read_only": True,
        }
    for case_id in ("app_channel_neutral_digest", "app_channel_neutral_brief"):
        assert by_id[case_id] == {
            "allowed_first_capabilities": [
                "message_list_direct",
                "gmail_list_messages",
            ],
            "required_success_capability": None,
            "after_capability_failure": "try_other_plausible_capability",
            "focused_clarification_verdict": "partial",
            "unsolicited_alternative_policy": "allowed_for_recovery",
            "read_only": True,
        }
    assert by_id["control_explicit_gmail"]["allowed_first_capabilities"] == [
        "gmail_list_messages"
    ]
    assert by_id["control_explicit_gmail"]["required_success_capability"] == (
        "gmail_list_messages"
    )
    assert by_id["control_explicit_von_direct"]["allowed_first_capabilities"] == [
        "message_list_direct"
    ]
    assert by_id["control_explicit_von_direct"]["required_success_capability"] == (
        "message_list_direct"
    )


def test_evaluator_rubric_model_and_decision_inputs_are_immutable_and_bounded() -> None:
    fixture = _fixture()
    evaluator = fixture["blind_evaluator"]
    rubric = fixture["global_rubric"]

    assert (
        evaluator["content_sha256"]
        == hashlib.sha256(evaluator["prompt"].encode("utf-8")).hexdigest()
    )
    assert "external_availability_changed" not in evaluator["prompt"]
    assert "material_failure as a boolean" in evaluator["prompt"]
    rubric_payload = copy.deepcopy(rubric)
    rubric_digest = rubric_payload.pop("definition_sha256")
    assert rubric_digest == _canonical_sha256(rubric_payload)
    assert set(rubric["verdicts"]) == {"pass", "partial", "fail", "inconclusive"}
    assert rubric["persistence_policy"]["persist_raw_message_content"] is False

    for case in fixture["cases"]:
        assert case["evaluator_concept_id"] == evaluator["concept_id"]
        assert case["evaluator_sha256"] == evaluator["content_sha256"]
        assert case["rubric_concept_id"] == rubric["concept_id"]
        assert case["rubric_sha256"] == rubric["definition_sha256"]

    experiment = fixture["experiment"]
    assert experiment["arms"] == ["A", "B"]
    assert experiment["randomisation_seed"] == 2720
    assert experiment["repeats"] == 2
    assert experiment["model"] == {
        "provider": "openai",
        "model_id": "gpt-5.6-luna",
        "parameters": {"temperature": 0.2, "max_output_tokens": 4000},
    }
    assert fixture["decision_rule"] == {
        "minimum_applicable_b_passes": 6,
        "minimum_applicable_pass_delta": 2,
        "require_more_paired_improvements_than_regressions": True,
        "forbid_b_only_material_failure": True,
        "require_control_non_inferiority": True,
    }
    assert fixture["decision_interpretation"]["activation_authorised"] is False
