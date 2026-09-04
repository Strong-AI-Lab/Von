from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

import pytest

from src.backend.services.learning_advice_experiment_service import (
    LearningAdviceExperimentAccessError,
    LearningAdviceExperimentDriftError,
    LearningAdviceExperimentError,
    bind_fresh_trial_identities,
    build_learning_advice_experiment_manifest,
    build_learning_advice_experiment_plan,
    build_learning_advice_experiment_run_binding,
    build_learning_advice_projection_for_trial,
    compute_learning_advice_paired_results,
    derive_learning_advice_evaluator_verdict,
    evaluate_learning_advice_content_decision,
    frozen_candidate_ref,
    is_sol_family_model_id,
    load_frozen_learning_candidate,
    validate_learning_advice_experiment_manifest,
    validate_learning_advice_experiment_run_binding,
)

_ACTOR_ID = "#V#michael_witbrock"
_ORGANISATION_ID = "#V#the_lu_witbrock_household"
_NAMESPACE = "#V#michael_witbrock@the_lu_witbrock_household"
_CANDIDATE_ID = "#V#learning_candidate_message_channel"
_CANDIDATE_BODY = (
    "Keep an unresolved communication channel open until the current wording "
    "or purpose discriminates, and reconsider other authorised channels after "
    "a selected channel fails."
)
_BODY_SHA256 = hashlib.sha256(_CANDIDATE_BODY.encode("utf-8")).hexdigest()
_REVISION_IDENTITY_SHA256 = "a" * 64
_SOURCE_LOCATOR_SHA256 = "b" * 64
_EVALUATOR_SHA256 = "c" * 64
_RUBRIC_SHA256 = "d" * 64


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate() -> dict[str, Any]:
    return {
        "schema_version": "learning_candidate.v1",
        "lifecycle_state": "non_active",
        "evaluation_disposition": "undecided",
        "candidate_id": _CANDIDATE_ID,
        "revision": 3,
        "body": _CANDIDATE_BODY,
        "body_sha256": _BODY_SHA256,
        "revision_identity_sha256": _REVISION_IDENTITY_SHA256,
        "source_locator_sha256": _SOURCE_LOCATOR_SHA256,
        "target_concept_ids": ["#V#ordinary_turn_capability_choice"],
        "authorship": {"author_concept_id": "#V#von_system"},
        "visibility": {
            "scope": "actor",
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
        },
        "namespace": _NAMESPACE,
        "source": {"kind": "conversation"},
    }


def _cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(1, 7):
        neutral = index in {3, 4}
        explicit_gmail = index == 5
        required_channel = (
            "gmail_list_messages" if explicit_gmail else "message_list_direct"
        )
        rows.append(
            {
                "case_id": f"held-out-{index:03d}",
                "kind": "applicable" if index <= 4 else "control",
                "prompt": f"Held-out communication request {index}",
                "evaluator_concept_id": "#V#message_channel_blind_evaluator",
                "evaluator_sha256": _EVALUATOR_SHA256,
                "rubric_concept_id": (
                    "#V#ambiguous_message_rubric"
                    if index <= 4
                    else "#V#explicit_message_channel_control_rubric"
                ),
                "rubric_sha256": _RUBRIC_SHA256,
                "evaluation_contract": {
                    "allowed_first_capabilities": (
                        ["message_list_direct", "gmail_list_messages"]
                        if neutral
                        else [required_channel]
                    ),
                    "required_success_capability": (
                        None if neutral else required_channel
                    ),
                    "after_capability_failure": (
                        "try_other_plausible_capability"
                        if neutral
                        else "report_specific_failure"
                    ),
                    "focused_clarification_verdict": ("partial" if neutral else "fail"),
                    "unsolicited_alternative_policy": (
                        "allowed_for_recovery" if neutral else "fail"
                    ),
                    "read_only": True,
                },
            }
        )
    return rows


