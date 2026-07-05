"""Tests for the multi-turn follow-up replay bank and oracle.

The oracle is pure and generic: it evaluates per-turn evidence against bank
expectation keys without any domain branch. These tests pin the oracle
semantics (obligation suppression, stale-referent grounding, multi-target
coverage, inconclusive-on-missing-evidence) and validate the shipped bank.
"""

from __future__ import annotations

import scripts.run_live_multi_turn_followup_replay as runner


def _record_with_obligation(*, sources, satisfied=False, suppressed=None):
    record = {
        "decision": "completed",
        "required_effects": [
            {
                "effect_id": "effect_required_tool_obligations_1",
                "required_tool_obligations": {
                    "obligations": [
                        {
                            "tool_name": "some_prior_tool",
                            "sources": list(sources),
                            "satisfied": satisfied,
                        }
                    ]
                },
            }
        ],
    }
    if suppressed is not None:
        record["execution"] = {
            "summary": {
                "expected_outcome_obligation_carry_forward": {
                    "suppressed_prior_obligations": list(suppressed),
                }
            }
        }
    return record


def _verdict(result):
    return result["verdict"]


def _check(result, name):
    return next(check for check in result["checks"] if check["check"] == name)


# --- bank validity ----------------------------------------------------------


def test_shipped_bank_is_valid() -> None:
    bank = runner.load_bank()
    assert bank["schema_version"] == runner.BANK_SCHEMA_VERSION
    case_ids = {case["case_id"] for case in bank["cases"]}
    assert {
        "arxiv_ingest_followup_family",
        "gmail_review_followup_family",
        "jira_lookup_followup_family",
        "generic_concept_followup_family",
        "arxiv_degraded_success_ingestion",
    } <= case_ids


def test_bank_rejects_unknown_expectation_keys() -> None:
    payload = {
        "schema_version": runner.BANK_SCHEMA_VERSION,
        "cases": [
            {
                "case_id": "x",
                "turns": [
                    {
                        "role": "task",
                        "prompt": "p",
                        "expectations": {"made_up_key": True},
                    }
                ],
            }
        ],
    }
    try:
        runner.validate_bank(payload)
    except ValueError as exc:
        assert "made_up_key" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown expectation key")


# --- obligation suppression oracle -----------------------------------------


def test_bare_followup_passes_when_suppression_recorded() -> None:
    record = _record_with_obligation(
        sources=["some_other_source"],
        satisfied=True,
        suppressed=["prior_tool_a", "prior_tool_b"],
    )
    result = runner.evaluate_turn_expectations(
        expectations={
            "require_completed": True,
            "forbid_decisions": ["escalation_required"],
            "require_prior_obligation_suppression": True,
        },
        visible_answer="Yes, that concept exists.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record=record,
    )
    assert _verdict(result) == "pass"


def test_bare_followup_fails_when_prior_contract_obligation_unsatisfied() -> None:
    record = _record_with_obligation(
        sources=["turn_expected_outcome_conditional_required_tools"],
        satisfied=False,
    )
    record["decision"] = "escalation_required"
    result = runner.evaluate_turn_expectations(
        expectations={
            "forbid_decisions": ["escalation_required"],
            "require_prior_obligation_suppression": True,
        },
        visible_answer="I could not verify that yet.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record=record,
    )
    assert _verdict(result) == "fail"
    assert _check(result, "forbid_decisions")["outcome"] == "fail"
    assert (
        _check(result, "forbid_unsatisfied_obligation_sources")["outcome"] == "fail"
    )


def test_missing_turn_record_is_inconclusive_not_silent_pass() -> None:
    result = runner.evaluate_turn_expectations(
        expectations={
            "forbid_decisions": ["escalation_required"],
            "require_prior_obligation_suppression": True,
        },
        visible_answer="answer",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={},
    )
    assert _verdict(result) == "inconclusive"


# --- grounding oracle -------------------------------------------------------


