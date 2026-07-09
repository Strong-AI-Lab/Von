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
                "summary": {
                    "required_effects_contract_profile_selected_id": "paper",
                    "required_effects_contract_profile_source": "workflow_contract",
                    "workflow_required_effects_contract_id": "conversation_diagnostics",
                    "workflow_required_effects_contract_source": "definition_metadata",
                    "selected_execution_mode": "tool_pipeline",
                    "dispatch_workflow_id": "#V#tool_calling_workflow",
                },
                "required_effects_contract": {
                    "schema_version": "required_effects_contract.v1",
                    "intent_class": "representation",
                    "domain_profile_id": "paper",
                    "artefact_context": {
                        "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                        "urls": [],
                    },
                },
                "workflow_required_effects_contract": {
                    "schema_version": "workflow_required_effects_contract.v1",
                    "contract_id": "conversation_diagnostics",
                },
            },
            "required_effects": [
                {
                    "effect_id": "effect_paper_representation_1",
                    "effect_type": "scholarly_representation",
                    "status": "not_executed",
                    "description": "Represent the corresponding scholarly paper.",
                    "required_tools": [
                        "materialise_scholarly_representation_for_file_copy"
                    ],
                    "targets": ["#V#uploaded_file_copy_abc123"],
                }
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "get_latest_workflow_use_episode",
        lambda **_kwargs: {
            "episode_id": "wfep_1380",
            "workflow_id": "#V#scholarly_paper_representation_workflow",
            "session_id": "session-1380",
            "source": "conversation_turn",
            "status": "failed",
            "final_state": "repair_required",
            "workflow_definition_identity": {
                "definition_hash": "defhash-1380",
            },
        },
    )

    context = service.get_session_workflow_continuation_context(
        session_id="session-1380",
        namespace="#V#user@test_org",
        user_id="#V#user",
    )

    assert context is not None
    assert context["source_request_id"] == "req-1380"
    assert (
        context["selected_workflow_id"] == "#V#scholarly_paper_representation_workflow"
    )
    assert context["requires_follow_up"] is True
    assert context["safe_to_claim_completion"] is False
    assert context["has_unresolved_required_effects"] is True
    assert context["active_workflow_episode_id"] == "wfep_1380"
    assert context["active_workflow_source"] == "conversation_turn"
    assert context["active_workflow_final_state"] == "repair_required"
    assert context["workflow_definition_identity"]["definition_hash"] == "defhash-1380"
    assert context["unresolved_required_effects"][0]["required_tools"] == [
        "materialise_scholarly_representation_for_file_copy"
    ]
    assert context["required_effects_contract"]["domain_profile_id"] == "paper"
    assert context["workflow_required_effects_contract"]["contract_id"] == (
        "conversation_diagnostics"
    )
    assert context["resolved_contract_identifiers"] == {
        "required_effects_contract_profile_selected_id": "paper",
        "required_effects_contract_profile_source": "workflow_contract",
        "workflow_required_effects_contract_id": "conversation_diagnostics",
        "workflow_required_effects_contract_source": "definition_metadata",
        "selected_execution_mode": "tool_pipeline",
        "dispatch_workflow_id": "#V#tool_calling_workflow",
    }


def test_get_session_workflow_continuation_context_keeps_completed_turn_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "classify_workflow_concept_executability",
        lambda _workflow_id: (True, "executable_now", None),
    )
    monkeypatch.setattr(
        service,
        "get_latest_turn_execution_record_projection",
        lambda **_kwargs: {
            "request_id": "req-2571",
            "session_id": "session-2571",
            "workflow_selection": {
                "selected_workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
                "selector_verdict": "rag_selected",
                "selector_source": "workflow_selector",
            },
            "completion_gate": {
                "decision": "completed",
                "requires_follow_up": False,
                "safe_to_claim_completion": True,
            },
            "prompt": {
                "preview": (
                    "Process the Gmail message matching arXiv 2606.30544 and "
                    "answer with represented evidence."
                )
            },
            "completion_report": {
                "workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
                "completed": True,
                "response_text": (
                    "Paper concept: #V#latent_actions_paper.\n"
                    "File copy concept: #V#arxiv_pdf_file_copy_abc123."
                ),
            },
        },
    )
    monkeypatch.setattr(
        service, "get_latest_workflow_use_episode", lambda **_kwargs: None
    )

    context = service.get_session_workflow_continuation_context(
        session_id="session-2571",
        namespace="#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab",
        user_id=None,
    )

    assert context is not None
    assert context["requires_follow_up"] is False
    assert context["has_unresolved_required_effects"] is False
    assert context["selected_workflow_id"] == "#V#zhan_gmail_arxiv_ingestion_workflow"
    assert context["latest_turn_execution_record"]["prompt_preview"].startswith(
        "Process the Gmail message"
    )
    projected = service.project_launch_inputs_from_continuation_context(context)
    assert projected["arxiv_id"] == "2606.30544"
    decision = service.assess_prompt_for_workflow_continuation(
        prompt="try again",
        continuation_context=context,
    )
    assert decision["applies"] is True
    assert decision["reason"] == "prior_workflow_evidence_available"


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


