from __future__ import annotations

from src.backend.services.chat_auxiliary_prompt_service import (
    build_applied_prompt_snapshot,
)


def test_finalise_llm_debug_info_records_observations_without_rebuilding_a_gate(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes
    from src.backend.services import speech_telemetry_service

    client_context = {"device_model": "Pixel 8", "speech_attempt_ids": ["speech-attempt"]}
    monkeypatch.setattr(speech_telemetry_service, "request_client_context", lambda: client_context)

    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )
    monkeypatch.setattr(
        von_routes,
        "get_model_registry_snapshot",
        lambda: {
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "test-v1",
                        "source": "test",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "rates": {
                            "input_tokens": 1.0,
                            "output_tokens": 2.0,
                        },
                    },
                }
            ]
        },
    )
    evidence = {
        "evidence_id": "evidence_opaque",
        "tool_name": "general_read",
        "status": "ok",
        "preview": '{"answer":"found"}',
        "preview_truncated": False,
        "sha256": "abc123",
    }
    applied_prompt_snapshot = build_applied_prompt_snapshot(
        user_concept_id="#V#person",
        namespace="#V#person@org",
        organisation_concept_id="#V#org",
        turn_id="req-adaptive-observation",
        behaviour_fragments=[
            {"concept_id": "#V#applied_prompt", "content": "Be precise."}
        ],
        narration_fragments=[],
        screen_fragments=[],
    )

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-adaptive-observation",
            "interaction_timestamp_utc": "2026-07-26T00:00:00+00:00",
            "response": "A useful answer.",
            "llm_interaction": {
                "ordinary_turn_terminal_status": "completed",
                "calls": [
                    {
                        "call_id": "req-adaptive-observation:llm:1",
                        "type": "adaptive_turn_model_call",
                        "status": "completed",
                        "provider": "openai",
                        "requested_model": "gpt-test",
                            "selected_model": "gpt-test",
                            "effective_model": "gpt-test",
                            "model_identity_source": "provider_response",
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                        },
                    }
                ],
            },
            "tool_invocations": [
                {
                    "tool": "general_read",
                    "status": "ok",
                    "evidence": evidence,
                }
            ],
            "turn_execution_record_tool_invocations": [
                {
                    "tool": "general_read",
                    "status": "ok",
                    "evidence": evidence,
                }
            ],
            "search_evidence": [],
            "turn_execution_diagnostics": {
                "execution_path": "direct_adaptive_turn"
            },
            "aux_llm_calls": [
                {
                    "type": "adaptive_turn_learning_advice_exposure",
                    "schema_version": (
                        "adaptive_capability_learning_advice_exposure.v1"
                    ),
                    "consumer": "direct_adaptive_turn",
                    "decision_kind": "capability_choice",
                    "arm": "B",
                    "source": "simple_sidecar",
                    "status": "exposed",
                    "experiment_id": "experiment-one",
                    "case_id": "case-one",
                    "candidate_id": "#V#candidate_one",
                    "candidate_revision": 2,
                    "candidate_body_sha256": "body-digest",
                    "candidate_revision_identity_sha256": "revision-digest",
                    "candidate_source_locator_sha256": "source-digest",
                    "projection_sha256": "projection-digest",
                    "model_visible": True,
                    "model_visible_call_ids": [
                        "req-adaptive-observation:llm:1"
                    ],
                    "dispositions": [
                        {
                            "capability_call_id": "tool-call-one",
                            "capability_name": "general_read",
                            "disposition": "adapted",
                        }
                    ],
                    "terminal_status": "completed",
                    "private_body": "must not enter the turn record",
                },
                {
                    "type": "adaptive_turn_evidence_index",
                    "evidence": [evidence],
                }
            ],
        },
        prompt_text="Use whichever delegated read is helpful.",
        response_text="A useful answer.",
        session_id="session-adaptive-observation",
        namespace="#V#person@org",
        user_id="#V#person",
        org_id="#V#org",
        workflow_discovery=None,
        workflow_routing=None,
        applied_prompt_snapshot=applied_prompt_snapshot,
    )

    record = result["turn_execution_record"]
    assert result["client_context"] == client_context
    assert result["turn_execution_diagnostics"]["client_context"] == client_context
    assert record["turn_execution_diagnostics"]["client_context"] == client_context
    assert record["schema_version"] == "turn_execution_record.observational.v1"
    assert record["record_kind"] == "observational"
    assert record["actor"] == {
        "actor_concept_id": "#V#person",
        "user_concept_id": "#V#person",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#person@org",
    }
    assert record["terminal_status"] == "completed"
    assert record["response"]["present"] is True
    assert record["applied_prompt_snapshot"] == applied_prompt_snapshot
    assert record["evidence_index"] == [evidence]
    assert "learning_advice_exposures" not in record
    summary = result["llm_usage_cost_summary"]
    assert summary["usage"]["total_tokens"] == 120
    assert summary["estimated_cost"]["status"] == "estimated"
    assert summary["estimated_cost"]["amount"] == 0.14
    assert record["llm_usage_cost_summary"] == summary
    assert "completion_gate" not in record
    assert "required_effects" not in record
    assert "required_tool_obligation_ledger" not in record


