from __future__ import annotations

import copy
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


def _set_dotted_value(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    cursor = target
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = copy.deepcopy(value)


class _ConceptStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []

    def create_concept(
        self,
        *,
        name: str,
        concept_id: str,
        description: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "name": name,
                "concept_id": concept_id,
                "description": description,
                **copy.deepcopy(_kwargs),
            }
        )
        relationships: dict[str, Any] = {}
        user_id = _kwargs.get("created_by_concept_id")
        org_id = _kwargs.get("organisation_concept_id")
        if user_id:
            relationships["#V#specific_to_user"] = [user_id]
        if org_id and _kwargs.get("visibility_scope_mode") != "user_only_default":
            relationships["#V#specific_to_organisation"] = [org_id]
        doc = {
            "concept_id": concept_id,
            "name": name,
            "description": description,
            "concept_data": {},
            "attributes": {},
            "relationships": relationships,
        }
        self.docs[concept_id] = doc
        return copy.deepcopy(doc)

    def update_concept(
        self, concept_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        doc = self.docs.setdefault(
            concept_id,
            {
                "concept_id": concept_id,
                "concept_data": {},
                "attributes": {},
            },
        )
        for dotted_key, value in updates.items():
            _set_dotted_value(doc, dotted_key, value)
        return copy.deepcopy(doc)

    def get_concept(self, concept_id: str) -> dict[str, Any]:
        from src.backend.services.concept_service import ConceptNotFoundError

        doc = self.docs.get(concept_id)
        if doc is None:
            raise ConceptNotFoundError(concept_id)
        return copy.deepcopy(doc)


class _RunCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.index_names: list[str] = []

    def list_indexes(self) -> list[dict[str, Any]]:
        return [{"name": name} for name in self.index_names]

    def create_index(
        self, _keys: list[tuple[str, int]], *, name: str, **_kwargs: Any
    ) -> None:
        if name not in self.index_names:
            self.index_names.append(name)

    def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> SimpleNamespace:
        run_id = str(query.get("run_id") or "")
        existing = self.docs.get(run_id)
        doc = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        for key, value in (update.get("$set") or {}).items():
            _set_dotted_value(doc, key, value)
        if existing is None and upsert:
            for key, value in (update.get("$setOnInsert") or {}).items():
                _set_dotted_value(doc, key, value)
        self.docs[run_id] = doc
        return SimpleNamespace(
            modified_count=1 if existing is not None else 0,
            matched_count=1 if existing is not None else 0,
            upserted_id=None if existing is not None else run_id,
        )

    def find_one(
        self, query: dict[str, Any], _projection: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        run_id = str(query.get("run_id") or "")
        doc = self.docs.get(run_id)
        return copy.deepcopy(doc) if isinstance(doc, dict) else None


class _DB:
    def __init__(
        self, collection: _RunCollection, *, client: Any = "mongo-client"
    ) -> None:
        self._collection = collection
        self.client = client

    def __getitem__(self, key: str) -> _RunCollection:
        if key != "experiment_runs":
            raise KeyError(key)
        return self._collection


class _SelectionUpdate:
    def __init__(self, *, experience_id: str, outcome: str) -> None:
        self.experience_id = experience_id
        self.outcome = outcome
        self.reward = 0.75

    def to_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "outcome": self.outcome,
            "reward": self.reward,
        }


def _patch_runtime(monkeypatch):
    from src.backend.services import experiment_run_service as mod

    store = _ConceptStore()
    run_collection = _RunCollection()
    relations: list[dict[str, Any]] = []
    text_writes: list[dict[str, Any]] = []
    singleton_writes: list[dict[str, Any]] = []

    def _add_relationship(
        *, source_id: str, predicate: str, target: str, **_kwargs: Any
    ):
        payload = {"source_id": source_id, "predicate": predicate, "target": target}
        relations.append(payload)
        return payload

    def _upsert_text_for_concept(**kwargs: Any) -> dict[str, Any]:
        text_writes.append(dict(kwargs))
        return {"relation_id": f"rel-{len(text_writes)}", "relation_created": True}

    def _upsert_singleton_text_relation(**kwargs: Any) -> dict[str, Any]:
        singleton_writes.append(dict(kwargs))
        return {
            "relation_id": f"singleton-{len(singleton_writes)}",
            "relation_created": True,
        }

    monkeypatch.setattr(mod, "_RUN_INDEXES_READY", False)
    monkeypatch.setattr(mod.concept_service, "create_concept", store.create_concept)
    monkeypatch.setattr(mod.concept_service, "update_concept", store.update_concept)
    monkeypatch.setattr(mod, "get_concept_by_concept_id_exact", store.get_concept)
    monkeypatch.setattr(mod, "get_db", lambda: _DB(run_collection))
    monkeypatch.setattr(mod, "add_relationship", _add_relationship)
    monkeypatch.setattr(mod, "upsert_text_for_concept", _upsert_text_for_concept)
    monkeypatch.setattr(
        mod,
        "upsert_singleton_text_relation",
        _upsert_singleton_text_relation,
    )
    monkeypatch.setattr(
        mod,
        "finalise_selection_experience",
        lambda *, experience_id, outcome, outcome_metadata: _SelectionUpdate(
            experience_id=experience_id,
            outcome=outcome,
        ),
    )
    monkeypatch.setattr(
        mod,
        "compute_testing_theory_diff",
        lambda *, theory_id: {
            "success": True,
            "diff": {"promotion_ready_assertion_ids": ["assert-1"]},
        },
    )
    return mod, store, run_collection, relations, text_writes, singleton_writes


def test_actor_only_experiment_concepts_are_not_visible_to_an_org_peer(
    monkeypatch,
):
    (
        mod,
        store,
        _run_collection,
        _relations,
        _text_writes,
        _singleton_writes,
    ) = _patch_runtime(monkeypatch)
    from src.backend.security.access_control import _document_visible_to_actor

    owner = "#V#user"
    organisation = "#V#org"
    spec_id = "#V#actor_only_spec"
    run_id = "#V#actor_only_run"
    created = mod.create_experiment_spec(
        name="Actor-only experiment",
        experiment_spec_id=spec_id,
        namespace="#V#user@org",
        user_id=owner,
        org_id=organisation,
        require_actor_only_visibility=True,
    )
    started = mod.start_experiment_run(
        experiment_spec_id=spec_id,
        run_id=run_id,
        namespace="#V#user@org",
        user_id=owner,
        org_id=organisation,
        require_actor_only_visibility=True,
    )

    assert created["success"] is True
    assert started["success"] is True
    for concept_id in (spec_id, run_id):
        concept = store.docs[concept_id]
        assert concept["relationships"] == {"#V#specific_to_user": [owner]}
        assert _document_visible_to_actor(concept, owner, organisation) is True
        assert (
            _document_visible_to_actor(concept, "#V#organisation_peer", organisation)
            is False
        )
    actor_only_creates = [
        item for item in store.create_calls if item["concept_id"] in {spec_id, run_id}
    ]
    assert len(actor_only_creates) == 2
    assert all(
        item["visibility_scope_mode"] == "user_only_default"
        for item in actor_only_creates
    )


def test_actor_only_experiment_refuses_preexisting_wider_spec_before_state_write(
    monkeypatch,
):
    (
        mod,
        store,
        _run_collection,
        _relations,
        _text_writes,
        singleton_writes,
    ) = _patch_runtime(monkeypatch)
    spec_id = "#V#wider_preexisting_spec"
    store.docs[spec_id] = {
        "concept_id": spec_id,
        "relationships": {
            "#V#specific_to_user": ["#V#user"],
            "#V#specific_to_organisation": ["#V#org"],
        },
        "concept_data": {},
        "attributes": {},
    }

    with pytest.raises(mod.ExperimentVisibilityScopeError):
        mod.create_experiment_spec(
            name="Must remain actor-only",
            experiment_spec_id=spec_id,
            namespace="#V#user@org",
            user_id="#V#user",
            org_id="#V#org",
            fixture_payload={"candidate_ref": {"body_sha256": "a" * 64}},
            require_actor_only_visibility=True,
        )

    assert "experiment_spec" not in store.docs[spec_id]["concept_data"]
    assert singleton_writes == []


def test_actor_only_experiment_refuses_preexisting_wider_run_before_state_write(
    monkeypatch,
):
    (
        mod,
        store,
        _run_collection,
        _relations,
        _text_writes,
        _singleton_writes,
    ) = _patch_runtime(monkeypatch)
    spec_id = "#V#actor_only_existing_spec"
    run_id = "#V#wider_preexisting_run"
    mod.create_experiment_spec(
        name="Actor-only experiment",
        experiment_spec_id=spec_id,
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        require_actor_only_visibility=True,
    )
    store.docs[run_id] = {
        "concept_id": run_id,
        "relationships": {
            "#V#specific_to_user": ["#V#user"],
            "#V#specific_to_organisation": ["#V#org"],
        },
        "concept_data": {},
        "attributes": {},
    }

    with pytest.raises(mod.ExperimentVisibilityScopeError):
        mod.start_experiment_run(
            experiment_spec_id=spec_id,
            run_id=run_id,
            namespace="#V#user@org",
            user_id="#V#user",
            org_id="#V#org",
            metadata={"learning_advice_experiment": {"binding_sha256": "a" * 64}},
            require_actor_only_visibility=True,
        )

    assert "experiment_run" not in store.docs[run_id]["concept_data"]


def test_experiment_run_service_full_lifecycle_emits_learning_signal(monkeypatch):
    (
        mod,
        store,
        run_collection,
        relations,
        text_writes,
        singleton_writes,
    ) = _patch_runtime(monkeypatch)

    created = mod.create_experiment_spec(
        name="Meeting invitation experiment",
        experiment_spec_id="#V#spec_meeting_test",
        namespace="#V#user@org",
        theory_id="#V#theory_meeting",
        target_workflow_ids=["#V#wf_meeting"],
        candidate_workflow_ids=["#V#wf_meeting", "#V#wf_backup"],
        baseline_workflow_id="#V#wf_backup",
        expected_outcomes=["structured_fields", "promotion_gate_kept_closed"],
        verdict_rules={"minimum_pass_count": 2},
        replay_policy={"retain_failing_cases": True},
        promotion_policy={"requires_manual_gate": True},
    )

    assert created["success"] is True
    assert created["experiment_spec_id"] == "#V#spec_meeting_test"
    spec_state = store.docs["#V#spec_meeting_test"]["concept_data"]["experiment_spec"]
    assert spec_state["theory_id"] == "#V#theory_meeting"
    assert spec_state["baseline_workflow_id"] == "#V#wf_backup"
    expected_outcome_writes = [
        row for row in text_writes if row["predicate"] == "#V#has_expected_outcome"
    ]
    assert len(expected_outcome_writes) == 2
    assert {
        row["target"] for row in relations if row["predicate"] == "#V#tests_workflow"
    } == {"#V#wf_meeting"}

    started = mod.start_experiment_run(
        experiment_spec_id="#V#spec_meeting_test",
        run_id="#V#run_meeting_test",
        selection_experience_id="selection-1",
    )

    assert started["success"] is True
    assert started["experiment_run"]["status"] == "running"
    assert "#V#run_meeting_test" in run_collection.docs
    assert set(run_collection.index_names) >= {
        "run_id_unique",
        "namespace_created_desc",
        "spec_created_desc",
        "theory_created_desc",
        "workflow_created_desc",
        "verdict_created_desc",
    }

    recorded = mod.record_experiment_observation(
        run_id="#V#run_meeting_test",
        observations=[
            {
                "schema_version": "strict_certification_experiment_observation.v1",
                "label": "structured_fields",
                "verdict": "pass",
                "observed_outcome": "structured_fields",
                "workflow_execution": {"workflow_id": "#V#wf_meeting"},
                "candidate_validation": {
                    "valid": True,
                    "definition_identity": {"hash": "candidate-hash"},
                },
                "trace_summary": {"completed_step_count": 3},
                "side_effect_audit": {"blocked_event_count": 0},
                "repair_hints": [
                    {
                        "scope": "workflow_contract",
                        "reason_code": "candidate_validated",
                    }
                ],
                "quality_signals": {"requires_follow_up": False},
                "assertion_classes": [
                    "workflow_candidate_validation",
                    "workflow_execution",
                ],
                "tool_invocations": [{"tool_name": "workflow_create_instance"}],
                "turn_execution_request_ids": ["req-1"],
                "execution_provenance": {
                    "trusted_runner_attestation": {
                        "schema_version": (
                            "operational_certification_runner_attestation.v1"
                        ),
                        "signature": "signed-digest",
                    }
                },
            },
            {
                "label": "promotion_gate_kept_closed",
                "verdict": "pass",
                "observed_outcome": "promotion_gate_kept_closed",
                "policy_decisions": [{"decision": "no_canonical_mutation"}],
            },
        ],
        turn_execution_request_ids=["req-2"],
    )

    assert recorded["success"] is True
    observed_outcome_writes = [
        row for row in text_writes if row["predicate"] == "#V#has_observed_outcome"
    ]
    assert len(observed_outcome_writes) == 2
    stored_run = store.docs["#V#run_meeting_test"]["concept_data"]["experiment_run"]
    assert stored_run["baseline_workflow_id"] == "#V#wf_backup"
    assert stored_run["turn_execution_request_ids"] == ["req-2", "req-1"]
    assert len(stored_run["evidence"]["workflow_execution"]) == 1
    assert stored_run["evidence"]["candidate_validation_results"] == [
        {
            "valid": True,
            "definition_identity": {"hash": "candidate-hash"},
        }
    ]
    assert stored_run["evidence"]["trace_summaries"] == [{"completed_step_count": 3}]
    assert stored_run["evidence"]["side_effect_audits"] == [{"blocked_event_count": 0}]
    assert stored_run["evidence"]["repair_hints"] == [
        {
            "scope": "workflow_contract",
            "reason_code": "candidate_validated",
        }
    ]
    assert stored_run["evidence"]["quality_signals"] == [{"requires_follow_up": False}]
    assert stored_run["evidence"]["assertion_classes"] == [
        "workflow_candidate_validation",
        "workflow_execution",
    ]
    canonical_readback = mod.get_experiment_run_state("#V#run_meeting_test")
    assert canonical_readback is not None
    canonical_observation = canonical_readback["observations"][0]
    assert canonical_observation["schema_version"] == (
        "strict_certification_experiment_observation.v1"
    )
    assert canonical_observation["execution_provenance"] == {
        "trusted_runner_attestation": {
            "schema_version": "operational_certification_runner_attestation.v1",
            "signature": "signed-digest",
        }
    }

    verdict = mod.compute_experiment_verdict(run_id="#V#run_meeting_test")

    assert verdict["success"] is True
    assert verdict["verdict"] == "pass"
    assert verdict["promotion_recommendation"]["recommended"] is True
    assert verdict["promotion_recommendation"]["promotion_ready_assertion_ids"] == [
        "assert-1"
    ]
    assert verdict["experiment_run"]["status"] == "completed"
    verdict_writes = [
        row
        for row in singleton_writes
        if row["predicate"] == "#V#has_experiment_verdict"
    ]
    assert len(verdict_writes) == 1

    learning = mod.emit_experiment_learning_signal(
        run_id="#V#run_meeting_test",
        expected_workflow_id="#V#wf_meeting",
    )

    assert learning["success"] is True
    assert learning["learning_signal"]["selection_outcome"] == "completed"
    assert learning["selection_experience"]["reward"] == 0.75
    replay_case = learning["learning_signal"]["replay_case"]
    assert replay_case["expected_workflow_id"] == "#V#wf_meeting"
    assert replay_case["baseline_workflow_id"] == "#V#wf_backup"
    assert replay_case["evidence"]["observation_total"] == 2


def test_compute_experiment_verdict_demotes_degraded_candidate_against_baseline(
    monkeypatch,
):
    mod, _store, _run_collection, _relations, _text_writes, _singleton_writes = (
        _patch_runtime(monkeypatch)
    )
    demotion_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mod,
        "build_workflow_prediction_envelope",
        lambda *, workflow_id, **_kwargs: {
            "success": True,
            "prediction_envelope": {
                "completion_rate": 0.55 if workflow_id == "#V#wf_candidate" else 0.95,
                "failure_rate": 0.35 if workflow_id == "#V#wf_candidate" else 0.05,
                "duration_ms": {
                    "p50": 1500 if workflow_id == "#V#wf_candidate" else 500
                },
                "llm_usage": {
                    "total_tokens": {
                        "p50": 240 if workflow_id == "#V#wf_candidate" else 120
                    }
                },
                "quality_proxy": {},
                "data_sufficiency": {"sufficient_for_prediction": True},
            },
            "sample_window": {"candidate_trace_count": 5, "matched_trace_count": 5},
        },
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **kwargs: (
            demotion_calls.append(kwargs)
            or {
                "workflow_id": kwargs["workflow_id"],
                "phase": kwargs["phase"],
                "published": kwargs["published"],
            }
        ),
    )

    mod.create_experiment_spec(
        name="Capability degradation check",
        experiment_spec_id="#V#spec_degradation",
        namespace="#V#user@org",
        target_workflow_ids=["#V#wf_candidate"],
        candidate_workflow_ids=["#V#wf_candidate"],
        baseline_workflow_id="#V#wf_baseline",
        expected_outcomes=["target_workflow_execution"],
        promotion_policy={
            "requires_manual_gate": True,
            "degradation_policy": {
                "minimum_baseline_runs": 3,
                "max_completion_rate_drop": 0.1,
                "max_failure_rate_increase": 0.1,
                "max_p50_duration_ratio": 1.2,
                "max_total_token_ratio": 1.5,
                "max_policy_violation_count": 0,
            },
        },
    )
    mod.start_experiment_run(
        experiment_spec_id="#V#spec_degradation",
        run_id="#V#run_degradation",
    )
    mod.record_experiment_observation(
        run_id="#V#run_degradation",
        observations=[
            {
                "label": "target_workflow_execution",
                "verdict": "pass",
                "observed_outcome": "completed",
                "side_effect_audit": {"blocked_event_count": 0},
            }
        ],
    )

    verdict = mod.compute_experiment_verdict(run_id="#V#run_degradation")

    assert verdict["success"] is True
    assert verdict["verdict"] == "fail"
    assert verdict["verdict_summary"]["reason"] == "degraded_against_baseline"
    assert verdict["promotion_recommendation"]["recommended"] is False
    assert verdict["promotion_recommendation"]["baseline_workflow_id"] == (
        "#V#wf_baseline"
    )
    assessment = verdict["degradation_assessment"]
    assert assessment["evaluated"] is True
    assert assessment["degraded"] is True
    assert assessment["candidate_workflow_id"] == "#V#wf_candidate"
    assert assessment["baseline_workflow_id"] == "#V#wf_baseline"
    assert assessment["demotion"] == {
        "applied": True,
        "lifecycle": {
            "workflow_id": "#V#wf_candidate",
            "phase": "demoted_due_to_degradation",
            "published": False,
        },
    }
    assert "completion_rate_regressed" in assessment["reasons"]
    assert "failure_rate_regressed" in assessment["reasons"]
    assert len(demotion_calls) == 1
    assert demotion_calls[0]["workflow_id"] == "#V#wf_candidate"
    assert demotion_calls[0]["phase"] == "demoted_due_to_degradation"
    assert demotion_calls[0]["published"] is False
    assert demotion_calls[0]["validation_passed"] is False
    assert demotion_calls[0]["postconditions_verified"] is False
    assert "completion_rate_regressed" in demotion_calls[0]["last_error"]