def test_non_executable_selected_workflow_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "classify_workflow_concept_executability",
        lambda _workflow_id: (False, "draft_not_published", "workflow_not_published"),
    )

    monkeypatch.setattr(
        service,
        "get_latest_turn_execution_record_projection",
        lambda **_kwargs: {
            "request_id": "req-1718",
            "workflow_selection": {
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "selector_verdict": "rag_selected",
                "selector_source": "selector",
            },
            "completion_gate": {
                "decision": "follow_up_required",
                "requires_follow_up": True,
                "safe_to_claim_completion": False,
            },
            "required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "status": "not_executed",
                    "description": "Obtain the selected workflow result needed for the user-facing answer.",
                }
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "get_latest_workflow_use_episode",
        lambda **_kwargs: {
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "session_id": "session-1718",
        },
    )

    context = service.get_session_workflow_continuation_context(
        session_id="session-1718",
        namespace="#V#user@test_org",
        user_id="#V#user",
    )

    assert context is not None
    assert context["selected_workflow_is_executable"] is False
    assert context["selected_workflow_executability_reason"] == "draft_not_published"

    decision = service.assess_prompt_for_workflow_continuation(
        prompt="Represent this paper https://arxiv.org/abs/2603.01896",
        continuation_context=context,
    )

    assert decision["applies"] is False
    assert decision["reason"] == "selected_workflow_not_executable"
    assert decision["decision_source"] == "workflow_state"
    assert decision["selected_workflow_executability_reason"] == "draft_not_published"


def test_divergence_phrasing_no_longer_decided_by_python_regexes() -> None:
    """Prompt-level divergence is a represented-selector judgement
    (JVNAUTOSCI-2500): continuation context still applies on workflow state,
    regardless of how the user phrases rejection, and the selector decides
    with the framing plus the user's message in context."""

    decision = service.assess_prompt_for_workflow_continuation(
        prompt=(
            "The enrichment workflow isn't the right one. Manually retrieve "
            "#V#timothy_pistotti and inspect the concept. Do not run an "
            "existing workflow."
        ),
        continuation_context={
            "requires_follow_up": True,
            "has_unresolved_required_effects": True,
            "selected_workflow_id": "#V#enrichment_workflow",
            "unresolved_required_effects": [
                {"effect_type": "representation_verification"}
            ],
        },
    )

    assert decision["applies"] is True
    assert decision["reason"] == "workflow_state_authoritative"
    assert decision["decision_source"] == "workflow_state"


def test_launch_projection_gated_on_matching_selected_workflow() -> None:
    """A divergent selection must not inherit stale continuation inputs."""

    continuation_context = {
        "requires_follow_up": True,
        "has_unresolved_required_effects": True,
        "selected_workflow_id": "#V#enrichment_workflow",
        "unresolved_required_effects": [
            {
                "effect_type": "representation_verification",
                "status": "not_executed",
                "targets": ["#V#timothy_pistotti"],
            }
        ],
    }

    matching = service.project_launch_inputs_from_continuation_context(
        continuation_context,
        selected_workflow_id="#V#enrichment_workflow",
    )
    assert matching.get("concept_ids") == ["#V#timothy_pistotti"]

    divergent = service.project_launch_inputs_from_continuation_context(
        continuation_context,
        selected_workflow_id="#V#general_tool_calling_workflow",
    )
    assert divergent == {}

    unconstrained = service.project_launch_inputs_from_continuation_context(
        continuation_context,
    )
    assert unconstrained.get("concept_ids") == ["#V#timothy_pistotti"]


