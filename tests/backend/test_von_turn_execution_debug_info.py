from __future__ import annotations


def test_finalise_llm_debug_info_records_observations_without_rebuilding_a_gate(
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

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-adaptive-observation",
            "interaction_timestamp_utc": "2026-07-26T00:00:00+00:00",
            "response": "A useful answer.",
            "llm_interaction": {
                "ordinary_turn_terminal_status": "completed",
                "calls": [
                    {
                        "type": "adaptive_turn_model_call",
                        "status": "completed",
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
    )

    record = result["turn_execution_record"]
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
    assert record["evidence_index"] == [evidence]
    assert "completion_gate" not in record
    assert "required_effects" not in record
    assert "required_tool_obligation_ledger" not in record


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
