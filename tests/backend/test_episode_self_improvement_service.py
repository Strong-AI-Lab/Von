from __future__ import annotations

from typing import Any

from src.backend.services import episode_self_improvement_service as svc


def test_launch_episode_self_improvement_workflows_suppresses_when_proposal_pending(
    monkeypatch,
) -> None:
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        svc,
        "_load_self_improvement_profile",
        lambda **_kwargs: (
            {
                "candidate_selection_policy": {
                    "max_candidate_launches": 3,
                    "priority_order": ["high", "medium", "low"],
                    "eligible_target_surfaces": ["workflow"],
                    "dedupe_identity_fields_by_surface": {
                        "workflow": ["target_workflow_id"]
                    },
                },
                "benchmark_policy": {"scan_limit": 200, "max_audit_cases": 5},
            },
            {},
        ),
    )
    monkeypatch.setattr(
        svc,
        "get_workflow_authoring_proposal",
        lambda _workflow_id: {"proposal_id": "proposal-1", "status": "pending_review"},
    )

    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_self_improvement",
        lambda **kwargs: recorded.update(kwargs) or {"state": {"memory_id": kwargs["memory_id"]}},
    )

    result = svc.launch_episode_self_improvement_workflows(
        memory_state={
            "memory_id": "#V#episode_critique_memory_abc",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "improvement_suggestions": [
                {
                    "suggestion_id": "workflow_change_alpha",
                    "priority": "high",
                    "target_surface": "workflow",
                    "target_workflow_id": "#V#alpha_workflow",
                }
            ],
        }
    )

    assert result["success"] is True
    assert result["launches"][0]["status"] == "suppressed_pending_review_proposal"
    assert recorded["launches"][0]["target_workflow_id"] == "#V#alpha_workflow"


def test_launch_episode_self_improvement_workflows_accepts_user_only_namespace(
    monkeypatch,
) -> None:
    recorded: dict[str, Any] = {}
    submissions: list[dict[str, Any]] = []
    monkeypatch.setattr(
        svc,
        "_load_self_improvement_profile",
        lambda **_kwargs: (
            {
                "candidate_selection_policy": {
                    "max_candidate_launches": 3,
                    "priority_order": ["high", "medium", "low"],
                    "eligible_target_surfaces": ["workflow"],
                    "dedupe_identity_fields_by_surface": {
                        "workflow": ["target_workflow_id"]
                    },
                },
                "benchmark_policy": {"scan_limit": 200, "max_audit_cases": 5},
            },
            {},
        ),
    )

    class _Submission:
        success = True
        status = "created"
        instance_id = "#V#wf_instance_self_improvement_1"
        error_code = None
        error = None

    monkeypatch.setattr(
        svc,
        "get_workflow_authoring_proposal",
        lambda _workflow_id: {},
    )
    monkeypatch.setattr(
        svc,
        "submit_verified_workflow_instance",
        lambda **kwargs: submissions.append(kwargs) or _Submission(),
    )
    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_self_improvement",
        lambda **kwargs: recorded.update(kwargs)
        or {"state": {"memory_id": kwargs["memory_id"]}},
    )

    result = svc.launch_episode_self_improvement_workflows(
        memory_state={
            "memory_id": "#V#episode_critique_memory_user_only",
            "namespace": "#V#user",
            "user_id": "#V#user",
            "improvement_suggestions": [
                {
                    "suggestion_id": "workflow_change_alpha",
                    "priority": "high",
                    "target_surface": "workflow",
                    "target_workflow_id": "#V#alpha_workflow",
                }
            ],
        }
    )

    assert result["success"] is True
    assert result["launched_count"] == 1
    assert submissions[0]["namespace"] == "#V#user"
    assert submissions[0]["user_id"] == "#V#user"
    assert submissions[0]["org_id"] is None
    assert submissions[0]["inputs"]["org_id"] is None
    assert recorded["launches"][0]["success"] is True


def test_launch_episode_self_improvement_workflows_fails_closed_when_profile_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "_load_self_improvement_profile",
        lambda **_kwargs: (
            None,
            {"error_code": "episode_self_improvement_profile_unavailable"},
        ),
    )

    result = svc.launch_episode_self_improvement_workflows(
        memory_state={
            "memory_id": "#V#episode_critique_memory_abc",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "improvement_suggestions": [],
        }
    )

    assert result["success"] is False
    assert result["reason"] == "episode_self_improvement_profile_unavailable"