def test_framing_instruction_fails_closed_to_facts_only_summary(monkeypatch) -> None:
    """Without the represented framing prompt, the system message contains
    only the factual summary, never a Python-authored instruction."""

    monkeypatch.setattr(
        service,
        "resolve_workflow_continuation_framing_instruction",
        lambda **_kwargs: (None, {"error": "missing"}),
    )
    message = service.build_workflow_continuation_system_message(
        {
            "session_id": "session-1",
            "selected_workflow_id": "#V#enrichment_workflow",
        }
    )
    assert message is not None
    assert message.startswith("ACTIVE WORKFLOW CONTINUATION CONTEXT")

    monkeypatch.setattr(
        service,
        "resolve_workflow_continuation_framing_instruction",
        lambda **_kwargs: (
            "Authored framing instruction with divergence licence.",
            {"loaded_prompt_concept_id": "#V#workflow_continuation_framing_prompt"},
        ),
    )
    message = service.build_workflow_continuation_system_message(
        {
            "session_id": "session-1",
            "selected_workflow_id": "#V#enrichment_workflow",
        }
    )
    assert message is not None
    assert message.startswith("Authored framing instruction")
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in message


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


def test_completed_prior_workflow_evidence_can_apply_without_open_work() -> None:
    context = {
        "requires_follow_up": False,
        "has_unresolved_required_effects": False,
        "selected_workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
        "latest_workflow_episode": {
            "episode_id": "wfep-2571",
            "completed": True,
            "metadata": {
                "result_snapshot": {
                    "arxiv_id": "2606.30544",
                    "paper_concept_id": "#V#latent_actions_paper",
                    "file_copy_concept_id": "#V#arxiv_pdf_file_copy_abc123",
                    "processed_message_id": "19f3b77986f71a4d",
                    "source_processing_marker": (
                        "#V#source_processing_marker_gmail_vonwitbrock_gmail_abc123"
                    ),
                }
            },
        },
    }

    decision = service.assess_prompt_for_workflow_continuation(
        prompt="try again",
        continuation_context=context,
    )
    projected = service.project_launch_inputs_from_continuation_context(
        context,
        selected_workflow_id="#V#zhan_gmail_arxiv_ingestion_workflow",
    )
    summary = service.build_workflow_continuation_summary_text(context)

    assert decision["applies"] is True
    assert decision["reason"] == "prior_workflow_evidence_available"
    assert decision["decision_source"] == "workflow_state"
    assert projected["arxiv_id"] == "2606.30544"
    assert projected["paper_concept_id"] == "#V#latent_actions_paper"
    assert projected["file_copy_concept_id"] == "#V#arxiv_pdf_file_copy_abc123"
    assert projected["source_item_id"] == "19f3b77986f71a4d"
    assert projected["message_id"] == "19f3b77986f71a4d"
    assert projected["source_processing_marker"].startswith(
        "#V#source_processing_marker_gmail"
    )
    assert "Prior workflow evidence available: yes" in summary
    assert "- arxiv_id: 2606.30544" in summary