def test_compute_experiment_verdict_fails_on_forbidden_side_effect(monkeypatch):
    mod, _store, _run_collection, _relations, _text_writes, _singleton_writes = (
        _patch_runtime(monkeypatch)
    )
    diff_calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "compute_testing_theory_diff",
        lambda *, theory_id: (
            diff_calls.append(theory_id) or {"success": True, "diff": {}}
        ),
    )

    mod.create_experiment_spec(
        name="Forbidden side effect experiment",
        experiment_spec_id="#V#spec_forbidden",
        namespace="#V#user@org",
        theory_id="#V#theory_forbidden",
        target_workflow_ids=["#V#wf_forbidden"],
        expected_outcomes=["safe_outcome"],
        forbidden_side_effects=["calendar_mutation"],
    )
    mod.start_experiment_run(
        experiment_spec_id="#V#spec_forbidden",
        run_id="#V#run_forbidden",
    )
    mod.record_experiment_observation(
        run_id="#V#run_forbidden",
        observations=[
            {
                "label": "unsafe_path",
                "verdict": "pass",
                "observed_outcome": "unsafe_outcome",
                "evidence": {"side_effects": ["calendar_mutation"]},
            }
        ],
    )

    verdict = mod.compute_experiment_verdict(run_id="#V#run_forbidden")

    assert verdict["success"] is True
    assert verdict["verdict"] == "fail"
    assert verdict["verdict_summary"]["reason"] == "forbidden_side_effect_observed"
    assert verdict["promotion_recommendation"]["recommended"] is False
    assert verdict["experiment_run"]["status"] == "failed"
    assert diff_calls == []


