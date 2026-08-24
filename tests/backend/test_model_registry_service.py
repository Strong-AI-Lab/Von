"""Tests for graph-backed model registry and runtime parameter policies."""

from __future__ import annotations

import src.backend.services  # noqa: F401


def _build_text_row_lookup(text_map):
    def _get_text_rows(concept_id, *, predicate=None, limit=50):
        values = text_map.get((concept_id, predicate), [])
        return [{"text": value} for value in values[:limit]]

    return _get_text_rows


def test_batched_graph_loader_preserves_registry_semantics_with_bounded_reads(
    monkeypatch,
) -> None:
    import src.backend.services.model_registry_service as mod
    from src.backend.services import concept_service, text_value_service

    relation_map = {
        ("#V#default_model_registry", mod.PRED_HAS_MODEL_ENTRY): [
            "#V#test_registry_entry"
        ],
        ("#V#test_registry_entry", mod.PRED_REFERS_TO_MODEL): ["#V#test_model"],
        ("#V#test_registry_entry", mod.PRED_HAS_MODEL_API_PROFILE): ["#V#test_profile"],
        ("#V#test_model", mod.PRED_HAS_PROVIDER): ["#V#test_provider"],
        ("#V#test_profile", mod.PRED_HAS_MODEL_PARAMETER_CONSTRAINT): [
            "#V#test_constraint"
        ],
        ("#V#test_constraint", mod.PRED_CONSTRAINS_MODEL_PARAMETER): [
            "#V#temperature_parameter"
        ],
    }
    concept_docs: dict[str, dict] = {}
    for (subject_id, predicate), targets in relation_map.items():
        concept_docs.setdefault(
            subject_id,
            {"concept_id": subject_id, "relationships": {}},
        )["relationships"][predicate] = targets
        for target in targets:
            concept_docs.setdefault(
                target,
                {"concept_id": target, "relationships": {}},
            )

    text_map = {
        ("#V#test_provider", "hasName"): ["OpenAI Provider"],
        ("#V#test_model", "hasName"): ["openai:test-model", "test-model"],
        ("#V#test_registry_entry", mod.PRED_HAS_MODEL_ID): ["test-model"],
        ("#V#test_profile", mod.PRED_HAS_API_SURFACE): ["responses"],
        ("#V#test_profile", mod.PRED_HAS_STRUCTURED_TOOL_CALLING): ["required"],
        ("#V#test_constraint", mod.PRED_HAS_PARAMETER_ACTION): ["omit"],
    }
    concept_read_batches: list[tuple[str, ...]] = []
    text_read_calls = 0

    def _bulk_concepts(concept_ids):
        batch = tuple(dict.fromkeys(concept_ids))
        concept_read_batches.append(batch)
        return {
            concept_id: concept_docs[concept_id]
            for concept_id in batch
            if concept_id in concept_docs
        }

    def _bulk_text(subject_ids, *, predicates, **_kwargs):
        nonlocal text_read_calls
        text_read_calls += 1
        predicate_set = set(predicates)
        return {
            subject_id: [
                {"predicate": predicate, "text": value}
                for (candidate_id, predicate), values in text_map.items()
                if candidate_id == subject_id and predicate in predicate_set
                for value in values
            ]
            for subject_id in dict.fromkeys(subject_ids)
        }

    monkeypatch.setattr(
        concept_service,
        "get_concepts_by_concept_ids_exact",
        _bulk_concepts,
    )
    monkeypatch.setattr(text_value_service, "get_texts_for_concepts", _bulk_text)

    snapshot = mod._load_registry_from_vontology_graph_batched()

    assert snapshot is not None
    assert snapshot["read_strategy"] == "batched_graph"
    assert snapshot["read_phases"] == 6
    assert len(concept_read_batches) == 5
    assert text_read_calls == 1
    assert snapshot["models"] == [
        {
            "model_id": "test-model",
            "model_aliases": ["openai:test-model", "test-model"],
            "provider": "openai",
            "locality": "external",
            "concept_id": "#V#test_model",
            "registry_entry_id": "#V#test_registry_entry",
            "api_profiles": [
                {
                    "profile_concept_id": "#V#test_profile",
                    "api_surface": "responses",
                    "structured_tool_calling": "required",
                    "tool_continuation_mode": None,
                    "response_storage_policy": None,
                    "connection_id": None,
                    "deployment_id": None,
                    "parameter_constraints": [
                        {
                            "constraint_concept_id": "#V#test_constraint",
                            "parameter_concept_id": "#V#temperature_parameter",
                            "parameter": "temperature",
                            "action": "omit",
                            "fixed_value": None,
                            "allowed_values": [],
                            "profile_concept_id": "#V#test_profile",
                        }
                    ],
                }
            ],
        }
    ]

    del concept_docs["#V#test_registry_entry"]["relationships"][
        mod.PRED_HAS_MODEL_API_PROFILE
    ]
    text_map[
        ("#V#test_registry_entry", mod.PRED_HAS_MODEL_API_PROFILE)
    ] = ["#V#test_profile"]

    assert mod._load_registry_from_vontology_graph_batched() is None


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
        ("#V#openai_gpt5_mini_registry_entry", mod.PRED_HAS_MODEL_ID): ["gpt-5-mini"],
        (
            "#V#openai_gpt5_mini_registry_entry",
            mod.PRED_HAS_MODEL_PRICING_JSON,
        ): [
            '{"schema_version":"llm_model_pricing.v1",'
            '"version":"test-v1","source":"test",'
            '"effective_at_utc":"2026-08-05T00:00:00Z",'
            '"model_id":"gpt-5-mini","currency":"USD",'
            '"unit_tokens":1000000,"rates":{'
            '"input_tokens":1.0,"output_tokens":4.0}}'
        ],
        (
            "#V#openai_gpt5_mini_registry_entry",
            mod.PRED_HAS_MODEL_CAPABILITIES_JSON,
        ): [
            (
                '{"schema_version":"llm_model_capabilities.v1",'
                '"model_id":"gpt-5-mini","input_token_limit":400000,'
                '"output_token_limit":128000,'
                '"features":{"function_calling":"supported"}}'
            )
        ],
        (
            "#V#openai_gpt5_mini_chat_completions_profile",
            mod.PRED_HAS_API_SURFACE,
        ): ["chat_completions"],
        (
            "#V#openai_gpt5_mini_chat_completions_profile",
            mod.PRED_HAS_STRUCTURED_TOOL_CALLING,
        ): ["supported"],
        (
            "#V#openai_gpt5_mini_chat_completions_profile",
            mod.PRED_HAS_TOOL_CONTINUATION_MODE,
        ): ["stateless"],
        (
            "#V#openai_gpt5_mini_chat_completions_profile",
            mod.PRED_HAS_RESPONSE_STORAGE_POLICY,
        ): ["disabled"],
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
    assert model_entry["pricing"]["schema_version"] == "llm_model_pricing.v1"
    assert model_entry["pricing"]["rates"]["output_tokens"] == 4.0
    assert model_entry["capabilities"]["schema_version"] == (
        "llm_model_capabilities.v1"
    )
    assert model_entry["capabilities"]["input_token_limit"] == 400000
    assert model_entry["capabilities"]["features"]["function_calling"] == (
        "supported"
    )

    profile = model_entry["api_profiles"][0]
    assert profile["api_surface"] == "chat_completions"
    assert profile["structured_tool_calling"] == "supported"
    assert profile["tool_continuation_mode"] == "stateless"
    assert profile["response_storage_policy"] == "disabled"

    constraint = profile["parameter_constraints"][0]
    assert constraint["parameter_concept_id"] == "#V#temperature_parameter"
    assert constraint["parameter"] == "temperature"
    assert constraint["action"] == "omit"
    assert constraint["fixed_value"] == "1.0"
    assert constraint["allowed_values"] == ["1.0"]
    assert constraint["profile_concept_id"] == (
        "#V#openai_gpt5_mini_chat_completions_profile"
    )