def test_launch_episode_self_improvement_workflows_uses_profile_budget_and_priority(
    monkeypatch,
) -> None:
    submissions: list[dict[str, Any]] = []
    recorded: dict[str, Any] = {}

    class _Submission:
        success = True
        status = "created"
        instance_id = "#V#wf_instance_self_improvement_1"
        error_code = None
        error = None

    monkeypatch.setattr(
        svc,
        "_load_self_improvement_profile",
        lambda **_kwargs: (
            {
                "candidate_selection_policy": {
                    "max_candidate_launches": 1,
                    "priority_order": ["medium", "high", "low"],
                    "eligible_target_surfaces": ["workflow"],
                    "dedupe_identity_fields_by_surface": {
                        "workflow": ["target_workflow_id"]
                    },
                },
                "benchmark_policy": {"scan_limit": 200, "max_audit_cases": 5},
            },
            {},
        ),
    )
    monkeypatch.setattr(svc, "get_workflow_authoring_proposal", lambda _workflow_id: {})
    monkeypatch.setattr(
        svc,
        "submit_verified_workflow_instance",
        lambda **kwargs: submissions.append(kwargs) or _Submission(),
    )
    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_self_improvement",
        lambda **kwargs: recorded.update(kwargs) or {"state": {"memory_id": kwargs["memory_id"]}},
    )

    result = svc.launch_episode_self_improvement_workflows(
        memory_state={
            "memory_id": "#V#episode_critique_memory_profile_budget",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "improvement_suggestions": [
                {
                    "suggestion_id": "workflow_change_alpha",
                    "priority": "high",
                    "target_surface": "workflow",
                    "target_workflow_id": "#V#alpha_workflow",
                },
                {
                    "suggestion_id": "workflow_change_beta",
                    "priority": "medium",
                    "target_surface": "workflow",
                    "target_workflow_id": "#V#beta_workflow",
                },
            ],
        }
    )

    assert result["success"] is True
    assert result["launched_count"] == 1
    assert submissions[0]["inputs"]["target_workflow_id"] == "#V#beta_workflow"
    assert recorded["launches"][0]["target_workflow_id"] == "#V#beta_workflow"


def test_submit_workflow_improvement_proposal_records_proposal_and_builds_launch(
    monkeypatch,
) -> None:
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        svc,
        "submit_workflow_authoring_proposal",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "proposal": {
                "proposal_id": "proposal-1",
                "status": "pending_review",
            },
            "candidate_validation": {"valid": True},
        },
    )
    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_self_improvement",
        lambda **kwargs: recorded.update(kwargs) or {"success": True},
    )

    result = svc.submit_workflow_improvement_proposal(
        episode_critique_memory_id="#V#episode_critique_memory_abc",
        target_workflow_id="#V#alpha_workflow",
        candidate_workflow_spec={
            "workflow_id": "#V#alpha_workflow",
            "steps": [{"state_id": "start"}],
        },
        suggestion={
            "suggestion_id": "workflow_change_alpha",
            "target_workflow_id": "#V#alpha_workflow",
        },
        proposal_context={"source": "episode_self_improvement_workflow"},
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        current_depth=1,
    )

    assert result["success"] is True
    assert result["proposal"]["proposal_id"] == "proposal-1"
    assert result["promotion_launch_inputs"]["proposal_id"] == "proposal-1"
    assert result["promotion_launch_inputs"]["episode_evaluation_depth"] == 1
    assert recorded["proposals"][0]["proposal_id"] == "proposal-1"


def test_record_workflow_promotion_evaluation_updates_memory_and_proposal(
    monkeypatch,
) -> None:
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        svc,
        "record_workflow_authoring_promotion_evaluation",
        lambda workflow_id, **kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "proposal": {
                "proposal_id": kwargs["proposal_id"],
                "promotion_evaluation": kwargs["promotion_evaluation"],
            },
        },
    )
    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_self_improvement",
        lambda **kwargs: recorded.update(kwargs) or {"success": True},
    )

    result = svc.record_workflow_promotion_evaluation(
        episode_critique_memory_id="#V#episode_critique_memory_abc",
        target_workflow_id="#V#alpha_workflow",
        proposal_id="proposal-1",
        suggestion={"suggestion_id": "workflow_change_alpha"},
        benchmark_summary={"benchmark_fingerprint": "bench-1"},
        promotion_recommendation="ready_for_review",
        summary="Ready for review",
        reasoning="Candidate validation passed.",
        required_follow_up=["Run reviewer approval"],
        approval_ready=True,
    )

    assert result["success"] is True
    assert result["promotion_evaluation"]["proposal_id"] == "proposal-1"
    assert (
        recorded["promotion_evaluations"][0]["promotion_recommendation"]
        == "ready_for_review"
    )


