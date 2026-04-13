from collections.abc import Iterator
from typing import Any


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def sort(self, field: str, _direction: Any) -> "_Cursor":
        self._docs.sort(key=lambda doc: str(doc.get(field) or ""))
        return self

    def limit(self, limit: int) -> "_Cursor":
        self._docs = self._docs[:limit]
        return self

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._docs)


class _Collection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def find(self, _query: dict[str, Any], _projection: dict[str, int]) -> _Cursor:
        return _Cursor(self._docs)


def _sample_docs() -> list[dict[str, Any]]:
    return [
        {
            "memory_id": "#V#memory_follow_up",
            "request_id": "req-follow-up",
            "episode_id": "ep-follow-up",
            "workflow_id": "#V#episode_evaluation_workflow",
            "namespace": "#V#user",
            "created_at_utc": "2026-03-29T00:00:00Z",
            "verdict": "follow_up_required",
            "confidence": 0.85,
            "unresolved_check_count": 2,
            "routing_decision": "create_task",
            "routing_repeat_count": 0,
            "routing_fingerprint": "fp-a",
            "routing_task_action": None,
            "routing_jira_action": None,
            "remediation_task_ids": [],
            "remediation_issue_keys": [],
            "recommendations": ["Create a remediation task"],
            "improvement_suggestion_count": 0,
            "improvement_suggestion_categories": [],
            "improvement_target_workflow_ids": [],
            "improvement_target_tool_names": [],
            "receipt_hash": "receipt-a",
        },
        {
            "memory_id": "#V#memory_false_positive",
            "request_id": "req-pass-remediated",
            "episode_id": "ep-pass-remediated",
            "workflow_id": "#V#episode_evaluation_workflow",
            "namespace": "#V#user",
            "created_at_utc": "2026-03-29T00:01:00Z",
            "verdict": "pass",
            "confidence": 0.6,
            "unresolved_check_count": 0,
            "routing_decision": "create_task",
            "routing_repeat_count": 0,
            "routing_fingerprint": "fp-b",
            "routing_task_action": "created_task",
            "routing_jira_action": None,
            "remediation_task_ids": ["#V#task_1"],
            "remediation_issue_keys": [],
            "recommendations": ["Document but do not escalate"],
            "improvement_suggestion_count": 0,
            "improvement_suggestion_categories": [],
            "improvement_target_workflow_ids": [],
            "improvement_target_tool_names": [],
            "receipt_hash": "receipt-b",
        },
        {
            "memory_id": "#V#memory_remediated",
            "request_id": "req-remediated",
            "episode_id": "ep-remediated",
            "workflow_id": "#V#episode_evaluation_workflow",
            "namespace": "#V#user",
            "created_at_utc": "2026-03-29T00:02:00Z",
            "verdict": "fail",
            "confidence": 0.91,
            "unresolved_check_count": 3,
            "routing_decision": "create_task",
            "routing_repeat_count": 1,
            "routing_fingerprint": "fp-c",
            "routing_task_action": "created_task",
            "routing_jira_action": None,
            "remediation_task_ids": ["#V#task_2"],
            "remediation_issue_keys": [],
            "recommendations": ["Fix workflow defect"],
            "implicated_workflow_ids": ["#V#episode_evaluation_workflow"],
            "improvement_suggestion_count": 1,
            "improvement_suggestion_categories": ["workflow_change"],
            "improvement_target_workflow_ids": ["#V#episode_evaluation_workflow"],
            "improvement_target_tool_names": [],
            "receipt_hash": "receipt-c1",
        },
        {
            "memory_id": "#V#memory_recurrence",
            "request_id": "req-recurrence",
            "episode_id": "ep-recurrence",
            "workflow_id": "#V#episode_evaluation_workflow",
            "namespace": "#V#user",
            "created_at_utc": "2026-03-29T00:03:00Z",
            "verdict": "fail",
            "confidence": 0.88,
            "unresolved_check_count": 2,
            "routing_decision": "create_task",
            "routing_repeat_count": 2,
            "routing_fingerprint": "fp-c",
            "routing_task_action": None,
            "routing_jira_action": None,
            "remediation_task_ids": [],
            "remediation_issue_keys": [],
            "recommendations": ["Escalate recurrence"],
            "implicated_tool_names": ["fetch_concept_content"],
            "improvement_suggestion_count": 1,
            "improvement_suggestion_categories": ["tool_addition"],
            "improvement_target_workflow_ids": [],
            "improvement_target_tool_names": ["fetch_concept_content"],
            "receipt_hash": "receipt-c2",
        },
        {
            "memory_id": "#V#memory_inconclusive",
            "request_id": "req-inconclusive",
            "episode_id": "ep-inconclusive",
            "workflow_id": "#V#episode_evaluation_workflow",
            "namespace": "#V#user",
            "created_at_utc": "2026-03-29T00:04:00Z",
            "verdict": "inconclusive",
            "confidence": 0.55,
            "unresolved_check_count": 1,
            "routing_decision": "memory_only",
            "routing_repeat_count": 0,
            "routing_fingerprint": "fp-d",
            "routing_task_action": None,
            "routing_jira_action": None,
            "remediation_task_ids": [],
            "remediation_issue_keys": [],
            "recommendations": ["Gather more evidence"],
            "improvement_suggestion_count": 1,
            "improvement_suggestion_categories": ["critic_self_improvement"],
            "improvement_target_workflow_ids": [],
            "improvement_target_tool_names": [],
            "receipt_hash": "receipt-d",
        },
    ]


