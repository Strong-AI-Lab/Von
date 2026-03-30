from src.backend.services import workflow_continuation_service as service


def test_get_session_workflow_continuation_context_uses_latest_record_and_episode(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_latest_turn_execution_record_projection",
        lambda **_kwargs: {
            "request_id": "req-1380",
            "workflow_selection": {
                "selected_workflow_id": None,
                "selector_verdict": "fallback",
                "selector_source": "default",
            },
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Representation was not executed.",
                "requires_follow_up": True,
                "safe_to_claim_completion": False,
            },
            "execution": {
                "required_effects_contract": {
                    "schema_version": "required_effects_contract.v1",
                    "intent_class": "representation",
                    "domain_profile_id": "paper",
                    "artefact_context": {
                        "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                        "urls": [],
                    },
                }
            },
            "required_effects": [
                {
                    "effect_id": "effect_paper_representation_1",
                    "effect_type": "scholarly_representation",
                    "status": "not_executed",
                    "description": "Represent the corresponding scholarly paper.",
                    "required_tools": ["interpret_file_copy"],
                    "targets": ["#V#uploaded_file_copy_abc123"],
                }
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "get_latest_workflow_use_episode",
        lambda **_kwargs: {
            "workflow_id": "#V#scholarly_paper_representation_workflow",
            "session_id": "session-1380",
        },
    )

    context = service.get_session_workflow_continuation_context(
        session_id="session-1380",
        namespace="#V#user@test_org",
        user_id="#V#user",
    )

    assert context is not None
    assert context["source_request_id"] == "req-1380"
    assert context["selected_workflow_id"] == "#V#scholarly_paper_representation_workflow"
    assert context["requires_follow_up"] is True
    assert context["safe_to_claim_completion"] is False
    assert context["has_unresolved_required_effects"] is True
    assert context["unresolved_required_effects"][0]["required_tools"] == [
        "interpret_file_copy"
    ]
    assert context["required_effects_contract"]["domain_profile_id"] == "paper"


def test_assess_prompt_for_workflow_continuation_requires_selected_workflow() -> None:
    decision = service.assess_prompt_for_workflow_continuation(
        prompt="You did not create this concept at all.",
        continuation_context={
            "requires_follow_up": True,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {"effect_type": "scholarly_representation"}
            ],
            # No selected_workflow_id -> workflow state alone is insufficient.
        },
    )

    assert decision["applies"] is False
    assert decision["reason"] == "open_work_without_selected_workflow"
    assert decision["decision_source"] == "workflow_state"


def test_workflow_state_authoritative_overrides_prompt_shape() -> None:
    """When continuation context has open work + a specific workflow_id,
    continuation applies regardless of prompt content."""
    decision = service.assess_prompt_for_workflow_continuation(
        prompt="Tell me about quantum physics.",
        continuation_context={
            "requires_follow_up": True,
            "has_unresolved_required_effects": True,
            "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
            "unresolved_required_effects": [
                {"effect_type": "scholarly_representation"}
            ],
        },
    )

    assert decision["applies"] is True
    assert decision["reason"] == "workflow_state_authoritative"
    assert decision["decision_source"] == "workflow_state"


def test_narrowed_heuristic_no_longer_matches_bare_yet() -> None:
    """Prompt wording alone should not trigger continuation without workflow state."""
    decision = service.assess_prompt_for_workflow_continuation(
        prompt="I haven't looked at this yet but I'll try later.",
        continuation_context={
            "requires_follow_up": True,
            "has_unresolved_required_effects": False,
        },
    )

    assert decision["applies"] is False
    assert decision["reason"] == "open_work_without_selected_workflow"
    assert decision["decision_source"] == "workflow_state"


def test_narrowed_heuristic_no_longer_matches_bare_check() -> None:
    """Prompt wording alone should not trigger continuation without workflow state."""
    decision = service.assess_prompt_for_workflow_continuation(
        prompt="Check out this new paper I found.",
        continuation_context={
            "requires_follow_up": True,
            "has_unresolved_required_effects": False,
        },
    )

    assert decision["applies"] is False
    assert decision["reason"] == "open_work_without_selected_workflow"
    assert decision["decision_source"] == "workflow_state"


def test_no_open_work_returns_workflow_state_decision_source() -> None:
    """When there's no open work, the decision is workflow-state based."""
    decision = service.assess_prompt_for_workflow_continuation(
        prompt="proceed",
        continuation_context={
            "requires_follow_up": False,
            "has_unresolved_required_effects": False,
        },
    )

    assert decision["applies"] is False
    assert decision["reason"] == "no_open_work"
    assert decision["decision_source"] == "workflow_state"


def test_build_workflow_continuation_routing_prompt_summarises_targets_and_tools() -> None:
    prompt = service.build_workflow_continuation_routing_prompt(
        prompt="Please proceed.",
        continuation_context={
            "session_id": "session-1380",
            "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
            "completion_gate_decision": "escalation_required",
            "unresolved_required_effects": [
                {
                    "effect_type": "scholarly_representation",
                    "targets": ["#V#uploaded_file_copy_abc123"],
                    "required_tools": ["interpret_file_copy"],
                    "description": "Represent the corresponding scholarly paper.",
                }
            ],
            "required_effects_contract": {
                "artefact_context": {
                    "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                    "urls": ["https://arxiv.org/abs/2502.14996"],
                }
            },
        },
    )

    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in prompt
    assert "Selected workflow: #V#scholarly_paper_representation_workflow" in prompt
    assert "required_tools: interpret_file_copy" in prompt
    assert "Artefact file_copy_ids: #V#uploaded_file_copy_abc123" in prompt
    assert "Artefact urls: https://arxiv.org/abs/2502.14996" in prompt
    assert prompt.rstrip().endswith("Please proceed.")