def test_abort_experiment_run_preserves_evidence_without_content_failure(monkeypatch):
    mod, _store, _runs, _relations, _texts, singleton_writes = _patch_runtime(
        monkeypatch
    )
    binding_sha256 = "a" * 64
    mod.create_experiment_spec(
        name="Abortable experiment",
        experiment_spec_id="#V#spec_abortable",
    )
    mod.start_experiment_run(
        experiment_spec_id="#V#spec_abortable",
        run_id="#V#run_abortable",
        metadata={
            "learning_advice_experiment": {
                "schema_version": "learning_advice_experiment_run_binding.v1",
                "binding_sha256": binding_sha256,
            }
        },
    )
    mod.record_experiment_observation(
        run_id="#V#run_abortable",
        observations={
            "observation_id": "completed-trial",
            "observation_type": "learning_advice_trial",
            "verdict": "pass",
        },
        turn_execution_request_ids=["request-1"],
    )

    aborted = mod.abort_experiment_run(
        run_id="#V#run_abortable",
        reason_code="learning_advice_experiment_request_contamination",
        binding_metadata_key="learning_advice_experiment",
        expected_binding_sha256=binding_sha256,
    )
    readback = mod.get_experiment_run_state("#V#run_abortable")

    assert aborted["success"] is True
    assert aborted["idempotent"] is False
    assert readback is not None
    assert readback["status"] == "failed"
    assert readback["verdict"] == "inconclusive"
    assert [item["observation_id"] for item in readback["observations"]] == [
        "completed-trial"
    ]
    assert readback["turn_execution_request_ids"] == ["request-1"]
    assert readback["abort_summary"] == {
        "schema_version": "experiment_run_abort.v1",
        "reason_code": "learning_advice_experiment_request_contamination",
        "binding_metadata_key": "learning_advice_experiment",
        "binding_sha256": binding_sha256,
        "preserved_observation_count": 1,
        "preserved_turn_execution_request_count": 1,
        "aborted_at_utc": readback["completed_at_utc"],
    }
    assert readback["evidence"]["experiment_run_abort"] == readback["abort_summary"]
    assert singleton_writes[-1]["text"] == "inconclusive"

    repeated = mod.abort_experiment_run(
        run_id="#V#run_abortable",
        reason_code="learning_advice_experiment_request_contamination",
        binding_metadata_key="learning_advice_experiment",
        expected_binding_sha256=binding_sha256,
    )
    assert repeated["success"] is True
    assert repeated["idempotent"] is True