def _state_for(memory_id: str) -> dict[str, Any]:
    return {
        "subject_episode": {
            "subject_kind": "workflow_instance",
            "instance_id": f"inst-{memory_id.rsplit('_', 1)[-1]}",
            "stable_key": memory_id,
            "turn_id": f"turn-{memory_id.rsplit('_', 1)[-1]}",
        },
        "critic": {
            "verdict": "fail" if "false_positive" not in memory_id else "pass",
            "confidence": 0.8,
            "summary": {
                "summary": "Critic memory summary",
                "root_cause_count": 1,
            },
        },
        "routing": {
            "decision": "create_task",
            "repeat_count": 1 if "recurrence" in memory_id else 0,
        },
    }


def test_build_episode_critique_benchmark_report_returns_metrics_and_sampled_cases(
    monkeypatch,
):
    from src.backend.services.episode_critique_benchmark_service import (
        build_episode_critique_benchmark_report,
    )

    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.get_episode_critique_memories_collection",
        lambda: _Collection(_sample_docs()),
    )
    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.get_episode_critique_memory_state",
        _state_for,
    )
    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.build_episode_critic_evidence_bundle",
        lambda **kwargs: {
            "success": True,
            "ready_for_critic": kwargs.get("request_id") != "req-inconclusive",
            "fail_closed": kwargs.get("request_id") == "req-inconclusive",
            "fail_closed_reason_codes": (
                ["missing_request_context"]
                if kwargs.get("request_id") == "req-inconclusive"
                else []
            ),
            "bundle_receipt": {"request_id": kwargs.get("request_id")},
            "episode_locator": {
                "request_id": kwargs.get("request_id"),
                "instance_id": kwargs.get("instance_id"),
                "workflow_id": "#V#episode_evaluation_workflow",
            },
            "source_resolution": {
                "record_found": True,
                "episode_found": True,
                "chat_history_found": True,
                "execution_trace_found": True,
            },
            "capability_gaps": (
                [{"gap_id": "missing_request_context"}]
                if kwargs.get("request_id") == "req-inconclusive"
                else []
            ),
        },
    )

    report = build_episode_critique_benchmark_report(
        namespace="#V#user",
        baseline_precision_proxy_pct=80,
        baseline_recall_proxy_pct=40,
        baseline_post_remediation_recurrence_rate_pct=20,
        regression_tolerance_pct=5,
    )

    assert report["success"] is True
    assert len(report["benchmark_fingerprint"]) == 16

    remediation_metrics = report["metrics"]["remediation_metrics"]
    assert remediation_metrics["false_positive_task_proxy_count"] == 1
    assert remediation_metrics["false_negative_task_proxy_count"] == 2
    assert remediation_metrics["task_creation_precision_proxy_pct"] == 50.0
    assert remediation_metrics["task_creation_recall_proxy_pct"] == 33.33

    recurrence_metrics = report["metrics"]["recurrence_metrics"]
    assert recurrence_metrics["remediated_fingerprint_count"] == 2
    assert recurrence_metrics["post_remediation_recurrence_fingerprint_count"] == 1
    assert recurrence_metrics["post_remediation_recurrence_rate_pct"] == 50.0

    improvement_metrics = report["metrics"]["improvement_suggestion_metrics"]
    assert improvement_metrics["episode_with_suggestions_count"] == 3
    assert improvement_metrics["strong_actionable_episode_with_suggestions_count"] == 2
    assert improvement_metrics["strong_actionable_episode_missing_suggestions_count"] == 1
    assert (
        improvement_metrics["strong_actionable_episode_with_useful_suggestions_count"]
        == 2
    )
    assert improvement_metrics["suggestion_usefulness_proxy_pct"] == 100.0

    sampled_meta_audit = report["sampled_meta_audit"]
    assert sampled_meta_audit["non_recursive"] is True
    assert sampled_meta_audit["bucket_counts"]["false_positive_task_proxy"] == 1
    assert sampled_meta_audit["bucket_counts"]["false_negative_task_proxy"] == 2
    assert sampled_meta_audit["bucket_counts"]["inconclusive_actionable"] == 1
    assert sampled_meta_audit["cases"][0]["improvement_suggestion_count"] >= 0

    regression = report["regression_assessment"]
    assert regression["regression_detected"] is True

    signals = {
        signal["signal_id"]: signal["status"] for signal in report["benchmark_signals"]
    }
    assert signals["low_signal_remediation_suppressed"] == "fail"
    assert signals["strong_actionable_findings_receive_remediation"] == "fail"
    assert (
        signals["strong_actionable_episodes_receive_improvement_suggestions"] == "fail"
    )
    assert signals["improvement_suggestions_target_actionable_surface"] == "pass"

    gap_ids = {gap["gap_id"] for gap in report["capability_gaps"]}
    assert "sampled_meta_audit_bundle_fail_closed" in gap_ids
    assert "missing_improvement_suggestions_for_actionable_episodes" in gap_ids


