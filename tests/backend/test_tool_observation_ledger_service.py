from src.backend.services.tool_observation_ledger_service import (
    TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION,
    build_tool_observation_ledger,
)
from src.backend.services.turn_execution_record_service import build_turn_execution_record


def test_ledger_records_invalid_arguments_from_validation_diagnostics() -> None:
    ledger = build_tool_observation_ledger(
        aux_llm_calls=[
            {
                "type": "tool_contract_attempt",
                "diagnostics": [
                    {
                        "tool": "search_arxiv",
                        "error_code": "schema_validation_failed",
                        "message": "search_arxiv: sort_by is not one of ['relevance', 'date']",
                    }
                ],
            }
        ]
    )

    assert ledger["schema_version"] == TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION
    assert ledger["has_invalid_args"] is True
    assert ledger["status_counts"] == {"invalid_args": 1}
    observation = ledger["observations"][0]
    assert observation["tool"] == "search_arxiv"
    assert observation["status"] == "invalid_args"
    assert observation["error_code"] == "schema_validation_failed"


def test_ledger_records_auth_or_unavailable_failures() -> None:
    ledger = build_tool_observation_ledger(
        tool_invocations=[
            {
                "tool": "gmail_list_messages",
                "status": "error",
                "error": "OAuth credentials unavailable for Gmail profile",
            }
        ]
    )

    assert ledger["has_unavailable_or_auth_failure"] is True
    assert ledger["observations"][0]["status"] == "auth_failed"


def test_ledger_records_pre_normalised_runtime_observations() -> None:
    ledger = build_tool_observation_ledger(
        tool_observations=[
            {
                "source": "background_task_progress",
                "tool": "jira_get_issue",
                "status": "tool_invoked",
                "call_id": "call-1",
                "result_summary": "Issue: JVNAUTOSCI-150",
            }
        ]
    )

    assert ledger["observed_tools"] == ["jira_get_issue"]
    assert ledger["status_counts"] == {"ok": 1}
    assert ledger["observations"][0]["source"] == "background_task_progress"
    assert ledger["observations"][0]["result_summary"] == "Issue: JVNAUTOSCI-150"


def test_ledger_distinguishes_empty_and_non_empty_observations_without_raw_result() -> None:
    ledger = build_tool_observation_ledger(
        tool_invocations=[
            {
                "tool": "search_arxiv",
                "status": "ok",
                "result_summary": "No results",
                "effective_payload": {"papers": []},
            },
            {
                "tool": "jira_get_issue",
                "status": "ok",
                "result_summary": "Loaded issue JVNAUTOSCI-150",
                "effective_payload": {
                    "items": [{"key": "JVNAUTOSCI-150", "summary": "private-ish"}]
                },
            },
        ]
    )

    assert ledger["has_empty_observation"] is True
    assert ledger["has_non_empty_observation"] is True
    statuses = {entry["tool"]: entry["status"] for entry in ledger["observations"]}
    assert statuses == {
        "search_arxiv": "empty_result",
        "jira_get_issue": "non_empty_result",
    }
    assert "effective_payload" not in ledger["observations"][1]
    assert "private-ish" not in str(ledger["observations"])


def test_ledger_records_cancelled_pending_tool_from_runtime_diagnostics() -> None:
    ledger = build_tool_observation_ledger(
        turn_execution_diagnostics={
            "latest_progress": {
                "status": "cancelled",
                "phase": "cancelled",
                "tool_history": [
                    {
                        "tool": "jira_get_issue",
                        "phase": "tool_execute",
                        "success": None,
                    }
                ],
            }
        }
    )

    assert ledger["has_timeout_or_cancellation"] is True
    assert ledger["observations"][0]["status"] == "cancelled"


def test_turn_execution_record_projects_tool_observation_ledger() -> None:
    record = build_turn_execution_record(
        request_id="req-tool-ledger",
        session_id="session-tool-ledger",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Tell me about JVNAUTOSCI-150 in Jira.",
        response_text="Loaded the issue.",
        interaction_timestamp_utc="2026-06-27T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
            "source": "selector",
        },
        tool_invocations=[
            {
                "tool": "jira_get_issue",
                "status": "ok",
                "result_summary": "Loaded issue JVNAUTOSCI-150",
                "effective_payload": {"items": [{"key": "JVNAUTOSCI-150"}]},
            }
        ],
    )

    ledger = record["execution"]["tool_observation_ledger"]
    assert ledger["schema_version"] == TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION
    assert ledger["observed_tools"] == ["jira_get_issue"]
    assert ledger["status_counts"] == {"non_empty_result": 1}
    assert record["execution"]["summary"]["tool_observation_count"] == 1


def test_ledger_carries_authored_terminal_receipt_without_tool_observations() -> None:
    ledger = build_tool_observation_ledger(
        terminal_outcome_receipt={
            "schema_version": "terminal_outcome_receipt.v1",
            "profile_concept_id": "#V#terminal_outcome_receipt",
            "outcome": "externally_blocked",
            "cause_code": "service_unavailable",
            "causal_stage": "invocation",
            "summary": "The represented action reached an unavailable dependency.",
            "evidence_refs": [{"source": "runtime", "ref": "event-1"}],
            "committed_effects": [],
            "remaining_obligations": [{"effect_id": "effect-1"}],
            "retryability": "after_external_change",
            "recovery_affordances": [{"action_type": "inspect"}],
            "learning_candidate": None,
            "redaction_status": "safe_projection",
            "provenance": {"decision_source": "represented_llm"},
        }
    )

    assert ledger["observation_count"] == 0
    projection = ledger["terminal_outcome_receipt_projection"]
    assert projection["available"] is True
    assert projection["outcome"] == "externally_blocked"
    assert projection["retryability"] == "after_external_change"
