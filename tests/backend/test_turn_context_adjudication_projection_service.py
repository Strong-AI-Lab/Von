from src.backend.services.turn_context_adjudication_projection_service import (
    build_turn_context_adjudication_projection,
)


def test_context_adjudication_projection_copies_represented_handoff_fields() -> None:
    projection = build_turn_context_adjudication_projection(
        (
            (
                "workflow_context",
                {
                    "turn_context_handoff_decision": {
                        "mode": "no_prior_context",
                        "summary": "Use only the current Gmail-token request.",
                        "routing_evidence_scope": "current_request_only",
                    },
                    "turn_context_handoff_mode": "no_prior_context",
                    "turn_context_handoff_summary": (
                        "Use only the current Gmail-token request."
                    ),
                    "turn_context_handoff_messages": [],
                    "turn_context_handoff_lineage": ["history_index:4"],
                    "turn_context_handoff_risks": ["older paper topic omitted"],
                    "llm_step_envelope": {
                        "selected_prompt_id": "#V#turn_prompt_context_adjudication_prompt",
                        "selected_model": "ollama/qwen3:8b",
                        "selected_model_candidate": {
                            "provider": "ollama",
                            "locality": "local",
                        },
                    },
                },
            ),
        )
    )

    assert projection is not None
    assert projection["schema_version"] == "turn_context_adjudication_projection.v1"
    assert projection["source"] == "workflow_context"
    assert projection["mode"] == "no_prior_context"
    assert projection["summary"] == "Use only the current Gmail-token request."
    assert projection["routing_evidence_scope"] == "current_request_only"
    assert projection["lineage"] == ["history_index:4"]
    assert projection["risks"] == ["older paper topic omitted"]
    assert projection["prompt_id"] == "#V#turn_prompt_context_adjudication_prompt"
    assert projection["model"] == "ollama/qwen3:8b"
    assert projection["model_policy"] == {"provider": "ollama", "locality": "local"}


def test_context_adjudication_projection_returns_none_without_recorded_handoff() -> None:
    assert (
        build_turn_context_adjudication_projection(
            (("workflow_context", {"user_prompt": "Hello"}),)
        )
        is None
    )


def test_context_adjudication_projection_reads_validated_progress_stage() -> None:
    projection = build_turn_context_adjudication_projection(
        (
            (
                "turn_progress",
                {
                    "progress_history": [
                        {
                            "stage": "context_adjudication",
                            "validated_json": {
                                "mode": "no_prior_context",
                                "summary": "Ignore unrelated previous turns.",
                                "routing_evidence_scope": "current_request_only",
                                "expected_outcome_scope": "current_request_only",
                                "answer_scope": "current_request_only",
                                "turn_context_handoff_messages": [],
                                "lineage": [],
                                "omitted_context_reasons": [
                                    "Earlier topic would contaminate routing."
                                ],
                                "risks": [],
                            },
                            "llm_step_envelope": {
                                "resolved_prompt_concept_id": (
                                    "#V#turn_prompt_context_adjudication_prompt"
                                ),
                                "model": "qwen3:8b",
                            },
                        }
                    ]
                },
            ),
        )
    )

    assert projection is not None
    assert projection["source"] == "turn_progress"
    assert projection["mode"] == "no_prior_context"
    assert projection["summary"] == "Ignore unrelated previous turns."
    assert projection["routing_evidence_scope"] == "current_request_only"
    assert projection["prompt_id"] == "#V#turn_prompt_context_adjudication_prompt"
    assert projection["model"] == "qwen3:8b"
