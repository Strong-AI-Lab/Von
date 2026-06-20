"""Tests for graph-backed model registry and runtime parameter policies."""

from __future__ import annotations

import src.backend.services  # noqa: F401


def _build_text_row_lookup(text_map):
    def _get_text_rows(concept_id, *, predicate=None, limit=50):
        values = text_map.get((concept_id, predicate), [])
        return [{"text": value} for value in values[:limit]]

    return _get_text_rows


def test_get_model_registry_snapshot_prefers_graph_and_exposes_constraints(
    monkeypatch,
) -> None:
    import src.backend.services.model_registry_service as mod

    resolution_map = {
        "default_model_registry": "#V#default_model_registry",
    }
    relation_map = {
        ("#V#default_model_registry", mod.PRED_HAS_MODEL_ENTRY): [
            "#V#openai_gpt5_mini_registry_entry"
        ],
        ("#V#openai_gpt5_mini_registry_entry", mod.PRED_REFERS_TO_MODEL): [
            "#V#openai_gpt_5_mini"
        ],
        ("#V#openai_gpt_5_mini", mod.PRED_HAS_PROVIDER): ["#V#openai_provider"],
        ("#V#openai_gpt5_mini_registry_entry", mod.PRED_HAS_MODEL_API_PROFILE): [
            "#V#openai_gpt5_mini_chat_completions_profile"
        ],
        (
            "#V#openai_gpt5_mini_chat_completions_profile",
            mod.PRED_HAS_MODEL_PARAMETER_CONSTRAINT,
        ): ["#V#openai_gpt5_mini_temperature_omit_constraint"],
        (
            "#V#openai_gpt5_mini_temperature_omit_constraint",
            mod.PRED_CONSTRAINS_MODEL_PARAMETER,
        ): ["#V#temperature_parameter"],
    }
    text_map = {
        ("#V#openai_provider", "hasName"): ["OpenAI Provider"],
        ("#V#openai_gpt_5_mini", "hasName"): [
            "openai:gpt-5-mini",
            "gpt-5-mini",
        ],
        ("#V#openai_gpt5_mini_registry_entry", mod.PRED_HAS_MODEL_ID): [
            "gpt-5-mini"
        ],
        (
            "#V#openai_gpt5_mini_chat_completions_profile",
            mod.PRED_HAS_API_SURFACE,
        ): ["chat_completions"],
        (
            "#V#openai_gpt5_mini_temperature_omit_constraint",
            mod.PRED_HAS_PARAMETER_ACTION,
        ): ["omit"],
        (
            "#V#openai_gpt5_mini_temperature_omit_constraint",
            mod.PRED_HAS_FIXED_PARAMETER_VALUE,
        ): ["1.0"],
        (
            "#V#openai_gpt5_mini_temperature_omit_constraint",
            mod.PRED_HAS_ALLOWED_PARAMETER_VALUE,
        ): ["1.0"],
    }

    monkeypatch.setattr(
        mod,
        "_resolve_concept_id_by_name",
        lambda name, preferred_language=None: resolution_map.get(name),
    )
    monkeypatch.setattr(
        mod,
        "_get_related_concept_ids",
        lambda concept_id, predicate: relation_map.get((concept_id, predicate), []),
    )
    monkeypatch.setattr(mod, "_get_text_rows", _build_text_row_lookup(text_map))

    snapshot = mod.get_model_registry_snapshot()

    assert snapshot["source"] == "vontology_graph"
    assert snapshot["registry_concept_id"] == "#V#default_model_registry"
    assert len(snapshot["models"]) == 1

    model_entry = snapshot["models"][0]
    assert model_entry["model_id"] == "gpt-5-mini"
    assert model_entry["provider"] == "openai"
    assert model_entry["concept_id"] == "#V#openai_gpt_5_mini"
    assert model_entry["registry_entry_id"] == "#V#openai_gpt5_mini_registry_entry"
    assert "gpt-5-mini" in model_entry["model_aliases"]

    profile = model_entry["api_profiles"][0]
    assert profile["api_surface"] == "chat_completions"

    constraint = profile["parameter_constraints"][0]
    assert constraint["parameter_concept_id"] == "#V#temperature_parameter"
    assert constraint["parameter"] == "temperature"
    assert constraint["action"] == "omit"
    assert constraint["fixed_value"] == "1.0"
    assert constraint["allowed_values"] == ["1.0"]


def test_runtime_parameter_policy_uses_graph_registry_snapshot(monkeypatch) -> None:
    import src.backend.services.model_registry_service as mod

    snapshot = {
        "source": "vontology_graph",
        "models": [
            {
                "model_id": "gpt-5.2",
                "provider": "openai",
                "concept_id": "#V#openai_gpt52",
                "registry_entry_id": "#V#openai_gpt52_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#openai_gpt52_chat_completions_profile",
                        "api_surface": "chat_completions",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": "#V#openai_gpt52_temperature_omit_constraint",
                                "parameter_concept_id": "#V#temperature_parameter",
                                "parameter": "temperature",
                                "action": "omit",
                                "fixed_value": "1.0",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    monkeypatch.setattr(
        mod,
        "get_model_registry_snapshot",
        lambda *, preferred_language=None: snapshot,
    )

    policy = mod.resolve_model_parameter_policy(
        model="gpt-5.2-chat-latest",
        provider="openai",
        parameter="temperature",
        api_surface="chat_completions",
    )

    assert policy is not None
    assert policy["action"] == "omit"
    assert policy["model_id"] == "gpt-5.2"

    assert (
        mod.sanitise_model_parameter_value(
            model="gpt-5.2-chat-latest",
            provider="openai",
            parameter="temperature",
            value=0.7,
            api_surface="chat_completions",
        )
        is None
    )
    assert (
        mod.sanitise_model_parameter_value(
            model="gpt-4",
            provider="openai",
            parameter="temperature",
            value=0.7,
            api_surface="chat_completions",
        )
        == 0.7
    )


def test_runtime_parameter_policy_rejects_values_outside_graph_allowed_set(
    monkeypatch,
) -> None:
    import src.backend.services.model_registry_service as mod

    snapshot = {
        "source": "vontology_graph",
        "models": [
            {
                "model_id": "gpt-5.5",
                "provider": "openai",
                "concept_id": "#V#openai_gpt55",
                "registry_entry_id": "#V#openai_gpt55_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#openai_gpt55_responses_profile",
                        "api_surface": "responses",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": "#V#openai_gpt55_effort_constraint",
                                "parameter_concept_id": "#V#reasoning_effort_parameter",
                                "parameter": "reasoning_effort",
                                "action": "allow",
                                "allowed_values": ["low", "medium"],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    monkeypatch.setattr(
        mod,
        "get_model_registry_snapshot",
        lambda *, preferred_language=None: snapshot,
    )

    assert (
        mod.sanitise_model_parameter_value(
            model="gpt-5.5",
            provider="openai",
            parameter="reasoning_effort",
            value="low",
            api_surface="responses",
        )
        == "low"
    )
    assert (
        mod.sanitise_model_parameter_value(
            model="gpt-5.5",
            provider="openai",
            parameter="reasoning_effort",
            value="xhigh",
            api_surface="responses",
        )
        is None
    )