def test_abort_experiment_run_rejects_binding_mismatch_without_mutation(monkeypatch):
    mod, _store, _runs, _relations, _texts, _singleton_writes = _patch_runtime(
        monkeypatch
    )
    mod.create_experiment_spec(
        name="Bound experiment",
        experiment_spec_id="#V#spec_bound_abort",
    )
    mod.start_experiment_run(
        experiment_spec_id="#V#spec_bound_abort",
        run_id="#V#run_bound_abort",
        metadata={"learning_advice_experiment": {"binding_sha256": "b" * 64}},
    )

    result = mod.abort_experiment_run(
        run_id="#V#run_bound_abort",
        reason_code="learning_advice_experiment_integrity_failure",
        binding_metadata_key="learning_advice_experiment",
        expected_binding_sha256="c" * 64,
    )

    assert result == {"success": False, "error": "experiment_run_binding_mismatch"}
    readback = mod.get_experiment_run_state("#V#run_bound_abort")
    assert readback is not None
    assert readback["status"] == "running"
    assert readback["verdict"] is None
    assert readback.get("abort_summary") is None


def test_prepare_meeting_invitation_experiment_spec_builds_gateable_fixture(
    monkeypatch,
):
    from src.backend.services import experiment_run_service as mod

    captured: dict[str, Any] = {}

    def _create_experiment_spec(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "success": True,
            "experiment_spec_id": "#V#meeting_spec",
            "experiment_spec": {"experiment_spec_id": "#V#meeting_spec"},
        }

    monkeypatch.setattr(mod, "create_experiment_spec", _create_experiment_spec)

    result = mod.prepare_meeting_invitation_experiment_spec(
        invitation_text="Please meet on Monday at 10am to discuss the roadmap.",
        candidate_workflow_ids=["#V#wf_meeting", "#V#wf_backup"],
        expected_meeting_type="project_meeting",
        expected_structure_fields=["title", "time", "participants"],
        expected_downstream_actions=["draft_calendar_entry"],
    )

    assert result["success"] is True
    assert result["theory_slice_inputs"]["experiment_spec_id"] == "#V#meeting_spec"
    assert (
        result["theory_slice_inputs"]["promotion_policy"]["requires_manual_gate"]
        is True
    )
    assert len(result["seed_claims"]) == 2
    assert captured["fixture_payload"]["invitation_text"].startswith("Please meet")
    assert captured["target_workflow_ids"] == ["#V#wf_meeting"]
    assert captured["forbidden_side_effects"] == [
        "canonical_calendar_mutation",
        "canonical_task_mutation",
        "external_action_without_gate",
    ]
    assert captured["verdict_rules"] == {
        "require_all_expected_outcomes": True,
        "minimum_pass_count": 4,
    }
    assert captured["metadata"]["scenario"] == "meeting_invitation_testing"