def test_generate_error_debug_preserves_paid_calls_before_route_failure(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes
    from src.backend.server.routes.generate_route_support import (
        _build_generate_error_debug_info,
    )

    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )
    monkeypatch.setattr(
        von_routes,
        "get_model_registry_snapshot",
        lambda: {
            "models": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "pricing": {
                        "schema_version": "llm_model_pricing.v1",
                        "version": "test-v1",
                        "source": "test",
                        "effective_at_utc": "2026-08-05T00:00:00Z",
                        "model_id": "gpt-test",
                        "currency": "USD",
                        "unit_tokens": 1_000,
                        "rates": {
                            "input_tokens": 1.0,
                            "output_tokens": 2.0,
                        },
                    },
                }
            ]
        },
    )

    result = _build_generate_error_debug_info(
        interaction_timestamp_utc="2026-08-05T10:00:00Z",
        request_id="request-paid-then-failed",
        model_name="gpt-test",
        prompt_text="Do useful work.",
        error_text="route finalisation failed",
        enhanced_context=[],
        user_prompt_debug=None,
        response_transformations=None,
        error_tool_progress_snapshot=None,
        error_workflow_discovery=None,
        error_workflow_routing=None,
        auxiliary_llm_calls=[],
        llm_interaction={
            "ordinary_turn_terminal_status": "model_error",
            "calls": [
                {
                    "call_id": "request-paid-then-failed:llm:1",
                        "provider": "openai",
                        "effective_model": "gpt-test",
                        "model_identity_source": "provider_response",
                        "provider_request_sent": True,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                }
            ],
        },
        error_elapsed_ms=100.0,
        session_id="session-test",
        namespace="#V#person@org",
        history_user_id="#V#person",
        org_concept_id="#V#org",
        calculate_context_stats_fn=lambda _context: {},
        build_response_transformation_telemetry_payload_fn=lambda **_kwargs: {},
        build_turn_execution_diagnostics_fn=lambda **_kwargs: {},
        finalise_llm_debug_info_fn=von_routes._finalise_llm_debug_info,
    )

    assert result["llm_usage_cost_summary"]["usage"]["total_tokens"] == 120
    assert result["llm_usage_cost_summary"]["estimated_cost"]["amount"] == 0.14
    assert (
        result["turn_execution_record"]["llm_usage_cost_summary"]
        == result["llm_usage_cost_summary"]
    )
    capsule = result["turn_failure_capsule"]
    assert capsule["terminal_status"] == "model_error"
    assert capsule["response_authority"] == "not_recorded"
    assert capsule["effects"] == []
    assert capsule["turn_error"]["preview"]["text"] == (
        "route finalisation failed"
    )