def test_build_episode_critique_benchmark_report_records_experiment_observation(
    monkeypatch,
):
    from src.backend.services.episode_critique_benchmark_service import (
        build_episode_critique_benchmark_report,
    )

    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.get_episode_critique_memories_collection",
        lambda: _Collection(_sample_docs()),
    )
    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.get_episode_critique_memory_state",
        _state_for,
    )
    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.build_episode_critic_evidence_bundle",
        lambda **kwargs: {
            "success": True,
            "ready_for_critic": True,
            "fail_closed": False,
            "fail_closed_reason_codes": [],
            "bundle_receipt": {"request_id": kwargs.get("request_id")},
            "episode_locator": {
                "request_id": kwargs.get("request_id"),
                "instance_id": kwargs.get("instance_id"),
                "workflow_id": "#V#episode_evaluation_workflow",
            },
            "source_resolution": {
                "record_found": True,
                "episode_found": True,
                "chat_history_found": True,
                "execution_trace_found": True,
            },
            "capability_gaps": [],
        },
    )
    def _record_experiment_observation(**kwargs: Any) -> dict[str, Any]:
        recorded["call"] = kwargs
        return {
            "success": True,
            "recorded_observations": kwargs.get("observations", []),
        }

    monkeypatch.setattr(
        "src.backend.services.experiment_run_service.record_experiment_observation",
        _record_experiment_observation,
    )

    report = build_episode_critique_benchmark_report(
        namespace="#V#user",
        run_id="run-1609",
        max_audit_cases=2,
    )

    experiment_recording = report["experiment_recording"]
    assert experiment_recording["run_id"] == "run-1609"
    assert experiment_recording["success"] is True
    assert recorded["call"]["run_id"] == "run-1609"
    observation = recorded["call"]["observations"][0]
    assert observation["label"] == "episode_critique_benchmark"
    assert observation["policy_decisions"][0]["decision"] == "sampled_meta_audit_non_recursive"