def _manifest(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "experiment_spec_id": "#V#message_channel_advice_experiment_v1",
        "experiment_run_id": "#V#message_channel_advice_run_001",
        "candidate_ref": frozen_candidate_ref(_candidate()),
        "actor_user_id": _ACTOR_ID,
        "organisation_concept_id": _ORGANISATION_ID,
        "namespace": _NAMESPACE,
        "provider": "openai",
        "model_id": "gpt-5.6-luna",
        "model_parameters": {
            "temperature": 0.2,
            "max_output_tokens": 4_000,
        },
        "runtime_snapshot": {
            "code_revision": "test-tree-2720",
            "capability_catalogue_sha256": "1" * 64,
            "workflow_catalogue_sha256": "2" * 64,
            "relevant_ontology_sha256": "3" * 64,
            "acting_support_sha256": "4" * 64,
            "context_budget_tokens": 32_000,
            "turn_budget_seconds": 180.0,
            "final_synthesis_reserve_seconds": 30.0,
            "final_answer_reserve_seconds": 0.0,
        },
        "cases": _cases(),
        "decision_rule": {
            "minimum_applicable_b_passes": 6,
            "minimum_applicable_pass_delta": 2,
            "require_more_paired_improvements_than_regressions": True,
            "forbid_b_only_material_failure": True,
            "require_control_non_inferiority": True,
        },
        "randomisation_seed": 2_720,
    }
    values.update(overrides)
    return build_learning_advice_experiment_manifest(**values)