def test_build_workflow_promotion_context_uses_exact_proposal_id(
    monkeypatch,
) -> None:
    benchmark_args: dict[str, Any] = {}

    monkeypatch.setattr(
        svc,
        "_load_self_improvement_profile",
        lambda **_kwargs: (
            {
                "candidate_selection_policy": {
                    "max_candidate_launches": 3,
                    "priority_order": ["high", "medium", "low"],
                    "eligible_target_surfaces": ["workflow"],
                    "dedupe_identity_fields_by_surface": {
                        "workflow": ["target_workflow_id"]
                    },
                },
                "benchmark_policy": {"scan_limit": 11, "max_audit_cases": 3},
            },
            {},
        ),
    )
    monkeypatch.setattr(
        svc,
        "get_episode_critique_memory_state",
        lambda _memory_id: {
            "memory_id": "#V#episode_critique_memory_abc",
            "namespace": "#V#user@org",
            "improvement_suggestions": [
                {
                    "suggestion_id": "workflow_change_alpha",
                    "target_surface": "workflow",
                    "target_workflow_id": "#V#alpha_workflow",
                }
            ],
        },
    )
    monkeypatch.setattr(
        svc,
        "get_workflow_authoring_proposal",
        lambda _workflow_id: {"proposal_id": "proposal-2", "status": "pending_review"},
    )
    monkeypatch.setattr(
        svc,
        "get_workflow_authoring_proposal_by_id",
        lambda _workflow_id, proposal_id: {
            "proposal_id": proposal_id,
            "status": "superseded",
        },
    )
    monkeypatch.setattr(
        svc,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: ({"phase": "published"}, "vontology"),
    )
    monkeypatch.setattr(
        svc,
        "build_episode_critique_benchmark_report",
        lambda **kwargs: benchmark_args.update(kwargs) or {"benchmark_fingerprint": "bench-1"},
    )

    result = svc.build_workflow_promotion_context(
        episode_critique_memory_id="#V#episode_critique_memory_abc",
        target_workflow_id="#V#alpha_workflow",
        proposal_id="proposal-1",
        suggestion_id="workflow_change_alpha",
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert result["proposal_id"] == "proposal-1"
    assert result["workflow_authoring_proposal"]["proposal_id"] == "proposal-1"
    assert result["workflow_authoring_proposal"]["status"] == "superseded"
    assert benchmark_args["scan_limit"] == 11
    assert benchmark_args["max_audit_cases"] == 3


def test_build_workflow_improvement_context_uses_profile_benchmark_policy(
    monkeypatch,
) -> None:
    benchmark_args: dict[str, Any] = {}

    monkeypatch.setattr(
        svc,
        "_load_self_improvement_profile",
        lambda **_kwargs: (
            {
                "candidate_selection_policy": {
                    "max_candidate_launches": 3,
                    "priority_order": ["high", "medium", "low"],
                    "eligible_target_surfaces": ["workflow"],
                    "dedupe_identity_fields_by_surface": {
                        "workflow": ["target_workflow_id"]
                    },
                },
                "benchmark_policy": {"scan_limit": 17, "max_audit_cases": 2},
            },
            {},
        ),
    )
    monkeypatch.setattr(
        svc,
        "get_episode_critique_memory_state",
        lambda _memory_id: {
            "memory_id": "#V#episode_critique_memory_abc",
            "namespace": "#V#user@org",
            "subject_episode": {"workflow_id": "#V#episode_evaluation_workflow"},
            "improvement_suggestions": [
                {
                    "suggestion_id": "workflow_change_alpha",
                    "target_surface": "workflow",
                    "target_workflow_id": "#V#alpha_workflow",
                }
            ],
        },
    )
    monkeypatch.setattr(svc, "get_workflow_authoring_proposal", lambda _workflow_id: {})
    monkeypatch.setattr(
        svc,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: object(),
    )
    monkeypatch.setattr(
        svc,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: {"workflow_id": "#V#alpha_workflow", "steps": []},
    )
    monkeypatch.setattr(
        svc,
        "build_workflow_definition_identity",
        lambda **_kwargs: {"definition_hash": "hash-1"},
    )
    monkeypatch.setattr(
        svc,
        "build_episode_critique_benchmark_report",
        lambda **kwargs: benchmark_args.update(kwargs)
        or {
            "benchmark_fingerprint": "bench-1",
            "benchmark_signal_summary": {"fail": 1},
        },
    )
    monkeypatch.setattr(
        svc,
        "build_workflow_authoring_prompt_contract",
        lambda: {"schema_version": "workflow_authoring_prompt_contract.v1"},
    )
    monkeypatch.setattr(
        svc,
        "get_workflow_authoring_prompt_health_status",
        lambda: {"healthy": True},
    )

    result = svc.build_workflow_improvement_context(
        episode_critique_memory_id="#V#episode_critique_memory_abc",
        suggestion_id="workflow_change_alpha",
        target_workflow_id="#V#alpha_workflow",
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert benchmark_args["scan_limit"] == 17
    assert benchmark_args["max_audit_cases"] == 2