def test_build_workflow_continuation_routing_prompt_summarises_targets_and_tools() -> (
    None
):
    prompt = service.build_workflow_continuation_routing_prompt(
        prompt="Please proceed.",
        continuation_context={
            "session_id": "session-1380",
            "active_workflow_episode_id": "wfep_1380",
            "active_workflow_source": "conversation_turn",
            "workflow_definition_identity": {"definition_hash": "defhash-1380"},
            "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
            "completion_gate_decision": "escalation_required",
            "completion_gate_decision_reason": "Representation was not executed.",
            "unresolved_required_effects": [
                {
                    "effect_type": "scholarly_representation",
                    "targets": ["#V#uploaded_file_copy_abc123"],
                    "required_tools": [
                        "materialise_scholarly_representation_for_file_copy"
                    ],
                    "description": "Represent the corresponding scholarly paper.",
                }
            ],
            "required_effects_contract": {
                "artefact_context": {
                    "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                    "urls": ["https://arxiv.org/abs/2502.14996"],
                }
            },
            "workflow_required_effects_contract": {
                "contract_id": "conversation_diagnostics"
            },
            "resolved_contract_identifiers": {
                "workflow_required_effects_contract_id": "conversation_diagnostics",
                "required_effects_contract_profile_selected_id": "paper",
                "selected_execution_mode": "tool_pipeline",
            },
        },
    )

    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in prompt
    assert "Active workflow episode: wfep_1380" in prompt
    assert "Active workflow source: conversation_turn" in prompt
    assert (
        "Selected workflow / episode workflow: "
        "#V#scholarly_paper_representation_workflow"
    ) in prompt
    assert "Prior completion gate reason: Representation was not executed." in prompt
    assert "Workflow definition identity: defhash-1380" in prompt
    assert (
        "required_tools: materialise_scholarly_representation_for_file_copy" in prompt
    )
    assert "Workflow required-evidence contract: conversation_diagnostics" in prompt
    assert "required_effects_contract_profile_selected_id: paper" in prompt
    assert "Artefact file_copy_ids: #V#uploaded_file_copy_abc123" in prompt
    assert "Artefact urls: https://arxiv.org/abs/2502.14996" in prompt
    assert prompt.rstrip().endswith("Please proceed.")


def test_extract_url_targets_from_continuation_context_reads_contract_and_targets() -> (
    None
):
    urls = service.extract_url_targets_from_continuation_context(
        {
            "unresolved_required_effects": [
                {
                    "targets": [
                        "https://arxiv.org/abs/2502.14996",
                        "#V#uploaded_file_copy_abc123",
                    ]
                }
            ],
            "required_effects_contract": {
                "artefact_context": {
                    "urls": [
                        "https://arxiv.org/abs/2502.14996",
                        "https://example.org/papers/2502.14996",
                    ]
                }
            },
        }
    )

    assert urls == [
        "https://arxiv.org/abs/2502.14996",
        "https://example.org/papers/2502.14996",
    ]


def test_project_launch_inputs_from_continuation_context_projects_stable_artefacts() -> (
    None
):
    projected = service.project_launch_inputs_from_continuation_context(
        {
            "unresolved_required_effects": [
                {
                    "targets": [
                        "#V#uploaded_file_copy_abc123",
                        "https://arxiv.org/abs/2502.14996",
                    ]
                }
            ],
            "required_effects_contract": {
                "artefact_context": {
                    "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                    "urls": ["https://arxiv.org/abs/2502.14996"],
                }
            },
        }
    )

    assert projected == {
        "file_copy_concept_ids": ["#V#uploaded_file_copy_abc123"],
        "file_copy_concept_id": "#V#uploaded_file_copy_abc123",
        "concept_ids": ["#V#uploaded_file_copy_abc123"],
        "source_uris": ["https://arxiv.org/abs/2502.14996"],
        "source_uri": "https://arxiv.org/abs/2502.14996",
        "arxiv_ids": ["2502.14996"],
        "arxiv_id": "2502.14996",
    }


def test_project_launch_inputs_from_continuation_context_preserves_plural_targets() -> (
    None
):
    projected = service.project_launch_inputs_from_continuation_context(
        {
            "unresolved_required_effects": [
                {
                    "targets": [
                        "#V#paper_alpha",
                        "#V#paper_beta",
                        "https://arxiv.org/abs/2502.14996",
                        "https://arxiv.org/abs/2502.14997",
                    ]
                }
            ],
            "required_effects_contract": {
                "artefact_context": {
                    "urls": [
                        "https://arxiv.org/abs/2502.14996",
                        "https://arxiv.org/abs/2502.14997",
                    ]
                }
            },
        }
    )

    assert projected == {
        "concept_ids": ["#V#paper_alpha", "#V#paper_beta"],
        "source_uris": [
            "https://arxiv.org/abs/2502.14996",
            "https://arxiv.org/abs/2502.14997",
        ],
        "arxiv_ids": ["2502.14996", "2502.14997"],
    }
