from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import benchmark_suite_vontology_service as service
from src.backend.services.concept_service import ConceptNotFoundError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_DIR = _REPO_ROOT / "src" / "backend" / "workflows" / "repo_seed_bundles"


def _selector_fixture_path() -> Path:
    return _SEED_DIR / "selector_routing_benchmark_seed_bundle.json"


def _operational_certification_fixture_path() -> Path:
    return _SEED_DIR / "operational_certification_benchmark_seed_bundle.json"


def _seed_v7_operational_definition() -> dict[str, Any]:
    """Reconstruct the exact reviewed seed-v7 authority from the current fixture."""

    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _operational_certification_fixture_path(),
        suite_concept_id=service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
    )
    definition["seed_version"] = 7
    durable_budget_ids = {
        "pilot_durable_checkpoint_interruption_and_resume": (
            "pilot_durable_checkpoint_interruption_follow_up_request_count"
        ),
        "pilot_durable_workflow_resume_and_idempotence": (
            "pilot_durable_workflow_resume_and_idempotence_follow_up_request_count"
        ),
    }
    for scenario in definition["case_sets"]["trusted_sail_pilot_v1"]:
        budget_id = durable_budget_ids.get(scenario["scenario_id"])
        if budget_id is None:
            continue
        scenario["budgets"].append(
            {
                "budget_id": budget_id,
                "measurement_path": "/operational_metrics/follow_up_request_count",
                "operator": "lte",
                "limit": 1,
                "blocking": True,
            }
        )
        burden_evidence = scenario["metadata"]["interaction_burden_evidence"]
        burden_evidence.pop("follow_up_request_applicable")
        burden_evidence["follow_up_request_measurement"] = "completion_gate_proxy"
        burden_evidence["follow_up_request_max_per_trial"] = 1
    return definition


def _seed_v3_operational_definition() -> dict[str, Any]:
    """Reconstruct the exact reviewed seed-v3 authority from the current fixture."""

    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _operational_certification_fixture_path(),
        suite_concept_id=service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
    )
    definition["seed_version"] = 3
    definition["suite_description"] = (
        "Migration fixture for a Vontology-authoritative five-trial operational "
        "certification campaign. The included executable engineering seed proves "
        "the runner and evaluator path but is deliberately ineligible for the "
        "first-SAIL-user verdict until the pilot cohort, recurring task corpus and "
        "operating envelopes are agreed in live authority."
    )
    definition["default_case_set"] = "executable_engineering_seed"
    definition["case_sets"].pop("trusted_sail_pilot_v1", None)
    definition["case_sets"].pop("transient_mcp_fault_recovery_v1", None)
    definition["case_sets"].pop("certification_negative_controls_v1", None)
    policy = definition["rubric"]["operational_certification_policy"]
    policy.pop("pilot_cohort", None)
    policy["pilot_envelopes"] = {
        "status": "pending_user_agreement",
        "latency": None,
        "clarification": None,
        "correction": None,
    }
    policy["certification_gates"] = [
        gate
        for gate in policy["certification_gates"]
        if gate["gate_id"]
        not in {
            "arxiv_mcp_family_pass_three_minimum",
            "jira_mcp_family_pass_three_minimum",
            "multi_tool_briefing_family_pass_three_minimum",
            "degraded_partial_success_family_pass_three_minimum",
            "durable_checkpoint_interruption_family_pass_three_minimum",
        }
    ]
    return definition