def test_manifest_is_canonical_body_free_and_carries_frozen_research_inputs() -> None:
    first = _manifest()
    second = _manifest()

    assert first == second
    assert validate_learning_advice_experiment_manifest(first) == first
    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert _CANDIDATE_BODY not in encoded
    assert "retrieval_reason" not in encoded
    assert "candidate_body" not in encoded
    assert first["candidate_ref"] == {
        "candidate_id": _CANDIDATE_ID,
        "revision": 3,
        "evaluation_disposition": "undecided",
        "body_sha256": _BODY_SHA256,
        "revision_identity_sha256": _REVISION_IDENTITY_SHA256,
        "source_locator_sha256": _SOURCE_LOCATOR_SHA256,
    }
    assert first["trusted_scope"] == {
        "actor_user_id": _ACTOR_ID,
        "organisation_concept_id": _ORGANISATION_ID,
        "namespace": _NAMESPACE,
    }
    assert first["model"] == {
        "provider": "openai",
        "model_id": "gpt-5.6-luna",
        "parameters": {"max_output_tokens": 4_000, "temperature": 0.2},
    }
    assert first["runtime_snapshot"]["code_revision"] == "test-tree-2720"
    assert first["runtime_snapshot"]["turn_budget_seconds"] == 180.0
    assert first["repeats"] == 2
    assert len(first["cases"]) == 6
    assert all(
        item["evaluator_concept_id"].startswith("#V#") for item in first["cases"]
    )
    assert all(item["evaluator_sha256"] == _EVALUATOR_SHA256 for item in first["cases"])
    assert all(item["rubric_concept_id"].startswith("#V#") for item in first["cases"])
    assert all(item["rubric_sha256"] == _RUBRIC_SHA256 for item in first["cases"])
    assert first["decision_rule"]["minimum_applicable_b_passes"] == 6

    digest_payload = copy.deepcopy(first)
    supplied_digest = digest_payload.pop("manifest_sha256")
    canonical_json = json.dumps(
        digest_payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert supplied_digest == hashlib.sha256(canonical_json.encode()).hexdigest()


@pytest.mark.parametrize(
    "model_id",
    [
        "sol",
        "SOL",
        "gpt-5.6-sol",
        "openai/gpt-5.6-sol",
        "gpt-5.6-sol-preview-2026-09-01",
    ],
)
def test_manifest_rejects_every_sol_family_model_form(model_id: str) -> None:
    assert is_sol_family_model_id(model_id) is True
    with pytest.raises(LearningAdviceExperimentError) as error:
        _manifest(model_id=model_id)
    assert error.value.reason_code == "learning_advice_experiment_sol_model_forbidden"


def test_explicit_non_sol_test_identifier_is_not_misclassified() -> None:
    assert is_sol_family_model_id("non-sol-test") is False
    assert _manifest(model_id="non-sol-test")["model"]["model_id"] == "non-sol-test"


@pytest.mark.parametrize("model_id", ["", "auto", "default", "latest"])
def test_manifest_requires_an_explicit_model(model_id: str) -> None:
    with pytest.raises(LearningAdviceExperimentError) as error:
        _manifest(model_id=model_id)
    assert error.value.reason_code in {
        "learning_advice_experiment_invalid",
        "learning_advice_experiment_explicit_model_required",
    }


def test_manifest_rejects_candidate_body_and_free_text_retrieval_reason() -> None:
    candidate_ref = frozen_candidate_ref(_candidate())
    candidate_ref["body"] = _CANDIDATE_BODY
    with pytest.raises(LearningAdviceExperimentError) as body_error:
        _manifest(candidate_ref=candidate_ref)
    assert body_error.value.reason_code == (
        "learning_advice_experiment_semantic_text_forbidden"
    )

    manifest = _manifest()
    manifest["retrieval_reason"] = "This sounds useful."
    with pytest.raises(LearningAdviceExperimentError) as reason_error:
        validate_learning_advice_experiment_manifest(manifest)
    assert reason_error.value.reason_code == (
        "learning_advice_experiment_semantic_text_forbidden"
    )


@pytest.mark.parametrize(
    ("cases", "repeats"),
    [
        (_cases()[:5], 2),
        (
            [
                {**item, "kind": "applicable" if index <= 5 else "control"}
                for index, item in enumerate(_cases(), start=1)
            ],
            2,
        ),
        (_cases(), 1),
    ],
)
def test_manifest_enforces_case_distribution_and_repeat_floor(
    cases: list[dict[str, Any]], repeats: int
) -> None:
    with pytest.raises(LearningAdviceExperimentError):
        _manifest(cases=cases, repeats=repeats)


def test_seeded_plan_has_24_unique_balanced_paired_trials() -> None:
    manifest = _manifest()
    plan = build_learning_advice_experiment_plan(manifest)

    assert plan == build_learning_advice_experiment_plan(manifest)
    assert plan["pair_count"] == 12
    assert plan["trial_count"] == 24
    assert Counter(trial["arm"] for trial in plan["trials"]) == {"A": 12, "B": 12}
    assert len({trial["trial_id"] for trial in plan["trials"]}) == 24
    assert [trial["order_index"] for trial in plan["trials"]] == list(range(1, 25))
    assert plan["runtime_snapshot"] == manifest["runtime_snapshot"]

    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    placeholder_values: list[str] = []
    for trial in plan["trials"]:
        by_pair[trial["pair_id"]].append(trial)
        placeholder_values.extend(trial["fresh_identity_placeholders"].values())
    assert len(placeholder_values) == 72
    assert len(set(placeholder_values)) == 72

    first_arm_counts: Counter[str] = Counter()
    case_repeat_arms: dict[tuple[str, int], set[str]] = defaultdict(set)
    for pair in by_pair.values():
        assert len(pair) == 2
        assert [trial["pair_position"] for trial in pair] == [1, 2]
        assert {trial["arm"] for trial in pair} == {"A", "B"}
        assert len({trial["case_id"] for trial in pair}) == 1
        assert len({trial["repeat"] for trial in pair}) == 1
        first_arm_counts[pair[0]["arm"]] += 1
        for trial in pair:
            case_repeat_arms[(trial["case_id"], trial["repeat"])].add(trial["arm"])
    assert first_arm_counts == {"A": 6, "B": 6}
    assert len(case_repeat_arms) == 12
    assert all(arms == {"A", "B"} for arms in case_repeat_arms.values())


def test_candidate_is_reloaded_and_exactly_attested_for_every_trial() -> None:
    manifest = _manifest()
    trials = build_learning_advice_experiment_plan(manifest)["trials"][:2]
    calls: list[tuple[str, dict[str, Any]]] = []

    def loader(candidate_id: str, **scope: Any) -> dict[str, Any]:
        calls.append((candidate_id, scope))
        return _candidate()

    projections = [
        build_learning_advice_projection_for_trial(
            manifest,
            trial,
            actor_user_id=_ACTOR_ID,
            organisation_concept_id=_ORGANISATION_ID,
            namespace=_NAMESPACE,
            candidate_loader=loader,
        )
        for trial in trials
    ]

    assert len(calls) == 2
    assert all(call[0] == _CANDIDATE_ID for call in calls)
    assert all(
        call[1]
        == {
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
            "namespace": _NAMESPACE,
        }
        for call in calls
    )
    by_arm = {projection["arm"]: projection for projection in projections}
    assert by_arm["A"]["items"] == []
    assert by_arm["B"]["items"][0]["body"] == _CANDIDATE_BODY
    assert all(
        projection["retrieval_basis"] == "frozen_candidate_revision"
        for projection in projections
    )
    assert "retrieval_reason" not in json.dumps(projections)


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("revision", 4),
        ("revision_identity_sha256", "c" * 64),
        ("source_locator_sha256", "d" * 64),
    ],
)
def test_candidate_identity_drift_aborts_trial(field: str, changed_value: Any) -> None:
    manifest = _manifest()
    changed = _candidate()
    changed[field] = changed_value

    with pytest.raises(LearningAdviceExperimentDriftError) as error:
        load_frozen_learning_candidate(
            manifest,
            actor_user_id=_ACTOR_ID,
            organisation_concept_id=_ORGANISATION_ID,
            namespace=_NAMESPACE,
            candidate_loader=lambda *_args, **_kwargs: changed,
        )
    assert error.value.reason_code == (
        "learning_advice_experiment_candidate_identity_drift"
    )


