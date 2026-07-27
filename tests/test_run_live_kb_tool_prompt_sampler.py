from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import run_live_kb_tool_prompt_sampler as sampler


def _prompt_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "case-1",
        "category": "research_assistance",
        "complexity_class": "tool_augmented",
        "prompt": "Find the relevant represented research.",
        "knowledge_surfaces": ["kb"],
        "likely_tools": ["search_knowledge_base"],
        "requires_tool_use": True,
    }
    entry.update(overrides)
    return entry


def _summary_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "prompt_entry": _prompt_entry(),
        "task_id": "task-1",
        "session_id": "session-1",
        "request_id": "request-1",
        "history_location": {
            "source": "history_debug",
            "session_id": "session-1",
            "history_index": 2,
        },
        "generate_payload": {
            "response": "A grounded answer.",
            "background_task_status": {"status": "completed"},
        },
        "llm_debug_data": {},
        "prompt_bank_schema_version": "live_kb_tool_prompt_bank.v3",
        "requested_complexity_classes": ["tool_augmented"],
        "seed": 7,
        "requested_model": "ollama:local-model",
        "run_environment": {"base_url": "http://127.0.0.1:5001"},
    }
    values.update(overrides)
    return values


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _SequencedSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


def test_prompt_bank_has_one_external_runtime_source() -> None:
    file_payload = json.loads(sampler.PROMPT_BANK_PATH.read_text(encoding="utf-8"))

    assert file_payload == sampler.PROMPT_BANK_PAYLOAD
    assert sampler._load_prompt_bank() == file_payload
    assert "EMBEDDED_PROMPT_BANK_PAYLOAD" not in vars(sampler)


def test_choose_prompt_respects_complexity_filter_and_seed() -> None:
    bank = [
        _prompt_entry(id="direct", complexity_class="direct_context_or_background"),
        _prompt_entry(id="tool-a", complexity_class="tool_augmented"),
        _prompt_entry(id="tool-b", complexity_class="tool_augmented"),
    ]

    selected_a = sampler._choose_prompt(
        bank,
        seed=4,
        prompt_id=None,
        allowed_complexity_classes=frozenset({"tool_augmented"}),
    )
    selected_b = sampler._choose_prompt(
        bank,
        seed=4,
        prompt_id=None,
        allowed_complexity_classes=frozenset({"tool_augmented"}),
    )

    assert selected_a == selected_b
    assert selected_a["id"] in {"tool-a", "tool-b"}


def test_model_arm_plan_records_explicit_models_without_certifying_them() -> None:
    arms = sampler._build_model_arm_plan(
        requested_model="ollama:small",
        compare_models=["openai:gpt-frontier", "ollama:small"],
        include_active_model_arm=True,
    )

    assert [arm["requested_model"] for arm in arms] == [
        None,
        "ollama:small",
        "openai:gpt-frontier",
    ]
    assert arms[2]["requested_provider"] == "openai"
    assert all("certification" not in arm for arm in arms)


def test_run_generate_background_sends_only_user_and_runtime_inputs() -> None:
    session = _SequencedSession(
        [
            _FakeResponse({"task_id": "task-1"}, status_code=202),
            _FakeResponse({"status": "completed"}),
            _FakeResponse(
                {
                    "result": {
                        "request_id": "request-1",
                        "session_id": "session-1",
                        "response": "Done.",
                    }
                }
            ),
        ]
    )

    task_id, result = sampler._run_generate_background(
        session=session,  # type: ignore[arg-type]
        base_url="http://von.test",
        prompt="Use your best judgement.",
        model="openai:gpt-frontier",
        gmail_profile="research",
        presenter_mode=True,
        timeout_seconds=30,
        poll_interval_seconds=0.01,
        include_status_payload=True,
    )

    submitted = session.requests[0]["json"]
    assert task_id == "task-1"
    assert result["response"] == "Done."
    assert submitted["prompt"] == "Use your best judgement."
    assert submitted["model"] == "openai:gpt-frontier"
    assert submitted["model_provider"] == "openai"
    assert submitted["gmail_profile"] == "research"
    assert submitted["presenter_mode"] is True
    assert "turn_expected_outcome_contract" not in submitted
    assert "agent_test_selector_replay_mode" not in submitted