def test_different_target_grounding_fails_on_stale_concept_leak() -> None:
    result = runner.evaluate_turn_expectations(
        expectations={
            "response_must_mention_any": ["target_b_token"],
            "response_must_not_mention": ["stale_concept_from_target_a"],
        },
        visible_answer=(
            "Verified: stale_concept_from_target_a is represented."
        ),
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(result) == "fail"
    assert _check(result, "response_must_mention_any")["outcome"] == "fail"
    assert _check(result, "response_must_not_mention")["outcome"] == "fail"


def test_different_target_grounding_passes_on_fresh_concept() -> None:
    result = runner.evaluate_turn_expectations(
        expectations={
            "response_must_mention_any": ["target_b_token"],
            "response_must_not_mention": ["stale_concept_from_target_a"],
        },
        visible_answer="Yes - target_b_token is represented and verified.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(result) == "pass"


def test_multi_target_requires_every_group() -> None:
    expectations = {
        "response_must_mention_all_groups": [
            ["alpha_id", "alpha name"],
            ["beta_id"],
        ]
    }
    both = runner.evaluate_turn_expectations(
        expectations=expectations,
        visible_answer="alpha name is represented; beta_id is also represented.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(both) == "pass"

    only_one = runner.evaluate_turn_expectations(
        expectations=expectations,
        visible_answer="alpha name is represented.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(only_one) == "fail"


# --- visible-answer shape invariant -----------------------------------------


def test_visible_answer_raw_json_payload_fails_even_when_bank_checks_pass() -> None:
    truncated_envelope = (
        '{\n  "turn_next_action": {\n    "action_type": "respond_with_follow_up",\n'
        '    "response_text": "I ingested 2406.15341 and read back the concept'
    )
    result = runner.evaluate_turn_expectations(
        expectations={"response_must_mention_any": ["2406.15341"]},
        visible_answer=truncated_envelope,
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(result) == "fail"
    assert _check(result, "visible_answer_not_raw_payload")["outcome"] == "fail"


def test_visible_answer_complete_tool_call_json_fails() -> None:
    result = runner.evaluate_turn_expectations(
        expectations={},
        visible_answer=(
            '{"action": "call_tool", "tool": "arxiv.download_paper",'
            ' "payload": {"paper_id": "2406.15341v3"}}'
        ),
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(result) == "fail"
    assert _check(result, "visible_answer_not_raw_payload")["outcome"] == "fail"


def test_visible_answer_prose_does_not_trigger_raw_payload_check() -> None:
    result = runner.evaluate_turn_expectations(
        expectations={"response_must_mention_any": ["2406.15341"]},
        visible_answer="Paper 2406.15341 is represented; the stored concept reads back.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record={"decision": "completed"},
    )
    assert _verdict(result) == "pass"
    assert all(
        check["check"] != "visible_answer_not_raw_payload"
        for check in result["checks"]
    )


# --- continuation + workflow expectations -----------------------------------


def test_explicit_continuation_allows_inherited_obligations() -> None:
    record = _record_with_obligation(
        sources=["turn_expected_outcome_conditional_required_tools"],
        satisfied=False,
    )
    result = runner.evaluate_turn_expectations(
        expectations={
            "allow_obligation_carry_forward": True,
            "require_completed": True,
        },
        visible_answer="Verified the earlier ingestion.",
        terminal_status="completed",
        selected_workflow_ids=[],
        turn_record=record,
    )
    # No suppression/forbidden-source checks fire for a continuation turn.
    assert _verdict(result) == "pass"


def test_expected_workflow_check() -> None:
    result = runner.evaluate_turn_expectations(
        expectations={"expected_workflow_id": "#V#some_workflow"},
        visible_answer="done",
        terminal_status="completed",
        selected_workflow_ids=["#V#other_workflow"],
        turn_record={"decision": "completed"},
    )
    assert _verdict(result) == "fail"


def test_fetch_turn_record_falls_back_to_projected_request_record(monkeypatch) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def _raise_history_unavailable(*_args, **_kwargs):
        raise RuntimeError("history unavailable")

    def _fake_projection_lookup(*, request_id, namespace=None):
        calls.append((request_id, namespace))
        return {
            "request_id": request_id,
            "namespace": namespace,
            "decision": "completed",
        }

    monkeypatch.setattr(runner, "_request_json", _raise_history_unavailable)
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service."
        "get_turn_execution_record_projection",
        _fake_projection_lookup,
    )

    record = runner.fetch_turn_record(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5010",
        chat_session_id="session-1",
        request_id="request-1",
        namespace="#V#user@org",
        task_result={},
    )

    assert record["decision"] == "completed"
    assert calls == [("request-1", "#V#user@org")]


def test_fetch_turn_record_skips_history_record_for_different_request(
    monkeypatch,
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def _fake_request_json(_session, _method, url, *, params=None):
        if url.endswith("/von/history"):
            return {
                "history": [
                    {
                        "role": "assistant",
                        "history_location": {"history_index": 7},
                    }
                ]
            }
        if url.endswith("/von/history/debug"):
            return {
                "llm_debug_data": {
                    "turn_execution_record": {
                        "request_id": "other-request",
                        "decision": "completed",
                    }
                }
            }
        raise AssertionError(f"unexpected URL {url}")

    def _fake_projection_lookup(*, request_id, namespace=None):
        calls.append((request_id, namespace))
        return {
            "request_id": request_id,
            "namespace": namespace,
            "decision": "failed",
        }

    monkeypatch.setattr(runner, "_request_json", _fake_request_json)
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service."
        "get_turn_execution_record_projection",
        _fake_projection_lookup,
    )

    record = runner.fetch_turn_record(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5010",
        chat_session_id="session-1",
        request_id="request-1",
        namespace="#V#user@org",
        task_result={},
    )

    assert record["request_id"] == "request-1"
    assert record["decision"] == "failed"
    assert calls == [("request-1", "#V#user@org")]


def test_extract_visible_answer_prefers_turn_record_checked_final_response() -> None:
    record = {
        "requested_evidence_lineage": {
            "final_response": {
                "text_checked_preview": "Current target could not be verified yet."
            }
        },
        "completion_report": {
            "response_text": "Stale selected-workflow answer from a prior target."
        },
    }

    answer, source = runner.extract_visible_answer_from_turn_record(record)

    assert answer == "Current target could not be verified yet."
    assert source == "requested_evidence_lineage.final_response.text_checked_preview"