def _origin_main_unversioned_operational_definition() -> dict[str, Any]:
    """Reconstruct the exact unversioned authority shipped on origin/main.

    Keeping this reconstruction beside the migration test avoids making test
    execution depend on a Git remote while still pinning the reviewed legacy
    authority digest carried by the seed metadata.
    """

    definition = _seed_v3_operational_definition()
    definition.pop("seed_version", None)
    policy = definition["rubric"]["operational_certification_policy"]
    policy["certification_gates"] = [
        gate
        for gate in policy["certification_gates"]
        if gate["gate_id"]
        not in {
            "durable_resume_idempotence_family_pass_three_minimum",
            "same_session_context_family_pass_three_minimum",
        }
    ]
    scenarios = definition["case_sets"]["executable_engineering_seed"]
    scenarios = [
        scenario
        for scenario in scenarios
        if scenario["scenario_id"]
        not in {
            "represented_workflow_concept_same_session_followup",
            "durable_concept_profile_resume_and_idempotence",
        }
    ]
    marker = next(
        scenario
        for scenario in scenarios
        if scenario["scenario_id"] == "unique_state_marker_create_and_read_back"
    )
    marker["scenario_id"] = "unique_state_message_create_and_read_back"
    marker["acceptable_goal_states"][0].update(
        {
            "goal_state_id": "unique_message_created_and_read_back",
            "description": (
                "Exactly one namespaced Von message with the unique marker is "
                "created and its stored state is read back."
            ),
        }
    )
    marker["execution"]["inputs"]["prompt"] = (
        "Create one namespaced Von message titled 'Operational certification "
        "{{isolation_id}}' with body 'Ephemeral engineering certification marker "
        "{{isolation_id}}'. Read the stored message back and report its stable "
        "identifier and exact title. Do not use Jira, Gmail, web or deletion tools."
    )
    marker["metadata"]["authority_note"] = (
        "Unique-state engineering mutation/read-back seed; not a substitute for "
        "an agreed pilot task."
    )
    marker["milestone_dag"][1]["milestone_id"] = "message_created"
    marker["milestone_dag"][2]["depends_on"] = ["message_created"]
    marker["milestone_dag"][2]["milestone_id"] = "message_read_back"
    marker["permitted_effects"][0]["effect_type"] = (
        "create_namespaced_von_message"
    )
    marker["reset_policy"].pop("authoritative_absence_probe", None)
    definition["case_sets"]["executable_engineering_seed"] = scenarios
    return definition


