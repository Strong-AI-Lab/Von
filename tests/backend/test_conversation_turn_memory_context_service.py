from __future__ import annotations

import src.backend.services.conversation_turn_memory_context_service as memory_service


def test_build_turn_memory_context_state_uses_available_subject_memory(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        memory_service,
        "resolve_effective_context",
        lambda **_kwargs: {
            "success": True,
            "effective_context_bundle_ids": ["#V#bundle_user_memory"],
            "effective_context_facet_ids": ["#V#facet_identity"],
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(
        memory_service,
        "list_attached_context_dossier_ids",
        lambda subject_id: (
            ["#V#user_turn_dossier"] if subject_id == "#V#test_user" else []
        ),
    )
    monkeypatch.setattr(
        memory_service,
        "load_context_dossier_state",
        lambda dossier_id: (
            {
                "subject_id": "#V#test_user",
                "subject_kind": "concept",
                "updated_at_utc": "2026-04-23T02:30:00+00:00",
                "latest_report_revision_id": "#V#user_turn_report",
            }
            if dossier_id == "#V#user_turn_dossier"
            else None
        ),
    )
    monkeypatch.setattr(
        memory_service,
        "load_workflow_report_revision_state",
        lambda revision_id: (
            {
                "dossier_id": "#V#user_turn_dossier",
                "title": "Latest represented report",
            }
            if revision_id == "#V#user_turn_report"
            else None
        ),
    )
    monkeypatch.setattr(
        memory_service,
        "build_reconstructed_workspace",
        lambda **_kwargs: {
            "success": True,
            "workspace": {
                "workspace_fingerprint": "workspace-fp-1",
                "open_questions": [
                    "Which identity details are grounded in the represented context?"
                ],
                "immediate_context": {
                    "current_turn_prompt": "Who am I?",
                    "recent_user_prompts": ["Earlier identity question"],
                },
                "evidence_receipts": [{"source": "bundle"}],
            },
        },
    )

    state = memory_service.build_turn_memory_context_state(
        prompt="Who am I?",
        recent_user_prompts=["Earlier identity question"],
        conversation_session_id="session-1",
        user_namespace="#V#test_user",
        user_concept_id="#V#test_user",
    )

    assert state["status"] == "available"
    assert state["fail_closed"] is False
    subject_contexts = state["subject_contexts"]
    assert len(subject_contexts) == 1
    assert subject_contexts[0]["context_dossier_id"] == "#V#user_turn_dossier"
    assert subject_contexts[0]["workspace_fingerprint"] == "workspace-fp-1"

    rendered_messages = memory_service.render_turn_memory_context_messages(state)
    assert len(rendered_messages) == 1
    assert "AUTHORITATIVE TURN MEMORY CONTEXT (User):" in rendered_messages[0]["content"]
    assert "#V#bundle_user_memory" in rendered_messages[0]["content"]
    assert "workspace-fp-1" in rendered_messages[0]["content"]

    lineage_summary = memory_service.summarise_turn_memory_context_for_lineage(state)
    assert lineage_summary == {
        "status": "available",
        "fail_closed": False,
        "subject_count": 1,
        "available_subject_count": 1,
        "context_dossier_ids": ["#V#user_turn_dossier"],
        "workspace_fingerprints": ["workspace-fp-1"],
    }


def test_build_turn_memory_context_state_fails_closed_for_missing_explicit_dossier(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        memory_service,
        "load_context_dossier_state",
        lambda _dossier_id: None,
    )

    state = memory_service.build_turn_memory_context_state(
        prompt="Use the requested dossier only.",
        user_namespace="#V#test_user",
        user_concept_id="#V#test_user",
        turn_memory_context={"context_dossier_id": "#V#missing_dossier"},
    )

    assert state["status"] == "unavailable"
    assert state["fail_closed"] is True
    assert state["failure_reason"] == "requested_context_dossier_missing"
    assert state["requested_memory_context"] == {
        "context_dossier_id": "#V#missing_dossier"
    }
    assert memory_service.render_turn_memory_context_messages(state) == []


def test_build_selected_workflow_policy_memory_state_renders_recent_suggestions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        memory_service,
        "list_recent_workflow_improvement_suggestions",
        lambda *_args, **_kwargs: [
            {
                "memory_id": "#V#policy_memory_1",
                "suggestion_id": "#V#workflow_suggestion_1",
                "category": "grounding",
                "priority": "high",
                "target_surface": "workflow",
                "title": "Prefer grounded identity evidence before answering",
                "rationale": "Recent evaluated turns regressed into stale identity claims.",
                "request_id": "request-1",
            }
        ],
    )

    state = memory_service.build_selected_workflow_policy_memory_state(
        selected_workflow_id="#V#chat_assistant_workflow",
        namespace="#V#test_user",
    )

    assert state["status"] == "available"
    assert state["suggestion_count"] == 1
    rendered_messages = memory_service.render_selected_workflow_policy_memory_messages(
        state
    )
    assert len(rendered_messages) == 1
    assert (
        "RECENT POLICY MEMORY FOR #V#chat_assistant_workflow:"
        in rendered_messages[0]["content"]
    )
    assert "Prefer grounded identity evidence before answering" in rendered_messages[0][
        "content"
    ]
    assert "Recent evaluated turns regressed into stale identity claims." in (
        rendered_messages[0]["content"]
    )

    lineage_summary = (
        memory_service.summarise_selected_workflow_policy_memory_for_lineage(state)
    )
    assert lineage_summary == {
        "status": "available",
        "selected_workflow_id": "#V#chat_assistant_workflow",
        "suggestion_count": 1,
        "memory_ids": ["#V#policy_memory_1"],
        "suggestion_ids": ["#V#workflow_suggestion_1"],
    }