def test_prepare_meeting_invitation_experiment_spec_normalises_single_candidate_id(
    monkeypatch,
):
    from src.backend.services import experiment_run_service as mod

    captured: dict[str, Any] = {}

    def _create_experiment_spec(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "success": True,
            "experiment_spec_id": "#V#meeting_spec_single",
            "experiment_spec": {"experiment_spec_id": "#V#meeting_spec_single"},
        }

    monkeypatch.setattr(mod, "create_experiment_spec", _create_experiment_spec)

    result = mod.prepare_meeting_invitation_experiment_spec(
        invitation_text="Please meet on Monday at 10am to discuss the roadmap.",
        candidate_workflow_ids="#V#meeting_invitation_candidate_workflow",
    )

    assert result["success"] is True
    assert captured["candidate_workflow_ids"] == [
        "#V#meeting_invitation_candidate_workflow"
    ]
    assert captured["target_workflow_ids"] == [
        "#V#meeting_invitation_candidate_workflow"
    ]


def test_prepare_experiment_spec_from_template_resolves_optional_inputs(monkeypatch):
    from src.backend.services import experiment_run_service as mod

    captured: dict[str, Any] = {}

    def _create_experiment_spec(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "success": True,
            "experiment_spec_id": "#V#scenario_spec",
            "experiment_spec": {"experiment_spec_id": "#V#scenario_spec"},
        }

    monkeypatch.setattr(mod, "create_experiment_spec", _create_experiment_spec)

    result = mod.prepare_experiment_spec_from_template(
        scenario_template={
            "schema_version": "testing_experiment_scenario_template.v1",
            "name": "Scenario-driven workflow validation",
            "description": "Generic testing scenario authored in workflow metadata.",
            "fixture_payload": {
                "invitation_text": {"$input": "invitation_text"},
            },
            "expected_outcomes": [
                {
                    "label": "structured_fields",
                    "expected_fields": {
                        "$input": "expected_structure_fields",
                        "$default": ["title", "time"],
                    },
                },
                {
                    "$if_input": "expected_downstream_actions",
                    "then": {
                        "label": "downstream_actions",
                        "expected_actions": {"$input": "expected_downstream_actions"},
                    },
                },
            ],
            "theory_setup": {
                "seed_claims": [
                    {
                        "source_id": "#V#meeting_invitation_testing_workflow",
                        "predicate": "#V#has_hypothesis",
                        "target": {
                            "$input": "expected_meeting_type",
                            "$default": "safe workflow candidate",
                        },
                        "target_kind": "text",
                    }
                ]
            },
            "verdict_rules": {
                "require_all_expected_outcomes": True,
            },
            "promotion_policy": {
                "requires_manual_gate": True,
            },
        },
        template_inputs={
            "invitation_text": "Meet on Tuesday to discuss the roadmap.",
            "candidate_workflow_ids": ["#V#wf_candidate", "#V#wf_backup"],
            "expected_meeting_type": "project_meeting",
        },
    )

    assert result["success"] is True
    assert result["scenario_template_schema_version"] == (
        "testing_experiment_scenario_template.v1"
    )
    assert result["theory_slice_inputs"]["experiment_spec_id"] == "#V#scenario_spec"
    assert result["theory_slice_inputs"]["expected_observations"] == [
        {
            "label": "structured_fields",
            "expected_fields": ["title", "time"],
        }
    ]
    assert result["theory_slice_inputs"]["promotion_policy"] == {
        "requires_manual_gate": True,
    }
    assert result["seed_claims"] == [
        {
            "source_id": "#V#meeting_invitation_testing_workflow",
            "predicate": "#V#has_hypothesis",
            "target": "project_meeting",
            "target_kind": "text",
        }
    ]
    assert captured["candidate_workflow_ids"] == ["#V#wf_candidate", "#V#wf_backup"]
    assert captured["target_workflow_ids"] == ["#V#wf_candidate"]
    assert captured["fixture_payload"] == {
        "invitation_text": "Meet on Tuesday to discuss the roadmap."
    }
    assert captured["verdict_rules"] == {
        "require_all_expected_outcomes": True,
        "minimum_pass_count": 1,
    }


