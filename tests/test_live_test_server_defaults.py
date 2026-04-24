from __future__ import annotations

from scripts import live_test_server_defaults as defaults
from scripts import run_live_arxiv_ingestion_workflow_test as arxiv_test
from scripts import run_live_kb_tool_prompt_sampler as sampler


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