def _install_operational_definition_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    definition: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Install a relation-row store that preserves migration receipt context."""

    state: dict[str, Any] = {
        "text": json.dumps(definition),
        "context": dict(context or {}),
    }
    upserts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )

    def _get_texts_for_concept(
        concept_id: str,
        *,
        predicate: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if (
            concept_id == service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
            and predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
        ):
            return [
                {
                    "text": state["text"],
                    "context": dict(state["context"]),
                }
            ]
        return []

    def _upsert_singleton_text_relation(**kwargs: Any) -> dict[str, Any]:
        upserts.append(dict(kwargs))
        state["text"] = str(kwargs["text"])
        state["context"] = dict(kwargs.get("context") or {})
        return {"success": True}

    monkeypatch.setattr(service, "get_texts_for_concept", _get_texts_for_concept)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _upsert_singleton_text_relation,
    )
    return state, upserts


def test_load_benchmark_suite_case_set_uses_represented_vontology_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _selector_fixture_path(),
        suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    )
    stored_definition = dict(definition)
    stored_definition.pop("source_path", None)
    stored_definition.pop("fixture_sha256", None)

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, *, predicate, limit=10: (
            [{"text": json.dumps(stored_definition)}]
            if predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
            else []
        ),
    )

    result = service.load_benchmark_suite_case_set(
        suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    )

    assert result["source"] == "vontology"
    assert (
        result["suite_concept_id"]
        == service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    )
    assert result["case_set"] == "phase1_seed"
    assert result["source_predicate"] == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
    assert result["rubric"]["rubric_id"] == "selector_routing_rubric.v1"
    assert len(result["cases"]) >= 5


def test_load_benchmark_suite_case_set_fails_closed_when_suite_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_concept_id: str) -> dict[str, Any]:
        raise ConceptNotFoundError("missing")

    monkeypatch.setattr(service, "get_concept_by_concept_id", _missing)

    with pytest.raises(service.BenchmarkSuiteAuthorityMissingError) as exc_info:
        service.load_benchmark_suite_case_set(
            suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
        )

    assert str(exc_info.value) == "benchmark_suite_concept_missing"
    assert exc_info.value.diagnostics["missing_suite_concept_ids"] == [
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    ]


def test_live_suite_authority_rejects_duplicate_canonical_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _selector_fixture_path(),
        suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    )
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [
            {"text": json.dumps(definition)},
            {"text": json.dumps(definition)},
        ],
    )

    with pytest.raises(service.BenchmarkSuiteAuthorityMissingError) as exc_info:
        service.load_benchmark_suite_definition(
            service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
        )

    assert str(exc_info.value) == "benchmark_suite_definition_ambiguous"


def test_malformed_canonical_definition_cannot_fall_through_to_legacy_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda _concept_id, *, predicate, limit=10: (
            [{"text": "{malformed"}]
            if predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
            else [{"text": "{}"}]
        ),
    )

    with pytest.raises(service.BenchmarkSuiteAuthorityMissingError) as exc_info:
        service.load_benchmark_suite_definition(
            service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
        )

    assert str(exc_info.value) == "benchmark_suite_definition_malformed"


def test_ensure_canonical_benchmark_suites_imports_without_overwriting_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: set[str] = set()
    persisted: list[dict[str, Any]] = []

    def _get_concept(concept_id: str) -> dict[str, Any]:
        if concept_id in created:
            return {"concept_id": concept_id}
        raise ConceptNotFoundError("missing")

    def _create_concept(**kwargs: Any) -> dict[str, Any]:
        created.add(str(kwargs["concept_id"]))
        return {"concept_id": kwargs["concept_id"]}

    def _get_texts_for_concept(
        _concept_id: str,
        *,
        predicate: str,
        limit: int = 1,
    ) -> list[dict[str, str]]:
        return []

    def _upsert_singleton_text_relation(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        return {"success": True}

    monkeypatch.setattr(service, "get_concept_by_concept_id", _get_concept)
    monkeypatch.setattr(service.concept_service, "create_concept", _create_concept)
    monkeypatch.setattr(service, "get_texts_for_concept", _get_texts_for_concept)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _upsert_singleton_text_relation,
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID],
    )

    assert report["success"] is True
    assert service.BENCHMARK_SUITE_TYPE_ID in created
    assert service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID in created
    assert report["persisted_suite_concept_ids"] == [
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    ]
    assert persisted[0]["predicate"] == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
    stored_payload = json.loads(persisted[0]["text"])
    assert stored_payload["suite_concept_id"] == (
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    )


@pytest.mark.parametrize("legacy_seed_version", [1, 2])
def test_operational_suite_preserves_unregistered_numeric_older_authority(
    monkeypatch: pytest.MonkeyPatch,
    legacy_seed_version: int,
) -> None:
    fixture_definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _operational_certification_fixture_path(),
        suite_concept_id=(service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID),
    )
    assert fixture_definition["seed_version"] == 8
    legacy_definition = dict(fixture_definition)
    legacy_definition["seed_version"] = legacy_seed_version
    legacy_scenario_ids = {
        "authenticated_identity_read_only",
        "unique_state_marker_create_and_read_back",
        "typed_missing_entity_recovery",
    }
    legacy_definition["case_sets"] = {
        "executable_engineering_seed": [
            scenario
            for scenario in fixture_definition["case_sets"][
                "executable_engineering_seed"
            ]
            if scenario["scenario_id"] in legacy_scenario_ids
        ]
    }
    legacy_definition["default_case_set"] = "executable_engineering_seed"
    state, persisted = _install_operational_definition_store(
        monkeypatch,
        definition=legacy_definition,
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[
            service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
        ],
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert report["success"] is False
    assert report["persisted_suite_concept_ids"] == []
    assert report["migrated_older_suite_concept_ids"] == []
    blocker = report["migration_blockers_by_concept_id"][concept_id]
    assert blocker["error_code"] == "legacy_authority_requires_explicit_migration"
    assert blocker["observed_seed_version_key"] == str(legacy_seed_version)
    assert blocker["known_legacy_authority_payload_sha256"] == []
    assert json.loads(str(state["text"])) == legacy_definition
    assert persisted == []


@pytest.mark.parametrize(
    ("legacy_definition_factory", "legacy_seed_version"),
    [
        pytest.param(_seed_v3_operational_definition, "3", id="seed-v3"),
        pytest.param(_seed_v7_operational_definition, "7", id="seed-v7"),
    ],
)
def test_operational_suite_migrates_an_exact_registered_numeric_legacy_payload(
    monkeypatch: pytest.MonkeyPatch,
    legacy_definition_factory: Any,
    legacy_seed_version: str,
) -> None:
    legacy_definition = legacy_definition_factory()
    legacy_sha256 = service._hash_payload(
        service._definition_authority_payload(legacy_definition)
    )
    assert legacy_sha256 in (
        service._known_legacy_authority_payload_digests_by_seed_version(
            _operational_certification_fixture_path()
        )[legacy_seed_version]
    )
    state, upserts = _install_operational_definition_store(
        monkeypatch,
        definition=legacy_definition,
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert report["success"] is True
    assert report["migrated_older_suite_concept_ids"] == [concept_id]
    assert report["migrated_known_legacy_suite_concept_ids"] == [concept_id]
    assert report["migration_readback_by_concept_id"][concept_id]["verified"] is True
    assert json.loads(str(state["text"]))["seed_version"] == 8
    assert len(upserts) == 3
    assert state["context"][service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY][
        "status"
    ] == "verified"


def test_operational_suite_migrates_exact_origin_main_unversioned_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_definition = _origin_main_unversioned_operational_definition()
    legacy_sha256 = service._hash_payload(
        service._definition_authority_payload(legacy_definition)
    )
    assert legacy_sha256 in (
        service._known_legacy_authority_payload_digests_by_seed_version(
            _operational_certification_fixture_path()
        )["unversioned"]
    )
    state, upserts = _install_operational_definition_store(
        monkeypatch,
        definition=legacy_definition,
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert report["success"] is True
    assert report["migrated_older_suite_concept_ids"] == [concept_id]
    assert report["migrated_known_legacy_suite_concept_ids"] == [concept_id]
    assert report["migration_readback_by_concept_id"][concept_id]["verified"] is True
    assert json.loads(str(state["text"]))["seed_version"] == 8
    assert len(upserts) == 3
    assert upserts[0]["context"][service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY][
        "status"
    ] == "pending"
    assert state["context"][service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY][
        "status"
    ] == "verified"


def test_operational_suite_preserves_altered_unversioned_live_authority_with_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _operational_certification_fixture_path(),
        suite_concept_id=(service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID),
    )
    definition.pop("seed_version", None)
    definition["suite_description"] = "Human-authored unversioned live authority."
    stored_text = json.dumps(definition)
    upserts: list[dict[str, Any]] = []
    concept_creations: list[dict[str, Any]] = []

    def _get_concept(concept_id: str) -> dict[str, Any]:
        if concept_id == service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID:
            return {"concept_id": concept_id}
        raise ConceptNotFoundError("missing")

    monkeypatch.setattr(service, "get_concept_by_concept_id", _get_concept)
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: concept_creations.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, *, predicate, limit=10: (
            [{"text": stored_text}]
            if concept_id
            == service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
            and predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
            else []
        ),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upserts.append(dict(kwargs)),
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert report["success"] is False
    assert report["persisted_suite_concept_ids"] == []
    assert report["migrated_older_suite_concept_ids"] == []
    assert report["preserved_equal_or_newer_suite_concept_ids"] == [concept_id]
    blocker = report["unversioned_migration_blockers_by_concept_id"][concept_id]
    assert blocker["error_code"] == (
        "unversioned_authority_requires_explicit_migration"
    )
    assert report["errors_by_concept_id"][concept_id] == blocker["error_code"]
    assert upserts == []
    assert concept_creations == []


def test_interrupted_operational_suite_migration_self_heals_from_pending_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, upserts = _install_operational_definition_store(
        monkeypatch,
        definition=_origin_main_unversioned_operational_definition(),
    )
    real_upsert = service.upsert_singleton_text_relation
    failure = {"armed": True}

    def _fail_receipt_verification_once(**kwargs: Any) -> dict[str, Any]:
        if len(upserts) == 2 and failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated_receipt_verification_interruption")
        return real_upsert(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _fail_receipt_verification_once,
    )
    first_report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert first_report["success"] is False
    assert "simulated_receipt_verification_interruption" in first_report[
        "errors_by_concept_id"
    ][concept_id]
    assert json.loads(str(state["text"]))["seed_version"] == 8
    assert state["context"][service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY][
        "status"
    ] == "pending"

    monkeypatch.setattr(service, "upsert_singleton_text_relation", real_upsert)
    retry_report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )

    assert retry_report["success"] is True
    assert retry_report["migrated_older_suite_concept_ids"] == [concept_id]
    assert retry_report["migration_readback_by_concept_id"][concept_id][
        "verified"
    ] is True
    assert state["context"][service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY][
        "status"
    ] == "verified"


def test_pending_operational_suite_receipt_cannot_authorise_a_human_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, upserts = _install_operational_definition_store(
        monkeypatch,
        definition=_origin_main_unversioned_operational_definition(),
    )
    real_upsert = service.upsert_singleton_text_relation
    failure = {"armed": True}

    def _fail_receipt_verification_once(**kwargs: Any) -> dict[str, Any]:
        if len(upserts) == 2 and failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated_receipt_verification_interruption")
        return real_upsert(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _fail_receipt_verification_once,
    )
    first_report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )
    assert first_report["success"] is False

    human_definition = json.loads(str(state["text"]))
    human_definition["suite_description"] = (
        "Human edit made after an interrupted repo-seed migration."
    )
    state["text"] = json.dumps(human_definition)
    writes_before_retry = len(upserts)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", real_upsert)
    retry_report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID]
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert retry_report["success"] is False
    blocker = retry_report["migration_blockers_by_concept_id"][concept_id]
    assert blocker["pending_migration_receipt_present"] is True
    assert blocker["pending_migration_receipt_valid"] is False
    assert json.loads(str(state["text"]))["suite_description"] == (
        human_definition["suite_description"]
    )
    assert len(upserts) == writes_before_retry


@pytest.mark.parametrize("live_seed_version", [8, 9])
def test_operational_suite_preserves_equal_or_newer_live_authority(
    monkeypatch: pytest.MonkeyPatch,
    live_seed_version: int,
) -> None:
    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _operational_certification_fixture_path(),
        suite_concept_id=(service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID),
    )
    definition["seed_version"] = live_seed_version
    upserts: list[dict[str, Any]] = []

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, *, predicate, limit=10: (
            [{"text": json.dumps(definition)}]
            if concept_id
            == service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
            and predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
            else []
        ),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upserts.append(dict(kwargs)),
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[
            service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
        ],
    )

    concept_id = service.OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID
    assert report["success"] is True
    assert report["persisted_suite_concept_ids"] == []
    assert report["skipped_existing_suite_concept_ids"] == [concept_id]
    assert report["preserved_equal_or_newer_suite_concept_ids"] == [concept_id]
    assert upserts == []


def test_non_versioned_suite_keeps_missing_only_publication_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _selector_fixture_path(),
        suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    )
    upserts: list[dict[str, Any]] = []

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, *, predicate, limit=10: (
            [{"text": json.dumps(definition)}]
            if concept_id == service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
            and predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
            else []
        ),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upserts.append(dict(kwargs)),
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID],
    )

    assert report["success"] is True
    assert report["skipped_existing_suite_concept_ids"] == [
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    ]
    assert report["migrated_older_suite_concept_ids"] == []
    assert upserts == []