def test_execute_regression_suite_tier2_delegates_to_benchmark_harness(monkeypatch):
    from src.backend.services import experiment_run_service as mod

    calls: dict[str, Any] = {}
    recorded_calls: list[dict[str, Any]] = []
    benchmark_module = ModuleType("src.backend.services.kb_clone_benchmark_service")

    def _build_default_benchmark_app() -> str:
        calls["app_built"] = True
        return "benchmark-app"

    def _run_kb_clone_benchmark(
        *,
        scenario: dict[str, Any],
        output_root: str,
        app: Any,
        mongo_client: Any,
    ) -> dict[str, Any]:
        calls["scenario"] = scenario
        calls["output_root"] = output_root
        calls["app"] = app
        calls["mongo_client"] = mongo_client
        return {"metrics": {"aggregate": {"all_runs_passed": True}}}

    cast(
        Any, benchmark_module
    ).build_default_benchmark_app = _build_default_benchmark_app
    cast(Any, benchmark_module).run_kb_clone_benchmark = _run_kb_clone_benchmark
    monkeypatch.setitem(
        sys.modules,
        "src.backend.services.kb_clone_benchmark_service",
        benchmark_module,
    )
    monkeypatch.setattr(mod, "get_db", lambda: SimpleNamespace(client="mongo-client"))
    monkeypatch.setattr(
        mod,
        "record_experiment_observation",
        lambda *, run_id, observations, turn_execution_request_ids=(): (
            recorded_calls.append(
                {
                    "run_id": run_id,
                    "observations": observations,
                    "turn_execution_request_ids": turn_execution_request_ids,
                }
            )
            or {
                "success": True,
                "recorded_observations": list(observations),
            }
        ),
    )

    result = mod.execute_regression_suite(
        execution_tier="tier2",
        benchmark_scenario={"scenario_id": "suite-1"},
        output_root="data/testing_workflows/benchmarks/test-suite",
        run_id="#V#run_suite",
        suite_policy={
            "schema_version": "testing_regression_suite_policy.v1",
            "default_execution_tier": "tier1",
            "tiers": {
                "tier1": {"mode": "cases"},
                "tier2": {"mode": "benchmark"},
                "benchmark": {"alias_for": "tier2"},
            },
        },
    )

    assert result["success"] is True
    assert result["execution_tier"] == "tier2"
    assert result["verdict"] == "pass"
    assert recorded_calls == [
        {
            "run_id": "#V#run_suite",
            "observations": [
                {
                    "label": "regression_suite_tier2",
                    "verdict": "pass",
                    "observed_outcome": "pass",
                    "evidence": {
                        "suite_result": {
                            "metrics": {"aggregate": {"all_runs_passed": True}}
                        },
                        "execution_tier": "tier2",
                    },
                    "metrics": {"aggregate": {"all_runs_passed": True}},
                }
            ],
            "turn_execution_request_ids": (),
        }
    ]
    assert calls == {
        "app_built": True,
        "scenario": {"scenario_id": "suite-1"},
        "output_root": "data/testing_workflows/benchmarks/test-suite",
        "app": "benchmark-app",
        "mongo_client": "mongo-client",
    }


def test_execute_regression_suite_resolves_aliases_from_suite_policy() -> None:
    from src.backend.services import experiment_run_service as mod

    result = mod.execute_regression_suite(
        execution_tier="benchmark",
        cases=(),
        suite_policy={
            "schema_version": "testing_regression_suite_policy.v1",
            "default_execution_tier": "tier1",
            "tiers": {
                "tier1": {"mode": "cases"},
                "tier2": {
                    "mode": "benchmark",
                    "output_root_default": "data/custom_benchmarks",
                },
                "benchmark": {"alias_for": "tier2"},
            },
        },
    )

    assert result == {
        "success": False,
        "error": "benchmark_scenario_required_for_benchmark_suite",
        "execution_tier": "tier2",
    }