def test_task_result_debug_fallback_preserves_terminal_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sampler,
        "_find_assistant_turn_history_location",
        lambda **_: (_ for _ in ()).throw(RuntimeError("history not settled")),
    )
    turn_record = {
        "terminal_status": "completed",
        "tool_invocations": [
            {
                "tool": "relation_upsert",
                "effective_payload": {
                    "effect_id": "effect-1",
                    "readback": {"status": "observed"},
                },
            }
        ],
    }

    location, debug = sampler._resolve_turn_debug_data(
        session=object(),  # type: ignore[arg-type]
        base_url="http://von.test",
        session_id="session-1",
        request_id="request-1",
        response_text="Done.",
        generate_payload={
            "response": "Done.",
            "turn_execution_record": turn_record,
        },
    )

    assert location["source"] == "background_task_result"
    assert "history not settled" in location["history_lookup_error"]
    assert debug["turn_execution_record"] == turn_record


def test_terminal_task_debug_enrichment_wins_without_losing_history() -> None:
    merged = sampler._merge_history_and_task_result_debug(
        {
            "request_id": "request-1",
            "history_only": {"value": 1},
            "turn_execution_record": {"terminal_status": "running"},
        },
        {
            "turn_execution_record": {
                "terminal_status": "completed",
                "tool_invocations": [{"tool": "search_records"}],
            }
        },
    )

    assert merged["history_only"] == {"value": 1}
    assert merged["turn_execution_record"]["terminal_status"] == "completed"
    assert merged["turn_execution_record"]["tool_invocations"] == [
        {"tool": "search_records"}
    ]


def test_summary_preserves_effect_readback_model_timing_and_llm_calls() -> None:
    invocation = {
        "tool": "relation_upsert",
        "status": "success",
        "effective_payload": {
            "effect_id": "effect-1",
            "operation": "upsert_relation",
            "readback": {
                "status": "observed",
                "closure_changed": False,
            },
        },
    }
    llm_call = {
        "stage": "turn_answer",
        "model": "openai:gpt-frontier",
        "elapsed_ms": 812,
    }
    debug = {
        "model": "openai:gpt-frontier",
        "turn_execution_record": {
            "terminal_status": "completed",
            "model": "openai:gpt-frontier",
            "tool_invocations": [invocation],
            "llm_calls": [llm_call],
            "timing_breakdown": {
                "totals": {
                    "elapsed_ms": 1234,
                    "llm_elapsed_ms": 812,
                    "llm_call_count": 1,
                },
                "llm_calls_by_stage_model": [
                    {
                        "stage": "turn_answer",
                        "model": "openai:gpt-frontier",
                        "call_count": 1,
                    }
                ],
            },
        },
    }

    summary = sampler._build_summary(**_summary_kwargs(llm_debug_data=debug))

    telemetry = summary["telemetry"]
    assert summary["status"] == "ok"
    assert telemetry["ordinary_turn_terminal_status"] == "completed"
    assert telemetry["model"] == "openai:gpt-frontier"
    assert telemetry["tool_invocations"] == [invocation]
    assert telemetry["observed_tools"] == ["relation_upsert"]
    assert telemetry["tool_count"] == 1
    assert telemetry["tool_observation_ledger"]["observation_count"] == 1
    assert telemetry["tool_invocations"][0]["effective_payload"]["readback"] == {
        "status": "observed",
        "closure_changed": False,
    }
    assert telemetry["timing"]["elapsed_ms"] == 1234
    assert telemetry["timing"]["llm_elapsed_ms"] == 812
    assert telemetry["llm_calls"] == [llm_call]
    assert "evaluation" not in summary
    assert "action_outcome" not in summary
    assert "model_portfolio_evaluation" not in summary
    assert "decision_attribution" not in summary
    assert "selector_telemetry_completeness" not in telemetry


def test_missing_or_non_success_completion_gate_does_not_change_collection_status() -> (
    None
):
    without_gate = sampler._build_summary(**_summary_kwargs())
    with_gate = sampler._build_summary(
        **_summary_kwargs(
            llm_debug_data={
                "turn_execution_record": {
                    "terminal_status": "completed",
                    "completion_gate": {
                        "status": "partial",
                        "safe_to_claim_completion": False,
                    },
                }
            }
        )
    )

    assert without_gate["status"] == "ok"
    assert with_gate["status"] == "ok"
    assert with_gate["telemetry"]["terminal_observations"] == {
        "turn_execution_record": {
            "completion_gate": {
                "status": "partial",
                "safe_to_claim_completion": False,
            }
        }
    }
    assert "terminal_observations" in without_gate["telemetry"]


