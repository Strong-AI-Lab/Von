from __future__ import annotations

from types import SimpleNamespace

from src.backend.integrations.internal_mcp import orchestrator as orchestrator_module
from src.backend.workflows import model_execution_budget_policy as policy_module


def test_candidate_model_concept_ids_include_ollama_gemma_slug() -> None:
    assert policy_module.candidate_model_concept_ids(
        provider="ollama",
        model="gemma4:26b",
    ) == (
        "#V#ollama_model_gemma4_26b",
        "#V#ollama_gemma4_26b",
        "#V#model_gemma4_26b",
        "#V#gemma4_26b_model",
        "#V#gemma4_26b",
    )


def test_normalise_model_execution_budget_policy_clamps_values() -> None:
    policy = policy_module.normalise_model_execution_budget_policy(
        {
            "schema": "model_execution_budget_policy.v1",
            "cost_class": "local_free",
            "locality": "local",
            "latency_class": "slow_local",
            "conversation_turn_llm_timeout_sec": 900,
            "completion_gate_loop_max_elapsed_ms": 900_000,
            "completion_gate_loop_max_attempts": 99,
            "completion_gate_loop_no_progress_limit": 99,
        },
        model_concept_id="#V#gemma4_26b",
        source_predicate="#V#has_model_execution_budget_policy",
    )

    assert policy is not None
    assert policy.cost_class == "local_free"
    assert policy.locality == "local"
    assert policy.latency_class == "slow_local"
    assert policy.conversation_turn_llm_timeout_sec == 600.0
    assert policy.completion_gate_loop_max_elapsed_ms == 600_000
    assert policy.completion_gate_loop_max_attempts == 20
    assert policy.completion_gate_loop_no_progress_limit == 10


def test_resolve_model_execution_budget_policy_reads_existing_candidate(
    monkeypatch,
) -> None:
    policy_module.clear_model_execution_budget_policy_cache()
    seen: list[str] = []

    def _concept_exists(concept_id: str) -> bool:
        seen.append(concept_id)
        return concept_id == "#V#gemma4_26b"

    def _load_policy_for_concept(concept_id: str):
        assert concept_id == "#V#gemma4_26b"
        return policy_module.normalise_model_execution_budget_policy(
            {"completion_gate_loop_max_elapsed_ms": 600_000},
            model_concept_id=concept_id,
            source_predicate="#V#has_model_execution_budget_policy",
        )

    monkeypatch.setattr(policy_module, "_concept_exists", _concept_exists)
    monkeypatch.setattr(policy_module, "_load_policy_for_concept", _load_policy_for_concept)

    policy = policy_module.resolve_model_execution_budget_policy(
        provider="ollama",
        model="gemma4:26b",
    )

    assert policy is not None
    assert policy.model_concept_id == "#V#gemma4_26b"
    assert policy.completion_gate_loop_max_elapsed_ms == 600_000
    assert "#V#gemma4_26b" in seen
    policy_module.clear_model_execution_budget_policy_cache()


def test_model_policy_timeout_used_when_settings_and_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(orchestrator_module, "get_model_llm_timeout", lambda *_args: None)
    policy = policy_module.normalise_model_execution_budget_policy(
        {"conversation_turn_llm_timeout_sec": 600},
        model_concept_id="#V#gemma4_26b",
        source_predicate="#V#has_model_execution_budget_policy",
    )

    timeout = orchestrator_module._resolve_model_llm_timeout_override_sec(
        llm_client=SimpleNamespace(__class__=type("OllamaLLM", (), {})),
        model="gemma4:26b",
        model_budget_policy=policy,
    )

    assert timeout == 600.0


def test_env_timeout_precedes_model_policy(monkeypatch) -> None:
    monkeypatch.setenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC", "120")
    monkeypatch.setattr(orchestrator_module, "get_model_llm_timeout", lambda *_args: None)
    policy = policy_module.normalise_model_execution_budget_policy(
        {"conversation_turn_llm_timeout_sec": 600},
        model_concept_id="#V#gemma4_26b",
        source_predicate="#V#has_model_execution_budget_policy",
    )

    timeout = orchestrator_module._resolve_model_llm_timeout_override_sec(
        llm_client=SimpleNamespace(),
        model="gemma4:26b",
        model_budget_policy=policy,
    )

    assert timeout == 120.0