def test_resolve_model_api_profiles_preserves_provenance_and_family_match(
    monkeypatch,
) -> None:
    import src.backend.services.model_registry_service as mod

    snapshot = {
        "source": "vontology_graph",
        "registry_concept_id": "#V#default_model_registry",
        "models": [
            {
                "model_id": "deployment-family",
                "provider": "openai",
                "concept_id": "#V#deployment_family",
                "registry_entry_id": "#V#deployment_family_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#deployment_responses_profile",
                        "api_surface": "responses",
                        "structured_tool_calling": "required",
                        "tool_continuation_mode": "stateless",
                        "response_storage_policy": "disabled",
                        "parameter_constraints": [],
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

    resolved = mod.resolve_model_api_profiles(
        model="deployment-family-blue",
        provider="openai",
    )

    assert resolved is not None
    assert resolved["source"] == "vontology_graph"
    assert resolved["registry_concept_id"] == "#V#default_model_registry"
    assert resolved["registry_entry_id"] == "#V#deployment_family_registry_entry"
    assert resolved["concept_id"] == "#V#deployment_family"
    assert resolved["api_profiles"][0]["profile_concept_id"] == (
        "#V#deployment_responses_profile"
    )


def test_resolve_model_api_profiles_prefers_exact_entry_over_earlier_family(
    monkeypatch,
) -> None:
    import src.backend.services.model_registry_service as mod

    snapshot = {
        "source": "vontology_graph",
        "registry_concept_id": "#V#default_model_registry",
        "models": [
            {
                "model_id": "gpt-5",
                "provider": "openai",
                "registry_entry_id": "#V#gpt_5_family_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#gpt_5_family_chat_profile",
                        "api_surface": "chat_completions",
                    }
                ],
            },
            {
                "model_id": "gpt-5.6-luna",
                "provider": "openai",
                "registry_entry_id": "#V#gpt_5_6_luna_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#gpt_5_6_luna_responses_profile",
                        "api_surface": "responses",
                    }
                ],
            },
        ],
    }
    monkeypatch.setattr(
        mod,
        "get_model_registry_snapshot",
        lambda *, preferred_language=None: snapshot,
    )

    resolved = mod.resolve_model_api_profiles(
        model="gpt-5.6-luna",
        provider="openai",
    )

    assert resolved is not None
    assert resolved["registry_entry_id"] == "#V#gpt_5_6_luna_entry"
    assert resolved["api_profiles"][0]["profile_concept_id"] == (
        "#V#gpt_5_6_luna_responses_profile"
    )


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