def test_summary_derives_observation_ledger_but_not_semantic_outcome() -> None:
    summary = sampler._build_summary(
        **_summary_kwargs(
            llm_debug_data={
                "tool_invocations": [
                    {
                        "tool": "search_records",
                        "status": "success",
                        "result": {"items": []},
                    }
                ]
            }
        )
    )

    ledger = summary["telemetry"]["tool_observation_ledger"]
    assert ledger["observation_count"] == 1
    assert ledger["observed_tools"] == ["search_records"]
    assert "action_outcome" not in summary


def test_summary_preserves_persisted_unrecovered_tool_failure() -> None:
    ledger = {
        "schema_version": sampler.TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION,
        "observation_count": 1,
        "observed_tools": ["gmail_list_messages"],
        "observations": [
            {
                "tool": "gmail_list_messages",
                "status": "auth_failed",
                "error_code": "oauth_expired",
                "error": "Authentication expired.",
            }
        ],
    }
    summary = sampler._build_summary(
        **_summary_kwargs(
            llm_debug_data={
                "turn_execution_record": {
                    "terminal_status": "completed",
                    "tool_observation_ledger": ledger,
                }
            }
        )
    )

    assert summary["status"] == "ok"
    assert summary["telemetry"]["tool_observation_ledger"] == ledger
    assert summary["telemetry"]["observed_tools"] == ["gmail_list_messages"]
    assert summary["telemetry"]["tool_count"] == 1
    assert (
        summary["telemetry"]["tool_observation_ledger"]["observations"][0]["error_code"]
        == "oauth_expired"
    )


def test_prompt_variant_observation_is_only_built_for_explicit_variant_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        sampler.replay_experiment_observation_service,
        "build_prompt_variant_evaluation",
        lambda **_: calls.append("variant") or {"selected_prompt_id": "#V#variant"},
    )
    monkeypatch.setattr(
        sampler.replay_experiment_observation_service,
        "build_replay_scoring_consistency",
        lambda **_: calls.append("consistency") or {"non_promotable": False},
    )

    ordinary = sampler._build_summary(**_summary_kwargs(arm_metadata={"arm_id": "a"}))
    variant = sampler._build_summary(
        **_summary_kwargs(
            arm_metadata={
                "arm_id": "b",
                "base_prompt_id": "#V#base",
                "candidate_prompt_variant_id": "#V#variant",
            }
        )
    )

    assert "prompt_variant_evaluation" not in ordinary
    assert variant["prompt_variant_evaluation"]["selected_prompt_id"] == "#V#variant"
    assert calls == ["variant", "consistency"]


def test_multi_arm_summary_compares_raw_observations() -> None:
    arms = [
        {
            "status": "ok",
            "arm": {
                "arm_id": "a",
                "label": "fast",
                "requested_model": "ollama:fast",
            },
            "response": {"text": "answer"},
            "telemetry": {
                "model": "ollama:fast",
                "ordinary_turn_terminal_status": "completed",
                "observed_tools": ["search_records"],
                "tool_count": 1,
                "timing": {"elapsed_ms": 800},
            },
        },
        {
            "status": "error",
            "arm": {
                "arm_id": "b",
                "label": "strong",
                "requested_model": "openai:gpt-frontier",
            },
            "response": {"text": ""},
            "telemetry": {"requested_model": "openai:gpt-frontier"},
        },
    ]

    summary = sampler._build_multi_arm_summary(
        prompt_entry=_prompt_entry(),
        prompt_bank_schema_version="v3",
        requested_complexity_classes=[],
        seed=None,
        requested_model="ollama:fast",
        requested_model_arms=[arm["arm"] for arm in arms],
        run_environment={},
        arm_summaries=arms,
    )

    comparison = summary["comparison"]
    assert summary["status"] == "partial"
    assert comparison["collected_arm_count"] == 1
    assert comparison["error_arm_count"] == 1
    assert comparison["all_arms_collected"] is False
    assert comparison["arm_observations"][0]["timing"]["elapsed_ms"] == 800
    assert "all_should_user_be_happy" not in comparison
    assert "model_portfolio_report" not in summary


def test_replay_plan_collection_success_does_not_read_semantic_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sampler,
        "_run_prompt_replay_arm",
        lambda **_: {
            "status": "ok",
            "evaluation": {"should_user_be_happy": False},
        },
    )

    summary, collected = sampler._run_replay_plan(
        prompt_entry=_prompt_entry(),
        base_url="http://von.test",
        requested_model="ollama:local",
        replay_arms=[{"arm_id": "a", "requested_model": "ollama:local"}],
        timeout_seconds=30,
        poll_interval_seconds=0.1,
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        session_name="sample",
        run_environment={},
        prompt_bank_schema_version="v3",
        requested_complexity_classes=[],
        seed=None,
        base_prompt_id=None,
        prompt_variant_ids=[],
        presenter_mode=False,
        gmail_profile=None,
    )

    assert summary["status"] == "ok"
    assert collected is True


