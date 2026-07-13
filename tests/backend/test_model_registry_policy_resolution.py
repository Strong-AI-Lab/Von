"""Tests for model registry-driven policy resolution."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, cast
from unittest.mock import patch

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _WorkflowModelPolicyState,
)
from src.backend.services.model_registry_service import (
    assess_model_stage_certification,
    build_model_stage_suitability_evidence,
)


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):  # pragma: no cover
        raise AssertionError("Gateway should not be invoked in this test")


def test_policy_candidate_resolves_via_model_registry():
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "planner": {
                    "primary": "#V#openaigpt5_nano20250807",
                    "fallback": [],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    registry_snapshot = {
        "source": "vontology",
        "models": [
            {
                "model_id": "openai:gpt-5-nano-2025-08-07",
                "concept_id": "#V#openaigpt5_nano20250807",
                "registry_entry_id": "#V#openai_gpt5_nano_registry_entry",
                "provider": "OpenAI",
            }
        ],
    }

    candidates = orchestrator._stage_model_candidates(
        stage="planner",
        default_model="gpt-4",
        policy_state=policy_state,
        registry_snapshot=registry_snapshot,
    )

    assert candidates, "Expected at least one candidate"
    assert candidates[0].provider == "openai"
    assert candidates[0].model == "gpt-5-nano-2025-08-07"
    assert candidates[-1].source == "active_llm"


def test_model_registry_snapshot_is_cached_within_ttl(monkeypatch):
    import src.backend.services.model_registry_service as registry_service

    registry_service._MODEL_REGISTRY_SNAPSHOT_CACHE.clear()
    calls = {"graph": 0}

    def _load_graph(**_kwargs):
        calls["graph"] += 1
        return {
            "registry_concept_id": "#V#default_model_registry",
            "models": [
                {
                    "model_id": "openai:gpt-5.4-nano",
                    "provider": "openai",
                }
            ],
        }

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "0")
    monkeypatch.setattr(
        registry_service,
        "_load_registry_from_vontology_graph",
        _load_graph,
    )
    monkeypatch.setattr(
        registry_service,
        "_load_registry_from_vontology_json",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("JSON fallback should not be consulted")
        ),
    )

    first = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")
    second = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")

    assert calls["graph"] == 1
    assert first is second
    assert first["source"] == "vontology_graph"
    registry_service._MODEL_REGISTRY_SNAPSHOT_CACHE.clear()


def _write_registry_disk_cache(
    path,
    *,
    cache_key="en-nz",
    expires_at=None,
    source="vontology_graph",
):
    now = time.time()
    snapshot = {
        "source": source,
        "registry_concept_id": "#V#default_model_registry",
        "models": [
            {
                "model_id": "openai:gpt-5.4-nano",
                "provider": "openai",
            }
        ],
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "model_registry_snapshot_cache.v1",
                "updated_at": now,
                "entries": {
                    cache_key: {
                        "cache_key": cache_key,
                        "preferred_language": "en-NZ",
                        "source": source,
                        "created_at": now,
                        "expires_at": (
                            expires_at if expires_at is not None else now + 60
                        ),
                        "metadata": {
                            "hydrate_duration_ms": 1234,
                        },
                        "snapshot": snapshot,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def test_pytest_does_not_use_live_registry_disk_cache_without_isolated_path(
    tmp_path, monkeypatch
):
    import src.backend.services.model_registry_service as registry_service

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "registry cache isolation")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.delenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        raising=False,
    )

    assert registry_service._registry_snapshot_disk_cache_enabled() is False

    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(tmp_path / "isolated-registry-cache.json"),
    )
    assert registry_service._registry_snapshot_disk_cache_enabled() is True


def test_model_registry_snapshot_uses_valid_disk_cache(tmp_path, monkeypatch):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    expected_snapshot = _write_registry_disk_cache(cache_path)
    registry_service.clear_model_registry_snapshot_caches()

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(
        registry_service,
        "_load_registry_from_vontology_graph",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("graph loader should not run on disk hit")
        ),
    )

    snapshot = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")

    assert snapshot == expected_snapshot
    assert registry_service._MODEL_REGISTRY_SNAPSHOT_CACHE["en-nz"]["snapshot"] is snapshot
    registry_service.clear_model_registry_snapshot_caches()


def test_model_registry_snapshot_ignores_stale_disk_cache(tmp_path, monkeypatch):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    _write_registry_disk_cache(cache_path, expires_at=time.time() - 1)
    registry_service.clear_model_registry_snapshot_caches()
    calls = {"graph": 0}

    def _load_graph(**_kwargs):
        calls["graph"] += 1
        return {
            "registry_concept_id": "#V#default_model_registry",
            "models": [
                {
                    "model_id": "openai:gpt-5.5-nano",
                    "provider": "openai",
                }
            ],
        }

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_graph", _load_graph)
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_json", lambda **_kwargs: None)

    snapshot = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))

    assert calls["graph"] == 1
    assert snapshot["models"][0]["model_id"] == "openai:gpt-5.5-nano"
    entry = persisted["entries"]["en-nz"]
    assert entry["source"] == "vontology_graph"
    assert entry["metadata"]["hydrate_duration_ms"] >= 0
    assert entry["snapshot"]["models"][0]["model_id"] == "openai:gpt-5.5-nano"
    registry_service.clear_model_registry_snapshot_caches()


def test_model_registry_snapshot_ignores_wrong_key_disk_cache(tmp_path, monkeypatch):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    _write_registry_disk_cache(cache_path, cache_key="fr")
    registry_service.clear_model_registry_snapshot_caches()
    calls = {"graph": 0}

    def _load_graph(**_kwargs):
        calls["graph"] += 1
        return {
            "registry_concept_id": "#V#default_model_registry",
            "models": [{"model_id": "openai:gpt-5.5-nano", "provider": "openai"}],
        }

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_graph", _load_graph)
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_json", lambda **_kwargs: None)

    snapshot = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")

    assert calls["graph"] == 1
    assert snapshot["models"][0]["model_id"] == "openai:gpt-5.5-nano"
    registry_service.clear_model_registry_snapshot_caches()


def test_model_registry_snapshot_ignores_invalid_disk_cache(tmp_path, monkeypatch):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    cache_path.write_text("{not valid json", encoding="utf-8")
    registry_service.clear_model_registry_snapshot_caches()
    calls = {"graph": 0}

    def _load_graph(**_kwargs):
        calls["graph"] += 1
        return {
            "registry_concept_id": "#V#default_model_registry",
            "models": [{"model_id": "openai:gpt-5.5-nano", "provider": "openai"}],
        }

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_graph", _load_graph)
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_json", lambda **_kwargs: None)

    snapshot = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")

    assert calls["graph"] == 1
    assert snapshot["models"][0]["model_id"] == "openai:gpt-5.5-nano"
    registry_service.clear_model_registry_snapshot_caches()


def test_model_registry_settings_fallback_is_not_disk_cached(tmp_path, monkeypatch):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    registry_service.clear_model_registry_snapshot_caches()
    calls = {"settings": 0}

    def _build_settings():
        calls["settings"] += 1
        return {
            "source": "settings",
            "models": [{"model_id": "openai:gpt-5.4-nano", "provider": "openai"}],
        }

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_graph", lambda **_kwargs: None)
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_json", lambda **_kwargs: None)
    monkeypatch.setattr(registry_service, "_build_registry_from_settings", _build_settings)

    first = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")
    second = registry_service.get_model_registry_snapshot(preferred_language="en-NZ")

    assert first["source"] == "settings"
    assert second["source"] == "settings"
    assert calls["settings"] == 2
    assert not cache_path.exists()
    registry_service.clear_model_registry_snapshot_caches()


def test_model_registry_disk_cache_write_is_atomic_enough_for_readers(
    tmp_path, monkeypatch
):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    registry_service.clear_model_registry_snapshot_caches()

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(
        registry_service,
        "_load_registry_from_vontology_graph",
        lambda **_kwargs: {
            "registry_concept_id": "#V#default_model_registry",
            "models": [{"model_id": "openai:gpt-5.4-nano", "provider": "openai"}],
        },
    )
    monkeypatch.setattr(registry_service, "_load_registry_from_vontology_json", lambda **_kwargs: None)

    registry_service.get_model_registry_snapshot(preferred_language="en-NZ")

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "model_registry_snapshot_cache.v1"
    assert persisted["entries"]["en-nz"]["cache_key"] == "en-nz"
    assert persisted["entries"]["en-nz"]["snapshot"]["source"] == "vontology_graph"
    assert list(tmp_path.glob(".*.tmp")) == []
    registry_service.clear_model_registry_snapshot_caches()


def test_model_registry_disk_cache_keeps_multiple_language_keys(
    tmp_path, monkeypatch
):
    import src.backend.services.model_registry_service as registry_service

    cache_path = tmp_path / "registry_snapshot.json"
    _write_registry_disk_cache(cache_path, cache_key="")
    registry_service.clear_model_registry_snapshot_caches()

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED", "1")
    monkeypatch.setenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        str(cache_path),
    )
    monkeypatch.setattr(
        registry_service,
        "_load_registry_from_vontology_graph",
        lambda **_kwargs: {
            "registry_concept_id": "#V#default_model_registry",
            "models": [{"model_id": "openai:gpt-5.5-nano", "provider": "openai"}],
        },
    )
    monkeypatch.setattr(
        registry_service,
        "_load_registry_from_vontology_json",
        lambda **_kwargs: None,
    )

    registry_service.get_model_registry_snapshot(preferred_language="en-NZ")
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))

    assert set(persisted["entries"]) == {"", "en-nz"}
    assert persisted["entries"][""]["snapshot"]["models"][0]["model_id"] == (
        "openai:gpt-5.4-nano"
    )
    assert persisted["entries"]["en-nz"]["snapshot"]["models"][0]["model_id"] == (
        "openai:gpt-5.5-nano"
    )
    registry_service.clear_model_registry_snapshot_caches()


def test_policy_candidate_prefers_active_llm_primary_before_enabled_models():
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "classifier": {
                    "primary": "active_llm",
                    "fallback": [],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    with patch(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        return_value=[
            {"provider": "openai", "model": "gpt-4.1-mini"},
            {"provider": "ollama", "model": "granite3.3:2b"},
        ],
    ):
        candidates = orchestrator._stage_model_candidates(
            stage="classifier",
            default_model="gemma4:26b",
            policy_state=policy_state,
            registry_snapshot=None,
        )

    assert candidates, "Expected at least one candidate"
    assert candidates[0].source == "active_llm"
    assert candidates[0].raw == "active_llm"


def test_workflow_specific_policy_candidate_precedes_global_stage_policy():
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "planner": {
                    "primary": "active_llm",
                    "fallback": [],
                }
            },
            "workflows": {
                "#V#specialised_research_workflow": {
                    "stages": {
                        "planner": {
                            "primary": "openai:gpt-5.2-chat-latest",
                            "fallback": ["active_llm"],
                        }
                    }
                }
            },
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    candidates = orchestrator._stage_model_candidates(
        stage="planner",
        default_model="gemma4:26b",
        policy_state=policy_state,
        registry_snapshot=None,
        workflow_id="#V#specialised_research_workflow",
    )

    assert candidates, "Expected at least one candidate"
    assert candidates[0].source == "policy"
    assert candidates[0].provider == "openai"
    assert candidates[0].model == "gpt-5.2-chat-latest"

    metadata = orchestrator._build_stage_model_selection_metadata(
        stage="planner",
        policy_stage="planner",
        selected_candidate=candidates[0],
        default_model="gemma4:26b",
        policy_state=policy_state,
        registry_snapshot=None,
        workflow_id="#V#specialised_research_workflow",
    )

    assert metadata["policy_scope"] == "workflow"
    assert metadata["policy_workflow_id"] == "#V#specialised_research_workflow"
    assert metadata["selection_mode"] == "policy_primary_override"


def test_model_stage_suitability_evidence_blocks_single_case_certification():
    evidence = build_model_stage_suitability_evidence(
        model="gemma4:26b",
        stage="workflow_selector",
        workflow_id="#V#entity_information_retrieval_workflow",
        prompt_id="#V#chat_turn_classifier_prompt",
        replay_set_id="JVNAUTOSCI-1894",
        replay_case_id="represented_self_facts_vs_inferences",
        request_id="request-2090",
        verdict="passed",
        metrics={"structured_output_valid": True},
        promotion_blockers=["single_prompt_replay_evidence_only"],
    )

    assert evidence["schema_version"] == "model_stage_suitability_evidence.v1"
    assert evidence["evidence_type"] == "#V#model_stage_suitability_evidence"
    assert evidence["promotion_eligible"] is False

    decision = assess_model_stage_certification([evidence])

    assert decision["schema_version"] == "model_stage_certification_decision.v1"
    assert decision["promotion_authorised"] is False
    assert "insufficient_distinct_replay_cases" in decision["promotion_blockers"]
    assert "evidence_entry_promotion_blockers_present" in (
        decision["promotion_blockers"]
    )


def test_model_stage_certification_requires_all_evidence_to_pass():
    entries = [
        build_model_stage_suitability_evidence(
            model="gpt-5.5",
            stage="workflow_selector",
            replay_set_id="JVNAUTOSCI-1894",
            replay_case_id="case-a",
            verdict="passed",
            metrics={},
        ),
        build_model_stage_suitability_evidence(
            model="gpt-5.5",
            stage="workflow_selector",
            replay_set_id="JVNAUTOSCI-1894",
            replay_case_id="case-b",
            verdict="failed",
            metrics={},
        ),
    ]

    decision = assess_model_stage_certification(entries)

    assert decision["distinct_replay_case_count"] == 2
    assert decision["promotion_authorised"] is False
    assert decision["verdict_counts"] == {"passed": 1, "failed": 1}
    assert "non_passing_evidence_present" in decision["promotion_blockers"]
