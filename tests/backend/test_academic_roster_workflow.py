from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services import academic_roster_workflow_vontology_service as service
from src.backend.services import concept_service
from src.backend.services.entity_representation_workflow_vontology_service import (
    PERSON_REPRESENTATION_WORKFLOW_ID,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable import academic_roster_workflow as mod
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.registry_factory import (
    build_durable_action_registry,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "academic_rosters"
_UOA_FIXTURE = _FIXTURE_DIR / "uoa_cs_doctoral_supervisors_2026-08-24.json"
_UOA_NEGATIVE_FIXTURE = (
    _FIXTURE_DIR / "uoa_school_of_computer_science_honorary_2026-08-24.json"
)
_RMIT_FIXTURE = _FIXTURE_DIR / "rmit_cs_academic_research_staff_2026-08-24.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _request(
    action_id: str,
    *,
    inputs: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id=action_id,
        inputs=inputs or {},
        data=data or {},
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace="#V#tester@research_org",
            user_concept_id="#V#tester",
            org_concept_id="#V#research_org",
        ),
    )


def _validate(snapshot: dict[str, Any]) -> WorkflowActionResult:
    return mod._handle_validate_snapshot(
        _request(
            mod.ACADEMIC_ROSTER_VALIDATE_ACTION_ID,
            inputs={"academic_roster_snapshot": snapshot},
        )
    )


def test_uoa_exact_supervisor_cohort_preserves_count_pages_and_roles() -> None:
    snapshot = _load(_UOA_FIXTURE)
    result = _validate(snapshot)

    assert result.status == "success"
    validation = result.outputs["academic_roster_validation"]
    assert validation["record_count"] == 42
    assert validation["page_counts"] == {"1": 25, "2": 17}
    assert validation["classification_counts"] == {
        "included": 42,
        "excluded": 0,
        "unresolved": 0,
    }
    assert validation["coverage_claim"] == "bounded_source_cohort"
    assert validation["all_academic_staff_claimed"] is False

    filters = {
        item["dimension"]: item["value"] for item in snapshot["cohort"]["filters"]
    }
    assert filters == {
        "Department": "Computer Science",
        "Graduate Supervision": "PhD/Doctoral Accredited Supervisor",
    }
    role_titles = {title for row in snapshot["records"] for title in row["role_titles"]}
    assert {
        "Professor",
        "Lecturer",
        "Professional Teaching Fellow",
        "Research Fellow",
        "Senior Research Fellow",
    } <= role_titles
    leadership = {
        title for row in snapshot["records"] for title in row["leadership_roles"]
    }
    assert (
        "Associate Dean International / Faculty of Science Administration" in leadership
    )
    assert (
        "Associate Dean Learning and Teaching / Faculty of Science Administration"
        in leadership
    )
    assert "Associate Dean Māori / Manupiri / Faculty of Science" in leadership
    assert all(
        row["graduate_supervision"]["assertion_required"] is True
        for row in snapshot["records"]
    )
    michael = next(
        row for row in snapshot["records"] if row["record_key"] == "m-witbrock"
    )
    assert michael["person"]["name"] == "Michael Witbrock"
    assert michael["person"]["display_name"] == "Professor Michael Witbrock"


def test_uoa_near_matching_school_facet_is_a_distinct_excluded_cohort() -> None:
    requested = _load(_UOA_FIXTURE)
    negative = _load(_UOA_NEGATIVE_FIXTURE)

    result = _validate(negative)
    assert result.status == "success"
    assert result.outputs["academic_roster_validation"]["record_count"] == 14
    assert result.outputs["academic_roster_validation"]["classification_counts"] == {
        "included": 0,
        "excluded": 14,
        "unresolved": 0,
    }
    assert requested["cohort"]["filters"][0]["value"] == "Computer Science"
    assert negative["cohort"]["filters"][0]["value"] == ("School of Computer Science")
    assert all(
        any(title.startswith("Honorary") for title in row["role_titles"])
        for row in negative["records"]
    )