def test_finalise_persists_inline_turn_failure_capsule_from_outcome_report(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    version = {
        "version": "v20260818_backend+gabc123",
        "git_commit": "abc123abc123abc123abc123abc123abc123abcd",
    }
    monkeypatch.setattr(von_routes, "get_runtime_code_version_info", lambda: version)
    monkeypatch.setattr(von_routes, "get_model_registry_snapshot", lambda: None)
    outcome_report = {
        "type": "adaptive_turn_effect_outcome_report",
        "schema_version": "adaptive_turn_effect_outcome_report.v1",
        "terminal_status": "effect_partially_completed",
        "response_authority": "canonical_outcome",
        "model_draft": {
            "authority": "non_authoritative",
            "preview": (
                "I represented it in #V#person@org for session-paper-lock."
            ),
        },
        "canonical_scopes": [{"mode": "user", "concept_id": "#V#person"}],
        "facts": [
            {
                "effect_id": "effect-download",
                "tool": "download_paper",
                "effect_status": "failed",
                "initial_effect_status": "failed",
                "changed": False,
                "outcome_resolved": False,
                "canonical_readback_present": False,
                "error_code": "arxiv_acquisition_unavailable",
                "error": (
                    "RuntimeError: asyncio lock is bound to a different event loop"
                ),
            },
            {
                "effect_id": "effect-workflow-instance",
                "tool": "Scholarly Article Metadata Representation Workflow",
                "effect_status": "failed",
                "initial_effect_status": "failed",
                "changed": True,
                "current_outcome_status": "failed",
                "outcome_resolved": True,
                "reconciliation_status": "canonically_verified",
                "canonical_readback_present": True,
                "canonical_readback_verified": False,
                "workflow_instance_readback_verified": True,
                "workflow_id": (
                    "#V#scholarly_article_metadata_representation_workflow"
                ),
                "instance_id": "workflow-instance-failed-1",
                "evidence_id": "evidence-workflow-instance-readback",
            },
        ],
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-paper-lock",
            "interaction_timestamp_utc": "2026-08-18T00:00:00Z",
            "response": (
                "User scope #V#person in session-paper-lock; "
                "the paper download failed."
            ),
            "llm_interaction": {
                "calls": [],
            },
            "tool_invocations": [],
            "turn_execution_record_tool_invocations": [],
            "turn_execution_diagnostics": {},
            "aux_llm_calls": [outcome_report],
        },
        prompt_text="Represent the paper.",
        response_text=(
            "User scope #V#person in session-paper-lock; "
            "the paper download failed."
        ),
        session_id="session-paper-lock",
        namespace="#V#person@org",
        user_id="#V#person",
        org_id="#V#org",
    )

    capsule = result["turn_failure_capsule"]
    assert capsule["schema_version"] == "turn_failure_capsule.v1"
    assert capsule["request_id"] == "req-paper-lock"
    assert capsule["terminal_status"] == "effect_partially_completed"
    assert capsule["response_authority"] == "canonical_outcome"
    assert capsule["producer"] == {
        "code_version": version["version"],
        "git_commit": version["git_commit"],
    }
    assert capsule["effects"][0]["error"]["code"] == (
        "arxiv_acquisition_unavailable"
    )
    workflow_effect = next(
        effect
        for effect in capsule["effects"]
        if effect["effect_id"] == "effect-workflow-instance"
    )
    assert workflow_effect["canonical_readback_verdict"] == (
        "workflow_instance_verified"
    )
    assert workflow_effect["instance_id"] == "workflow-instance-failed-1"
    assert workflow_effect["evidence_id"] == (
        "evidence-workflow-instance-readback"
    )
    assert capsule["pre_presentation_draft"]["authority"] == (
        "non_authoritative"
    )
    assert "#V#person" not in capsule["visible_response"]["text"]
    assert "session-paper-lock" not in capsule["visible_response"]["text"]
    assert "session-paper-lock" not in capsule["pre_presentation_draft"]["text"]
    assert result["code_version_details"] == version


def test_finalise_reuses_turn_pricing_snapshot_summary(monkeypatch) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )

    def fail_if_registry_is_read_again():
        raise AssertionError("turn finalisation must reuse the supplied summary")

    monkeypatch.setattr(
        von_routes,
        "get_model_registry_snapshot",
        fail_if_registry_is_read_again,
    )
    supplied_summary = {
        "schema_version": "llm_usage_cost_summary.v1",
        "call_count": 1,
        "unique_call_count": 1,
        "duplicate_call_count": 0,
        "billable_call_count": 1,
        "usage": {"status": "reported", "total_tokens": 120},
        "estimated_cost": {
            "status": "estimated",
            "amount": 0.14,
            "known_amount": 0.14,
            "currency": "USD",
        },
        "model_identities": [],
        "model_identity_omitted_count": 0,
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "reuse-summary",
            "interaction_timestamp_utc": "2026-08-05T10:00:00Z",
            "response": "Done.",
            "llm_interaction": {
                "ordinary_turn_terminal_status": "completed",
                "calls": [],
            },
            "llm_usage_cost_summary": supplied_summary,
            "tool_invocations": [],
            "turn_execution_record_tool_invocations": [],
            "search_evidence": [],
            "turn_execution_diagnostics": {},
            "aux_llm_calls": [],
        },
        prompt_text="Do the task.",
        response_text="Done.",
        session_id="session-reuse-summary",
        namespace="#V#person@org",
        user_id="#V#person",
        org_id="#V#org",
    )

    assert result["llm_usage_cost_summary"] == supplied_summary
    assert result["turn_execution_record"]["llm_usage_cost_summary"] == supplied_summary
    capsule = result["turn_failure_capsule"]
    assert capsule["terminal_status"] == "completed"
    assert capsule["response_authority"] == "not_recorded"
    assert capsule["visible_response"]["text"] == "Done."
    assert capsule["effects"] == []
    assert "turn_error" not in capsule


