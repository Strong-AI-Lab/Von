from __future__ import annotations

from scripts import live_test_server_defaults as defaults
from scripts import run_live_arxiv_ingestion_workflow_test as arxiv_test
from scripts import run_live_kb_tool_prompt_sampler as sampler
from src.backend.services.agent_test_replay_mode_service import (
    AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY,
    AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM,
)


def test_live_test_scripts_default_to_2070_agent_test_port() -> None:
    assert defaults.DEFAULT_AGENT_TEST_BASE_URL == "http://127.0.0.1:5010"
    assert sampler.DEFAULT_BASE_URL == defaults.DEFAULT_AGENT_TEST_BASE_URL
    assert arxiv_test.DEFAULT_BASE_URL == defaults.DEFAULT_AGENT_TEST_BASE_URL


def test_agent_test_base_url_can_follow_explicit_test_port_env() -> None:
    assert (
        defaults.get_default_agent_test_base_url(
            {"VON_AGENT_TEST_BASE_URL": "http://127.0.0.1:5011/"}
        )
        == "http://127.0.0.1:5011"
    )


def test_live_test_base_url_resolution_prefers_explicit_value() -> None:
    assert (
        defaults.resolve_live_test_base_url(
            "http://127.0.0.1:5012/",
            environ={"VON_AGENT_TEST_BASE_URL": "http://127.0.0.1:5011"},
        )
        == "http://127.0.0.1:5012"
    )


def test_agent_test_server_requirement_accepts_health_marker() -> None:
    assert (
        defaults.build_agent_test_server_requirement_error(
            {
                "server_agent_test_instance": True,
                "server_metadata_source": "health",
            },
            base_url="http://127.0.0.1:5010",
        )
        is None
    )


def test_agent_test_server_requirement_rejects_missing_health_marker() -> None:
    error = defaults.build_agent_test_server_requirement_error(
        {
            "server_metadata_source": "health",
            "server_metadata_error": None,
        },
        base_url="http://127.0.0.1:5000",
    )

    assert error is not None
    assert "JVNAUTOSCI-2070" in error
    assert r".\run.ps1 restart -AgentTest -HealthTimeoutSec 180" in error
    assert "--allow-non-agent-test-server" in error


def test_arxiv_live_test_requires_agent_test_health_marker(monkeypatch) -> None:
    def fake_request_json(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"agent_test_instance": False}

    monkeypatch.setattr(arxiv_test, "_request_json", fake_request_json)

    message = ""
    try:
        arxiv_test._require_agent_test_server(
            session=object(),  # type: ignore[arg-type]
            base_url="http://127.0.0.1:5000",
            allow_non_agent_test_server=False,
        )
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RuntimeError")

    assert "JVNAUTOSCI-2070" in message


def test_prompt_sampler_background_payload_carries_selector_replay_mode(
    monkeypatch,
) -> None:
    captured_payloads: list[dict[str, object]] = []

    def fake_request_json(
        _session: object,
        method: str,
        url: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if method == "POST" and url.endswith("/von/generate"):
            captured_payloads.append(dict(kwargs["json"]))  # type: ignore[index]
            return {"task_id": "task-1"}
        if method == "GET" and url.endswith("/von/api/task/status/task-1"):
            return {"status": "completed"}
        if method == "GET" and url.endswith("/von/api/task/result/task-1"):
            return {"result": {"response": "ok", "request_id": "task-1"}}
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    task_id, payload = sampler._run_generate_background(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5010",
        prompt="Prompt",
        model="ollama/gemma4:e4b",
        gmail_profile=None,
        presenter_mode=False,
        agent_test_selector_replay_mode=AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM,
        turn_expected_outcome_contract=None,
        timeout_seconds=30.0,
        poll_interval_seconds=0.2,
    )

    assert task_id == "task-1"
    assert payload["response"] == "ok"
    assert captured_payloads[0][AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY] == (
        AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM
    )


def test_prompt_sampler_waits_for_background_result_diagnostics(
    monkeypatch,
) -> None:
    result_fetch_count = 0

    def fake_request_json(
        _session: object,
        method: str,
        url: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal result_fetch_count
        if method == "POST" and url.endswith("/von/generate"):
            return {"task_id": "task-1"}
        if method == "GET" and url.endswith("/von/api/task/status/task-1"):
            return {"status": "completed"}
        if method == "GET" and url.endswith("/von/api/task/result/task-1"):
            result_fetch_count += 1
            if result_fetch_count == 1:
                return {
                    "result": {
                        "response": "ok",
                        "request_id": "task-1",
                        "llm_debug": {"request_id": "task-1"},
                    }
                }
            return {
                "result": {
                    "response": "ok",
                    "request_id": "task-1",
                    "llm_debug": {
                        "request_id": "task-1",
                        "turn_execution_diagnostics": {
                            "workflow_routing_diagnostics": {}
                        },
                    },
                }
            }
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    _task_id, payload = sampler._run_generate_background(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5010",
        prompt="Prompt",
        model="ollama/gemma4:e4b",
        gmail_profile=None,
        presenter_mode=False,
        agent_test_selector_replay_mode=None,
        turn_expected_outcome_contract=None,
        timeout_seconds=30.0,
        poll_interval_seconds=0.2,
    )

    assert result_fetch_count == 2
    assert payload["llm_debug"]["turn_execution_diagnostics"] == {
        "workflow_routing_diagnostics": {}
    }


def test_prompt_sampler_uses_embedded_debug_when_history_location_is_missing(
    monkeypatch,
) -> None:
    def fake_request_json(
        _session: object,
        method: str,
        url: str,
        **kwargs: object,
    ) -> dict[str, object]:
        assert method == "GET"
        assert url.endswith("/von/history")
        assert kwargs["params"] == {"session_id": "session-1", "tail_limit": 8}
        return {"history": []}

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    history_location, llm_debug_data = sampler._resolve_turn_debug_data(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5010",
        session_id="session-1",
        request_id="request-1",
        response_text="gmail_api_error",
        generate_payload={
            "llm_debug": {
                "request_id": "request-1",
                "turn_execution_diagnostics": {
                    "workflow_routing_diagnostics": {
                        "dispatch": {
                            "dispatch_workflow_id": "#V#workflow_from_task_result"
                        }
                    }
                },
            }
        },
    )

    assert history_location["source"] == "background_task_result.llm_debug"
    assert history_location["request_id"] == "request-1"
    assert "Could not resolve assistant history location" in history_location[
        "history_lookup_error"
    ]
    assert llm_debug_data["turn_execution_diagnostics"][
        "workflow_routing_diagnostics"
    ]["dispatch"]["dispatch_workflow_id"] == "#V#workflow_from_task_result"