def test_multi_arm_replay_preserves_partial_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run_arm(**kwargs: Any) -> dict[str, Any]:
        arm = kwargs["arm_metadata"]
        if arm["arm_id"] == "arm_2":
            raise RuntimeError("model endpoint unavailable")
        return {
            "status": "ok",
            "arm": dict(arm),
            "response": {"text": "Collected."},
            "telemetry": {
                "model": arm["requested_model"],
                "ordinary_turn_terminal_status": "completed",
                "observed_tools": [],
                "tool_count": 0,
                "timing": {"elapsed_ms": 10},
            },
        }

    monkeypatch.setattr(sampler, "_run_prompt_replay_arm", run_arm)
    arms = [
        {"arm_id": "arm_1", "label": "fast", "requested_model": "ollama:fast"},
        {
            "arm_id": "arm_2",
            "label": "strong",
            "requested_model": "openai:gpt-frontier",
        },
    ]

    summary, collected = sampler._run_replay_plan(
        prompt_entry=_prompt_entry(),
        base_url="http://von.test",
        requested_model="ollama:fast",
        replay_arms=arms,
        timeout_seconds=30,
        poll_interval_seconds=0.1,
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        session_name="sample",
        run_environment={},
        prompt_bank_schema_version="v3",
        requested_complexity_classes=[],
        seed=None,
        base_prompt_id=None,
        prompt_variant_ids=[],
        presenter_mode=False,
        gmail_profile=None,
    )

    assert collected is False
    assert summary["comparison"]["collected_arm_count"] == 1
    assert summary["arms"][0]["response"]["text"] == "Collected."
    assert summary["arms"][1]["response"]["failure"]["message"] == (
        "model endpoint unavailable"
    )


def test_failed_attempt_preserves_raw_background_evidence() -> None:
    exc = sampler.BackgroundGenerateTaskError(
        "timed out",
        task_id="task-1",
        status_payload={
            "status": "running",
            "progress": {"phase": "tool_execute", "tool": "search_records"},
        },
        cancellation_payload={
            "post_cancellation_terminal": True,
            "post_cancellation_status_payload": {"status": "cancelled"},
        },
    )

    summary = sampler._build_failed_replay_attempt_summary(
        exc=exc,
        attempt_index=1,
        prompt_entry=_prompt_entry(),
        run_environment={},
        requested_model="ollama:local",
    )

    background = summary["response"]["failure"]["background_task"]
    assert summary["status"] == "error"
    assert background["status_payload"]["progress"]["tool"] == "search_records"
    assert background["cancellation_payload"]["post_cancellation_terminal"] is True
    assert summary["telemetry"]["background_task_observations"] == background
    assert "evaluation" not in summary
    assert "action_outcome" not in summary


def test_repeated_summary_reports_collection_not_semantic_success() -> None:
    summary = sampler._build_repeated_replay_summary(
        prompt_entry=_prompt_entry(),
        prompt_bank_schema_version="v3",
        requested_complexity_classes=[],
        seed=None,
        requested_model="ollama:local",
        requested_model_arms=[],
        run_environment={},
        attempt_summaries=[{"status": "ok"}, {"status": "error"}],
        collection_count=1,
        minimum_collection_rate=0.5,
    )

    repeat = summary["repeat"]
    assert summary["status"] == "ok"
    assert repeat["collected_attempt_count"] == 1
    assert repeat["error_attempt_count"] == 1
    assert repeat["collection_rate"] == 0.5
    assert repeat["metric"] == "harness_collection_only_not_semantic_success"
    assert "success_rate" not in repeat


def test_main_exits_on_collection_status_not_old_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sampler,
        "_collect_run_environment",
        lambda **_: {"server_agent_test_instance": True},
    )
    monkeypatch.setattr(
        sampler,
        "_augment_run_environment_with_server_diag",
        lambda **kwargs: dict(kwargs["run_environment"]),
    )
    monkeypatch.setattr(
        sampler,
        "_run_replay_plan",
        lambda **_: (
            {
                "status": "ok",
                "response": {"text": "Collected."},
                "evaluation": {"should_user_be_happy": False},
            },
            True,
        ),
    )
    output_path = tmp_path / "sample.json"

    exit_code = sampler.main(
        [
            "--prompt-text",
            "Use your best judgement.",
            "--model",
            "openai:gpt-frontier",
            "--allow-non-agent-test-server",
            "--output-json",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "ok"