def test_candidate_body_digest_drift_aborts_trial() -> None:
    changed = _candidate()
    changed["body"] += " Changed after manifest freeze."

    with pytest.raises(LearningAdviceExperimentDriftError) as error:
        load_frozen_learning_candidate(
            _manifest(),
            actor_user_id=_ACTOR_ID,
            organisation_concept_id=_ORGANISATION_ID,
            namespace=_NAMESPACE,
            candidate_loader=lambda *_args, **_kwargs: changed,
        )
    assert error.value.reason_code == "learning_advice_experiment_candidate_body_drift"


def test_manifest_scope_mismatch_fails_before_candidate_read() -> None:
    called = False

    def loader(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return _candidate()

    with pytest.raises(LearningAdviceExperimentAccessError) as error:
        load_frozen_learning_candidate(
            _manifest(),
            actor_user_id="#V#different_actor",
            organisation_concept_id=_ORGANISATION_ID,
            namespace=_NAMESPACE,
            candidate_loader=loader,
        )
    assert error.value.reason_code == "learning_advice_experiment_scope_mismatch"
    assert called is False


def test_candidate_visibility_or_namespace_drift_aborts_trial() -> None:
    for change in (
        {"namespace": "#V#michael_witbrock@different_org"},
        {
            "visibility": {
                "scope": "actor",
                "actor_user_id": "#V#different_actor",
                "organisation_concept_id": _ORGANISATION_ID,
            }
        },
    ):
        changed = {**_candidate(), **change}
        with pytest.raises(LearningAdviceExperimentAccessError) as error:
            load_frozen_learning_candidate(
                _manifest(),
                actor_user_id=_ACTOR_ID,
                organisation_concept_id=_ORGANISATION_ID,
                namespace=_NAMESPACE,
                candidate_loader=lambda *_args, _changed=changed, **_kwargs: _changed,
            )
        assert error.value.reason_code == (
            "learning_advice_experiment_candidate_scope_mismatch"
        )


def test_tampered_trial_cannot_select_a_different_arm_or_prompt() -> None:
    manifest = _manifest()
    trial = copy.deepcopy(build_learning_advice_experiment_plan(manifest)["trials"][0])
    trial["arm"] = "B" if trial["arm"] == "A" else "A"

    with pytest.raises(LearningAdviceExperimentError) as error:
        build_learning_advice_projection_for_trial(
            manifest,
            trial,
            actor_user_id=_ACTOR_ID,
            organisation_concept_id=_ORGANISATION_ID,
            namespace=_NAMESPACE,
            candidate_loader=lambda *_args, **_kwargs: _candidate(),
        )
    assert error.value.reason_code == "learning_advice_experiment_trial_invalid"


def test_exact_candidate_body_cannot_be_smuggled_into_acting_prompt() -> None:
    cases = _cases()
    cases[0]["prompt"] = _CANDIDATE_BODY
    manifest = _manifest(cases=cases)
    trial = next(
        item
        for item in build_learning_advice_experiment_plan(manifest)["trials"]
        if item["case_id"] == cases[0]["case_id"]
    )

    with pytest.raises(LearningAdviceExperimentError) as error:
        build_learning_advice_projection_for_trial(
            manifest,
            trial,
            actor_user_id=_ACTOR_ID,
            organisation_concept_id=_ORGANISATION_ID,
            namespace=_NAMESPACE,
            candidate_loader=lambda *_args, **_kwargs: _candidate(),
        )
    assert error.value.reason_code == (
        "learning_advice_experiment_control_contaminated"
    )


def test_fresh_trial_identities_are_bound_without_mutating_the_plan() -> None:
    trial = build_learning_advice_experiment_plan(_manifest())["trials"][0]
    original = copy.deepcopy(trial)
    bound = bind_fresh_trial_identities(
        trial,
        request_id="request-001",
        session_id="session-001",
        turn_id="turn-001",
    )

    assert trial == original
    assert "fresh_identity_placeholders" not in bound
    assert bound["execution_identity"] == {
        "request_id": "request-001",
        "session_id": "session-001",
        "turn_id": "turn-001",
    }

    with pytest.raises(LearningAdviceExperimentError) as error:
        bind_fresh_trial_identities(
            trial,
            request_id="reused",
            session_id="reused",
            turn_id="different",
        )
    assert error.value.reason_code == "learning_advice_experiment_identity_reuse"


def test_manifest_digest_detects_any_post_freeze_change() -> None:
    manifest = _manifest()
    manifest["cases"][0]["prompt"] = "Changed after the held-out set was frozen"

    with pytest.raises(LearningAdviceExperimentError) as error:
        validate_learning_advice_experiment_manifest(manifest)
    assert error.value.reason_code == (
        "learning_advice_experiment_manifest_digest_mismatch"
    )


def test_run_binding_is_body_free_canonical_and_rebuilds_seeded_plan() -> None:
    manifest = _manifest()
    plan = build_learning_advice_experiment_plan(manifest)
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)

    assert validate_learning_advice_experiment_run_binding(binding) == binding
    assert binding["manifest_sha256"] == manifest["manifest_sha256"]
    assert binding["plan_sha256"] == plan["plan_sha256"]
    assert binding["required_applicable_pair_count"] == 8
    assert binding["required_control_pair_count"] == 4
    assert len(binding["body_free_manifest_preimage"]["cases"]) == 6
    assert len(binding["body_free_plan_preimage"]["trials"]) == 24
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True)
    assert _CANDIDATE_BODY not in encoded
    assert all(case["prompt"] not in encoded for case in manifest["cases"])
    assert '"prompt"' not in encoded