def test_broader_source_universe_is_classified_without_all_staff_claim() -> None:
    snapshot = _load(_UOA_FIXTURE)
    snapshot["source"] = {
        **snapshot["source"],
        "source_id": "uoa-computer-science-universe-260",
    }
    snapshot["cohort"] = {
        **snapshot["cohort"],
        "label": "Computer Science directory source universe",
        "membership_claim": (
            "Rows observed in the broader Computer Science directory source; "
            "classification determines inclusion in the supervisor cohort"
        ),
        "declared_total": 260,
    }
    snapshot["cohort"].pop("pagination")
    for index in range(43, 261):
        snapshot["records"].append(
            {
                "record_key": f"source-row-{index:03d}",
                "classification": "excluded",
                "classification_reason": (
                    "Observed in the broader source universe but outside the "
                    "doctoral-accredited-supervisor facet"
                ),
                "source_evidence": {
                    "source_url": snapshot["source"]["source_url"],
                    "locator": f"broader directory source row {index}",
                },
            }
        )

    result = _validate(snapshot)
    assert result.status == "success"
    validation = result.outputs["academic_roster_validation"]
    assert validation["record_count"] == 260
    assert validation["classification_counts"] == {
        "included": 42,
        "excluded": 218,
        "unresolved": 0,
    }
    assert validation["coverage_claim"] == "bounded_source_cohort"
    assert validation["all_academic_staff_claimed"] is False


def test_rmit_grouped_page_uses_same_contract_without_supervision_inference() -> None:
    snapshot = _load(_RMIT_FIXTURE)
    result = _validate(snapshot)

    assert result.status == "success"
    validation = result.outputs["academic_roster_validation"]
    assert validation["record_count"] == 23
    assert validation["page_counts"] == {"1": 23}
    assert snapshot["source"]["source_kind"] == "grouped_staff_page"
    assert all("graduate_supervision" not in row for row in snapshot["records"])
    assert any(
        row["leadership_roles"] == ["Associate Dean"] for row in snapshot["records"]
    )
    assert any(row["role_titles"] == ["Research Fellow"] for row in snapshot["records"])


def test_validator_fails_closed_on_silent_pagination_loss() -> None:
    snapshot = _load(_UOA_FIXTURE)
    snapshot["records"].pop()

    result = _validate(snapshot)

    assert result.status == "failed"
    assert result.error == "academic_roster_snapshot_invalid"
    assert any(
        "declared_total" in error
        for error in result.outputs["academic_roster_validation_errors"]
    )
    assert any(
        "pagination" in error
        for error in result.outputs["academic_roster_validation_errors"]
    )


