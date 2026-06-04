"""Tests for workflow-authored Thinking-card progress fact projection."""

from src.backend.workflows.progress_projection import build_progress_facts_for_step


def test_build_progress_facts_for_step_projects_authored_metadata() -> None:
    facts = build_progress_facts_for_step(
        workflow_id="#V#research_triage_workflow",
        state_id="read_message",
        action_id="gmail_read",
        workflow_metadata={},
        state_metadata={
            "progress_projection": {
                "schema_version": "workflow_progress_projection.v1",
                "facts": [
                    {
                        "fact_id": "email_subject",
                        "label": "Email subject",
                        "source_path": "context.email.subject",
                        "value_kind": "title",
                        "visibility": "default",
                        "contract_id": "#V#email_subject_progress_fact",
                    },
                    {
                        "fact_id": "paper_concept",
                        "label": "Paper concept",
                        "source_path": "action_outputs.paper.concept_id",
                        "value_kind": "concept_id",
                        "visibility": "expert",
                    },
                ],
            }
        },
        context_before={},
        context_after={"email": {"subject": "Progress on salience cards"}},
        action_outputs={"paper": {"concept_id": "#V#paper_attention_is_all_you_need"}},
    )

    assert [fact["fact_id"] for fact in facts] == [
        "email_subject",
        "paper_concept",
    ]
    assert facts[0]["label"] == "Email subject"
    assert facts[0]["value"] == "Progress on salience cards"
    assert facts[0]["contract_id"] == "#V#email_subject_progress_fact"
    assert facts[1]["value"] == "#V#paper_attention_is_all_you_need"
    assert facts[1]["visibility"] == "expert"


def test_build_progress_facts_for_step_fails_closed_for_sensitive_raw_value() -> None:
    facts = build_progress_facts_for_step(
        workflow_id="#V#research_triage_workflow",
        state_id="read_message",
        action_id="gmail_read",
        workflow_metadata={},
        state_metadata={
            "progress_projection": {
                "facts": [
                    {
                        "fact_id": "sender_email",
                        "label": "Sender email",
                        "source_path": "sender.email",
                        "sensitive": True,
                    }
                ]
            }
        },
        context_before={},
        context_after={"sender": {"email": "private@example.invalid"}},
        action_outputs={},
    )

    assert len(facts) == 1
    assert facts[0]["status"] == "redacted"
    assert facts[0]["redacted"] is True
    assert facts[0]["reason_code"] == "redaction_policy_missing"
    assert "value" not in facts[0]
