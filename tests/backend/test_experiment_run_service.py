from __future__ import annotations

import copy
import sys
from types import ModuleType, SimpleNamespace
from typing import Any


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

    def create_concept(
        self,
        *,
        name: str,
        concept_id: str,
        description: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        doc = {
            "concept_id": concept_id,
            "name": name,
            "description": description,
            "concept_data": {},
            "attributes": {},
        }
        self.docs[concept_id] = doc
        return copy.deepcopy(doc)

    def update_concept(self, concept_id: str, updates: dict[str, Any]) -> dict[str, Any]:
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

    def create_index(self, _keys: list[tuple[str, int]], *, name: str, **_kwargs: Any) -> None:
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

    def find_one(self, query: dict[str, Any], _projection: dict[str, Any] | None = None) -> dict[str, Any] | None:
        run_id = str(query.get("run_id") or "")
        doc = self.docs.get(run_id)
        return copy.deepcopy(doc) if isinstance(doc, dict) else None


class _DB:
    def __init__(self, collection: _RunCollection, *, client: Any = "mongo-client") -> None:
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

    def _add_relationship(*, source_id: str, predicate: str, target: str, **_kwargs: Any):
        payload = {"source_id": source_id, "predicate": predicate, "target": target}
        relations.append(payload)
        return payload

    def _upsert_text_for_concept(**kwargs: Any) -> dict[str, Any]:
        text_writes.append(dict(kwargs))
        return {"relation_id": f"rel-{len(text_writes)}", "relation_created": True}

    def _upsert_singleton_text_relation(**kwargs: Any) -> dict[str, Any]:
        singleton_writes.append(dict(kwargs))
        return {"relation_id": f"singleton-{len(singleton_writes)}", "relation_created": True}

    monkeypatch.setattr(mod, "_RUN_INDEXES_READY", False)
    monkeypatch.setattr(mod.concept_service, "create_concept", store.create_concept)
    monkeypatch.setattr(mod.concept_service, "update_concept", store.update_concept)
    monkeypatch.setattr(mod, "get_concept_by_concept_id", store.get_concept)
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
        expected_outcomes=["structured_fields", "promotion_gate_kept_closed"],
        verdict_rules={"minimum_pass_count": 2},
        replay_policy={"retain_failing_cases": True},
        promotion_policy={"requires_manual_gate": True},
    )

    assert created["success"] is True
    assert created["experiment_spec_id"] == "#V#spec_meeting_test"
    assert store.docs["#V#spec_meeting_test"]["concept_data"]["experiment_spec"]["theory_id"] == "#V#theory_meeting"
    expected_outcome_writes = [
        row for row in text_writes if row["predicate"] == "#V#has_expected_outcome"
    ]
    assert len(expected_outcome_writes) == 2
    assert {
        row["target"]
        for row in relations
        if row["predicate"] == "#V#tests_workflow"
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
                "label": "structured_fields",
                "verdict": "pass",
                "observed_outcome": "structured_fields",
                "workflow_execution": {"workflow_id": "#V#wf_meeting"},
                "tool_invocations": [{"tool_name": "workflow_create_instance"}],
                "turn_execution_request_ids": ["req-1"],
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
    assert stored_run["turn_execution_request_ids"] == ["req-2", "req-1"]
    assert len(stored_run["evidence"]["workflow_execution"]) == 1

    verdict = mod.compute_experiment_verdict(run_id="#V#run_meeting_test")

    assert verdict["success"] is True
    assert verdict["verdict"] == "pass"
    assert verdict["promotion_recommendation"]["recommended"] is True
    assert verdict["promotion_recommendation"]["promotion_ready_assertion_ids"] == ["assert-1"]
    assert verdict["experiment_run"]["status"] == "completed"
    verdict_writes = [
        row for row in singleton_writes if row["predicate"] == "#V#has_experiment_verdict"
    ]
    assert len(verdict_writes) == 1

    learning = mod.emit_experiment_learning_signal(
        run_id="#V#run_meeting_test",
        expected_workflow_id="#V#wf_meeting",
        baseline_workflow_id="#V#wf_backup",
    )

    assert learning["success"] is True
    assert learning["learning_signal"]["selection_outcome"] == "completed"
    assert learning["selection_experience"]["reward"] == 0.75
    replay_case = learning["learning_signal"]["replay_case"]
    assert replay_case["expected_workflow_id"] == "#V#wf_meeting"
    assert replay_case["baseline_workflow_id"] == "#V#wf_backup"
    assert replay_case["evidence"]["observation_total"] == 2


def test_compute_experiment_verdict_fails_on_forbidden_side_effect(monkeypatch):
    mod, _store, _run_collection, _relations, _text_writes, _singleton_writes = _patch_runtime(
        monkeypatch
    )
    diff_calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "compute_testing_theory_diff",
        lambda *, theory_id: diff_calls.append(theory_id) or {"success": True, "diff": {}},
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


def test_prepare_meeting_invitation_experiment_spec_builds_gateable_fixture(monkeypatch):
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
    assert result["theory_slice_inputs"]["promotion_policy"]["requires_manual_gate"] is True
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

    benchmark_module.build_default_benchmark_app = _build_default_benchmark_app
    benchmark_module.run_kb_clone_benchmark = _run_kb_clone_benchmark
    monkeypatch.setitem(
        sys.modules,
        "src.backend.services.kb_clone_benchmark_service",
        benchmark_module,
    )
    monkeypatch.setattr(mod, "get_db", lambda: SimpleNamespace(client="mongo-client"))
    monkeypatch.setattr(
        mod,
        "record_experiment_observation",
        lambda *, run_id, observations, turn_execution_request_ids=(): recorded_calls.append(
            {
                "run_id": run_id,
                "observations": observations,
                "turn_execution_request_ids": turn_execution_request_ids,
            }
        )
        or {
            "success": True,
            "recorded_observations": list(observations),
        },
    )

    result = mod.execute_regression_suite(
        execution_tier="tier2",
        benchmark_scenario={"scenario_id": "suite-1"},
        output_root="data/testing_workflows/benchmarks/test-suite",
        run_id="#V#run_suite",
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
                        "suite_result": {"metrics": {"aggregate": {"all_runs_passed": True}}},
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