def test_appointment_and_supervision_are_separate_idempotent_assertions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _load(_UOA_FIXTURE)
    record = next(
        row for row in snapshot["records"] if row["record_key"] == "yu-cheng-tu"
    )
    seen: set[tuple[str, str]] = set()
    calls: list[dict[str, Any]] = []

    def _fake_upsert(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        key = (kwargs["predicate"], kwargs["target_text"])
        changed = key not in seen
        seen.add(key)
        return {
            "effect_status": "succeeded",
            "changed": changed,
            "assertion_id": f"ska_{len(seen)}",
            "canonical_read_back": {"predicate": kwargs["predicate"]},
        }

    monkeypatch.setattr(mod, "upsert_scoped_assertion", _fake_upsert)
    request = _request(
        mod.ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID,
        inputs={
            "current_academic_roster_record": record,
            "person_concept_id": "#V#person_yu_cheng_tu",
            "academic_roster_source": snapshot["source"],
            "academic_roster_cohort": snapshot["cohort"],
        },
    )

    first = mod._handle_assert_appointment(request)
    second = mod._handle_assert_appointment(request)

    assert first.status == "success"
    assert second.status == "success"
    assert [call["predicate"] for call in calls] == [
        mod.ACADEMIC_APPOINTMENT_PREDICATE_ID,
        mod.GRADUATE_SUPERVISION_PREDICATE_ID,
        mod.ACADEMIC_APPOINTMENT_PREDICATE_ID,
        mod.GRADUATE_SUPERVISION_PREDICATE_ID,
    ]
    appointment = json.loads(calls[0]["target_text"])
    supervision = json.loads(calls[1]["target_text"])
    assert appointment["role_titles"] == ["Professional Teaching Fellow"]
    assert "status" not in appointment
    assert supervision["status"] == "accredited"
    assert [
        receipt["changed"]
        for receipt in first.outputs["return_payload"]["effect_receipts"]
    ] == [True, True]
    assert [
        receipt["changed"]
        for receipt in second.outputs["return_payload"]["effect_receipts"]
    ] == [False, False]


def test_one_item_partial_failure_is_retryable_without_duplicate_appointment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _load(_UOA_FIXTURE)
    record = snapshot["records"][0]
    stored: set[tuple[str, str]] = set()
    supervision_attempts = 0

    def _flaky_upsert(**kwargs: Any) -> dict[str, Any]:
        nonlocal supervision_attempts
        key = (kwargs["predicate"], kwargs["target_text"])
        if kwargs["predicate"] == mod.GRADUATE_SUPERVISION_PREDICATE_ID:
            supervision_attempts += 1
            if supervision_attempts == 1:
                raise RuntimeError("temporary supervision write failure")
        changed = key not in stored
        stored.add(key)
        return {
            "effect_status": "succeeded",
            "changed": changed,
            "assertion_id": f"ska_{len(stored)}",
            "canonical_read_back": {"predicate": kwargs["predicate"]},
        }

    monkeypatch.setattr(mod, "upsert_scoped_assertion", _flaky_upsert)
    request = _request(
        mod.ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID,
        inputs={
            "current_academic_roster_record": record,
            "person_concept_id": "#V#person_robert_amor",
            "academic_roster_source": snapshot["source"],
            "academic_roster_cohort": snapshot["cohort"],
        },
    )

    first = mod._handle_assert_appointment(request)
    second = mod._handle_assert_appointment(request)

    assert first.status == "failed"
    assert first.outputs["academic_roster_item_result"]["terminal_status"] == ("failed")
    assert len(first.outputs["academic_roster_item_result"]["effect_receipts"]) == 1
    assert second.status == "success"
    assert [
        receipt["changed"]
        for receipt in second.outputs["return_payload"]["effect_receipts"]
    ] == [False, True]
    assert len(stored) == 2


def test_non_included_row_returns_terminal_result_without_mutation() -> None:
    record = _load(_UOA_NEGATIVE_FIXTURE)["records"][0]
    result = mod._handle_finalise_non_included(
        _request(
            mod.ACADEMIC_APPOINTMENT_FINALISE_NON_INCLUDED_ACTION_ID,
            inputs={"current_academic_roster_record": record},
        )
    )

    assert result.status == "success"
    assert result.outputs["control_signal"] == "return"
    assert result.outputs["return_payload"]["terminal_status"] == "excluded"
    assert result.outputs["return_payload"]["effect_receipts"] == []


def test_reconciliation_requires_one_non_failed_terminal_result_per_row() -> None:
    validation = {
        "record_count": 2,
        "classification_counts": {"included": 1, "excluded": 1, "unresolved": 0},
        "record_keys": ["included", "excluded"],
        "classification_by_record_key": {
            "included": "included",
            "excluded": "excluded",
        },
    }
    results = [
        {
            "completed": True,
            "result": {
                "schema_version": "academic_roster_item_result.v1",
                "record_key": "included",
                "classification": "included",
                "terminal_status": "represented",
            },
        },
        {
            "completed": True,
            "result": {
                "schema_version": "academic_roster_item_result.v1",
                "record_key": "excluded",
                "classification": "excluded",
                "terminal_status": "excluded",
            },
        },
    ]
    success = mod._handle_reconcile_results(
        _request(
            mod.ACADEMIC_ROSTER_RECONCILE_ACTION_ID,
            inputs={
                "academic_roster_validation": validation,
                "academic_roster_iteration_results": results,
            },
        )
    )
    assert success.status == "success"
    assert success.outputs["academic_roster_reconciliation"]["all_rows_terminal"]

    failed_results = copy.deepcopy(results)
    failed_results[1] = {"completed": False, "error": "child failed", "result": {}}
    failure = mod._handle_reconcile_results(
        _request(
            mod.ACADEMIC_ROSTER_RECONCILE_ACTION_ID,
            inputs={
                "academic_roster_validation": validation,
                "academic_roster_iteration_results": failed_results,
            },
        )
    )
    assert failure.status == "failed"
    assert failure.error == "academic_roster_aggregate_reconciliation_failed"

    duplicated_results = copy.deepcopy(results)
    duplicated_results[1]["result"]["record_key"] = "included"
    duplicate = mod._handle_reconcile_results(
        _request(
            mod.ACADEMIC_ROSTER_RECONCILE_ACTION_ID,
            inputs={
                "academic_roster_validation": validation,
                "academic_roster_iteration_results": duplicated_results,
            },
        )
    )
    assert duplicate.status == "failed"
    reconciliation = duplicate.outputs["academic_roster_reconciliation"]
    assert reconciliation["missing_record_keys"] == ["excluded"]
    assert reconciliation["unexpected_record_keys"] == ["included"]
    assert reconciliation["duplicate_record_keys"] == ["included"]


def test_repo_seed_defines_source_neutral_parent_and_reusable_child() -> None:
    definitions = build_repo_seed_workflow_definitions(
        bundle_paths=[service._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[
            service.ACADEMIC_ROSTER_WORKFLOW_ID,
            service.ACADEMIC_APPOINTMENT_WORKFLOW_ID,
        ],
    )
    assert set(definitions) == {
        service.ACADEMIC_ROSTER_WORKFLOW_ID,
        service.ACADEMIC_APPOINTMENT_WORKFLOW_ID,
    }
    parent = definitions[service.ACADEMIC_ROSTER_WORKFLOW_ID]
    child = definitions[service.ACADEMIC_APPOINTMENT_WORKFLOW_ID]
    assert [
        state.actions[0].action_id if state.actions else None
        for state in parent.states.values()
    ] == [
        "workflow_control.context_set",
        "llm.action",
        mod.ACADEMIC_ROSTER_VALIDATE_ACTION_ID,
        "workflow_control.for_each",
        mod.ACADEMIC_ROSTER_RECONCILE_ACTION_ID,
        None,
        None,
    ]
    assert parent.states["acquire_and_extract_snapshot"].actions[0].llm_policy[
        "required_tools"
    ] == ["resilient_extract_url"]
    assert child.states["materialise_person_core"].actions[0].action_id == (
        "entity_representation.materialise_from_payload"
    )
    assert child.states["assert_appointment_record"].actions[0].action_id == (
        mod.ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID
    )
    route_inputs = child.states["route_record_classification"].actions[0].inputs
    name_assignment = route_inputs["assignments"][0]
    assert name_assignment["value_from_context_options"] == [
        "current_academic_roster_record.person.name",
        "current_academic_roster_record.person.display_name",
    ]
    module_source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "University of Auckland" not in module_source
    assert "RMIT" not in module_source
    assert "Professional Teaching Fellow" not in module_source


def test_shared_registry_keeps_single_person_and_roster_actions_available() -> None:
    registry = build_durable_action_registry()

    assert registry.has("entity_representation.materialise_from_payload")
    assert registry.has(mod.ACADEMIC_ROSTER_VALIDATE_ACTION_ID)
    assert registry.has(mod.ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID)
    assert registry.has(mod.ACADEMIC_APPOINTMENT_FINALISE_NON_INCLUDED_ACTION_ID)
    assert registry.has(mod.ACADEMIC_ROSTER_RECONCILE_ACTION_ID)


def test_bootstrap_materialises_both_workflows_prompt_and_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_academic_roster_workflow")
    db = get_db()
    assert db is not None
    for collection_name in ("concepts", "text_relations", "text_values"):
        db.drop_collection(collection_name)
    authority_service.clear_workflow_type_resolution_cache()
    invalidate_workflow_discovery_executability_caches()

    report = service.bootstrap_canonical_academic_roster_workflows()

    assert report["success"] is True
    assert set(report["workflow_ids"]) == {
        service.ACADEMIC_ROSTER_WORKFLOW_ID,
        service.ACADEMIC_APPOINTMENT_WORKFLOW_ID,
    }
    assert (
        load_workflow_definition_from_vontology(service.ACADEMIC_ROSTER_WORKFLOW_ID)
        is not None
    )
    assert (
        load_workflow_definition_from_vontology(
            service.ACADEMIC_APPOINTMENT_WORKFLOW_ID
        )
        is not None
    )
    for concept_id in (
        service.ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID,
        mod.ACADEMIC_APPOINTMENT_PREDICATE_ID,
        mod.GRADUATE_SUPERVISION_PREDICATE_ID,
    ):
        assert concept_service.get_concept_by_concept_id_exact(concept_id) is not None

    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def test_single_row_parent_executes_through_generic_child_and_keeps_person_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _load(_RMIT_FIXTURE)
    snapshot["cohort"] = {
        **snapshot["cohort"],
        "declared_total": 1,
        "pagination": {"pages": [{"page_index": 1, "observed_count": 1}]},
    }
    snapshot["records"] = [snapshot["records"][0]]
    definitions = build_repo_seed_workflow_definitions(
        bundle_paths=[service._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[
            service.ACADEMIC_ROSTER_WORKFLOW_ID,
            service.ACADEMIC_APPOINTMENT_WORKFLOW_ID,
        ],
    )
    registry = ActionRegistry()
    register_control_flow_actions(
        registry, definition_loader=lambda workflow_id: definitions.get(workflow_id)
    )
    mod.register_academic_roster_actions(registry)
    materialised_names: list[str] = []

    def _materialise_person(request: WorkflowActionRequest) -> WorkflowActionResult:
        materialised_names.append(str(request.inputs.get("entity_name") or ""))
        return WorkflowActionResult(
            status="success",
            outputs={
                "entity_representation_concept_id": "#V#person_john_thangarajah",
                "entity_core_representation_verified": True,
            },
        )

    registry.register(
        ActionSpec(
            action_id="entity_representation.materialise_from_payload",
            handler=_materialise_person,
            side_effects="write",
        )
    )
    monkeypatch.setattr(
        mod,
        "upsert_scoped_assertion",
        lambda **kwargs: {
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_single",
            "canonical_read_back": {"predicate": kwargs["predicate"]},
        },
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definitions[service.ACADEMIC_ROSTER_WORKFLOW_ID],
        # Unscoped execution deliberately exercises the repo-seed fallback
        # loader. Production actor-scoped runs resolve the published child from
        # Vontology; the trusted actor values remain workflow data for this test.
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "academic_roster_snapshot": snapshot,
            "user_concept_id": "#V#tester",
            "org_concept_id": "#V#research_org",
        },
    )

    assert result.completed is True
    reconciliation = result.data["academic_roster_reconciliation"]
    assert reconciliation["observed_count"] == 1
    assert reconciliation["terminal_status_counts"] == {"represented": 1}
    assert reconciliation["item_results"][0]["person_concept_id"] == (
        "#V#person_john_thangarajah"
    )
    assert materialised_names == ["John Thangarajah"]
    assert PERSON_REPRESENTATION_WORKFLOW_ID == "#V#person_representation_workflow"


def test_prompt_requires_general_source_adaptation_and_honest_partial_coverage() -> (
    None
):
    prompt = service._load_academic_roster_acquisition_prompt_seed_text()
    assert "paged or\nfaceted result" in prompt
    assert "grouped page" in prompt
    assert "official API" in prompt
    assert "Do not silently stop at the first page" in prompt
    assert "not the same thing as all staff" in prompt
    assert "Preserve unfamiliar roles verbatim" in prompt