def test_run_binding_rejects_rehashed_non_deterministic_plan() -> None:
    binding = build_learning_advice_experiment_run_binding(_manifest())
    tampered = copy.deepcopy(binding)
    tampered["body_free_plan_preimage"]["trials"][0]["arm"] = (
        "B" if tampered["body_free_plan_preimage"]["trials"][0]["arm"] == "A" else "A"
    )
    tampered["body_free_plan_preimage_sha256"] = _canonical_sha256(
        tampered["body_free_plan_preimage"]
    )
    tampered.pop("binding_sha256")
    tampered["binding_sha256"] = _canonical_sha256(tampered)

    with pytest.raises(LearningAdviceExperimentError) as error:
        validate_learning_advice_experiment_run_binding(tampered)

    assert error.value.reason_code == (
        "learning_advice_experiment_run_binding_plan_mismatch"
    )


def test_run_binding_rejects_rehashed_duplicate_and_semantic_text_mutations() -> None:
    binding = build_learning_advice_experiment_run_binding(_manifest())
    duplicated_count = copy.deepcopy(binding)
    duplicated_count["required_control_pair_count"] = 0
    duplicated_count.pop("binding_sha256")
    duplicated_count["binding_sha256"] = _canonical_sha256(duplicated_count)

    with pytest.raises(LearningAdviceExperimentError) as count_error:
        validate_learning_advice_experiment_run_binding(duplicated_count)
    assert count_error.value.reason_code == (
        "learning_advice_experiment_run_binding_duplicate_mismatch"
    )

    contaminated = copy.deepcopy(binding)
    contaminated["body_free_manifest_preimage"]["cases"][0]["content"] = (
        "private evaluator text"
    )
    contaminated["body_free_manifest_preimage_sha256"] = _canonical_sha256(
        contaminated["body_free_manifest_preimage"]
    )
    contaminated.pop("binding_sha256")
    contaminated["binding_sha256"] = _canonical_sha256(contaminated)

    with pytest.raises(LearningAdviceExperimentError) as text_error:
        validate_learning_advice_experiment_run_binding(contaminated)
    assert text_error.value.reason_code == (
        "learning_advice_experiment_run_binding_contains_text"
    )