def test_runtime_parameter_policy_is_scoped_to_selected_profile(monkeypatch) -> None:
    import src.backend.services.model_registry_service as mod

    snapshot = {
        "source": "vontology_graph",
        "models": [
            {
                "model_id": "same-surface-deployment",
                "provider": "openai",
                "registry_entry_id": "#V#same_surface_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#connection_a_responses",
                        "api_surface": "responses",
                        "parameter_constraints": [
                            {
                                "parameter": "temperature",
                                "action": "omit",
                            }
                        ],
                    },
                    {
                        "profile_concept_id": "#V#connection_b_responses",
                        "api_surface": "responses",
                        "parameter_constraints": [
                            {
                                "parameter": "temperature",
                                "action": "fixed_value",
                                "fixed_value": "0.25",
                            }
                        ],
                    },
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
            model="same-surface-deployment",
            provider="openai",
            parameter="temperature",
            value=0.7,
            api_surface="responses",
            profile_concept_id="#V#connection_a_responses",
        )
        is None
    )
    assert (
        mod.sanitise_model_parameter_value(
            model="same-surface-deployment",
            provider="openai",
            parameter="temperature",
            value=0.7,
            api_surface="responses",
            profile_concept_id="#V#connection_b_responses",
        )
        == 0.25
    )
    assert (
        mod.resolve_model_parameter_policy(
            model="same-surface-deployment",
            provider="openai",
            parameter="temperature",
            api_surface="responses",
            profile_concept_id="#V#connection_b_responses",
        )["profile_concept_id"]
        == "#V#connection_b_responses"
    )


def test_runtime_parameter_policy_prefers_exact_entry_over_family(monkeypatch) -> None:
    import src.backend.services.model_registry_service as mod

    snapshot = {
        "source": "vontology_graph",
        "models": [
            {
                "model_id": "gpt-5",
                "provider": "openai",
                "registry_entry_id": "#V#gpt_5_family_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#gpt_5_family_responses",
                        "api_surface": "responses",
                        "parameter_constraints": [
                            {"parameter": "temperature", "action": "omit"}
                        ],
                    }
                ],
            },
            {
                "model_id": "gpt-5.6-terra",
                "provider": "openai",
                "registry_entry_id": "#V#terra_exact_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#terra_exact_responses",
                        "api_surface": "responses",
                        "parameter_constraints": [
                            {
                                "parameter": "temperature",
                                "action": "fixed_value",
                                "fixed_value": "0.4",
                            }
                        ],
                    }
                ],
            },
        ],
    }
    monkeypatch.setattr(
        mod,
        "get_model_registry_snapshot",
        lambda *, preferred_language=None: snapshot,
    )

    policy = mod.resolve_model_parameter_policy(
        model="gpt-5.6-terra",
        provider="openai",
        parameter="temperature",
        api_surface="responses",
        profile_concept_id="#V#terra_exact_responses",
    )

    assert policy is not None
    assert policy["registry_entry_id"] == "#V#terra_exact_entry"
    assert policy["action"] == "fixed_value"


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