def test_finalise_llm_debug_info_preserves_bounded_evidence_envelope(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )
    evidence = {
        "evidence_id": "evidence_opaque",
        "tool_name": "general_read",
        "status": "ok",
        "preview": '{"answer":"found"}',
        "preview_truncated": False,
        "sha256": "abc123",
    }
    bounded_invocation = {
        "tool": "general_read",
        "status": "ok",
        "evidence": evidence,
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-rich-evidence",
            "interaction_timestamp_utc": "2026-07-26T00:00:00+00:00",
            "response": "Found.",
            "llm_interaction": {
                "ordinary_turn_terminal_status": "completed",
                "calls": [],
            },
            "tool_invocations": [
                {"tool": "general_read", "status": "ok"},
            ],
            "turn_execution_record_tool_invocations": [bounded_invocation],
            "search_evidence": [],
            "turn_execution_diagnostics": {},
            "aux_llm_calls": [],
        },
        prompt_text="Read the evidence.",
        response_text="Found.",
        session_id="session-rich-evidence",
        namespace="#V#person@org",
        user_id="#V#person",
        org_id="#V#org",
        workflow_discovery=None,
        workflow_routing=None,
    )

    assert result["turn_execution_record"]["tool_invocations"] == [
        bounded_invocation
    ]


def test_effect_status_and_evidence_survive_bounded_serialisation() -> None:
    from src.backend.server.routes import von_routes

    evidence = {
        "evidence_id": "ev_effect",
        "preview": '{"partial_failures":[{"stage":"inverse_relationship"}]}',
        "sha256": "abc123",
    }
    invocation = {
        "tool": "add_relationship",
        "status": "ok",
        "effect_id": "effect_opaque",
        "effect_status": "partial",
        "changed": True,
        "evidence": evidence,
    }

    assert von_routes._serialise_tool_invocations_for_llm_debug([invocation])[0] == {
        "tool": "add_relationship",
        "method": "add_relationship",
        "arguments": {},
        "status": "ok",
        "effect_id": "effect_opaque",
        "effect_status": "partial",
        "changed": True,
    }
    assert von_routes._serialise_tool_invocations_for_turn_execution_record(
        [invocation]
    )[0] == {
        "tool": "add_relationship",
        "method": "add_relationship",
        "status": "ok",
        "effect_id": "effect_opaque",
        "effect_status": "partial",
        "changed": True,
        "evidence": evidence,
    }


def test_created_concept_label_extractor_ignores_existing_concept_results() -> None:
    from src.backend.server.routes import von_routes

    labels = von_routes._extract_created_concept_labels_from_payload(
        {
            "results": [
                {
                    "success": False,
                    "requested_name": "Tool Calling Workflow",
                    "existing_concept_id": "#V#tool_calling_workflow",
                    "error_code": "already_exists",
                    "duplicate_prevented": True,
                }
            ],
            "created_concept_ids": [],
        }
    )

    assert labels == []


def test_created_concept_label_extractor_prefers_named_created_ids() -> None:
    from src.backend.server.routes import von_routes

    labels = von_routes._extract_created_concept_labels_from_payload(
        {
            "created_concept_ids": ["#V#new_review_workflow"],
            "results": [
                {
                    "success": True,
                    "requested_name": "New Review Workflow",
                    "concept_id": "#V#new_review_workflow",
                }
            ],
        }
    )

    assert labels == ["New Review Workflow (#V#new_review_workflow)"]