def test_manifest_freezes_evaluator_only_contract_and_revision_digests() -> None:
    manifest = _manifest()
    contract = manifest["cases"][2]["evaluation_contract"]

    assert contract == {
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
    plan_trial = next(
        trial
        for trial in build_learning_advice_experiment_plan(manifest)["trials"]
        if trial["case_id"] == manifest["cases"][2]["case_id"]
    )
    assert plan_trial["evaluation_contract"] == contract
    assert plan_trial["evaluator_sha256"] == _EVALUATOR_SHA256
    assert plan_trial["rubric_sha256"] == _RUBRIC_SHA256


def test_manifest_rejects_invalid_or_unfrozen_evaluator_contract() -> None:
    cases = _cases()
    cases[0]["evaluation_contract"]["allowed_first_capabilities"] = []
    with pytest.raises(LearningAdviceExperimentError):
        _manifest(cases=cases)

    cases = _cases()
    cases[0].pop("evaluator_sha256")
    with pytest.raises(LearningAdviceExperimentError):
        _manifest(cases=cases)


def test_manifest_rejects_impossible_decision_threshold() -> None:
    with pytest.raises(LearningAdviceExperimentError):
        _manifest(
            decision_rule={
                "minimum_applicable_b_passes": 9,
                "minimum_applicable_pass_delta": 2,
                "require_more_paired_improvements_than_regressions": True,
                "forbid_b_only_material_failure": True,
                "require_control_non_inferiority": True,
            }
        )


def _canonical_trial_observations() -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for trial in build_learning_advice_experiment_plan(_manifest())["trials"]:
        passed = trial["arm"] == "B" or trial["case_kind"] == "control"
        verdict = "pass" if passed else "fail"
        observations.append(
            {
                "trial_id": trial["trial_id"],
                "case_id": trial["case_id"],
                "case_kind": trial["case_kind"],
                "repeat": trial["repeat"],
                "arm": trial["arm"],
                "trial_integrity_valid": True,
                "evaluation": {
                    "verdict": verdict,
                    "material_failure": False,
                    "external_availability_changed": False,
                },
            }
        )
    return observations


@pytest.mark.parametrize(
    ("capability_choice", "work_product", "material_failure", "expected"),
    [
        ("pass", "pass", False, "pass"),
        ("pass", "partial", False, "partial"),
        ("partial", "fail", False, "fail"),
        ("fail", "inconclusive", False, "inconclusive"),
        ("pass", "pass", True, "fail"),
        ("inconclusive", "pass", True, "fail"),
    ],
)
def test_evaluator_verdict_is_deterministically_derived_from_dimensions(
    capability_choice: str,
    work_product: str,
    material_failure: bool,
    expected: str,
) -> None:
    assert (
        derive_learning_advice_evaluator_verdict(
            capability_choice=capability_choice,
            work_product=work_product,
            material_failure=material_failure,
        )
        == expected
    )


def test_evaluator_verdict_derivation_rejects_untyped_inputs() -> None:
    with pytest.raises(LearningAdviceExperimentError) as error:
        derive_learning_advice_evaluator_verdict(
            capability_choice="looks-good",
            work_product="pass",
            material_failure=False,
        )

    assert error.value.reason_code == (
        "learning_advice_experiment_evaluator_verdict_invalid"
    )


def test_paired_results_and_content_decision_are_purely_recomputed() -> None:
    manifest = _manifest()
    observations = _canonical_trial_observations()

    paired = compute_learning_advice_paired_results(
        observations,
        required_applicable_pair_count=8,
        required_control_pair_count=4,
    )
    decision = evaluate_learning_advice_content_decision(
        paired,
        decision_rule=manifest["decision_rule"],
    )

    assert paired["valid_pair_count"] == 12
    assert paired["applicable"]["b_better_count"] == 8
    assert paired["controls"]["negative_transfer_count"] == 0
    assert decision["decision"] == "arm_b_content_win"
    assert decision["activation_authorised"] is False

    mutated = next(
        observation
        for observation in observations
        if observation["case_kind"] == "applicable" and observation["arm"] == "A"
    )
    mutated["evaluation"]["verdict"] = "pass"
    recomputed = compute_learning_advice_paired_results(
        observations,
        required_applicable_pair_count=8,
        required_control_pair_count=4,
    )
    assert recomputed != paired


def test_paired_recomputation_excludes_availability_and_requires_comparators() -> None:
    manifest = _manifest()
    observations = _canonical_trial_observations()
    affected_case = observations[0]["case_id"]
    for observation in observations:
        if observation["case_id"] == affected_case:
            observation["evaluation"]["external_availability_changed"] = True

    paired = compute_learning_advice_paired_results(
        observations,
        required_applicable_pair_count=8,
        required_control_pair_count=4,
    )
    decision = evaluate_learning_advice_content_decision(
        paired,
        decision_rule=manifest["decision_rule"],
    )

    section = paired[
        "applicable" if observations[0]["case_kind"] == "applicable" else "controls"
    ]
    assert section["comparable_pair_count"] < section["required_comparable_pair_count"]
    assert paired["outcome_counts"]["availability_changed"] == 2
    assert paired["comparison"] == "inconclusive"
    assert decision["decision"] == "inconclusive"


def test_paired_recomputation_rejects_duplicate_trial_evidence() -> None:
    observations = _canonical_trial_observations()
    observations[-1] = copy.deepcopy(observations[0])

    with pytest.raises(LearningAdviceExperimentError) as error:
        compute_learning_advice_paired_results(
            observations,
            required_applicable_pair_count=8,
            required_control_pair_count=4,
        )

    assert error.value.reason_code == "learning_advice_experiment_paired_result_invalid"
