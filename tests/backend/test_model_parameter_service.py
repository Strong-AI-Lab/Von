from __future__ import annotations


def test_openai_reasoning_capabilities_default_to_fast_provider_metadata(
    monkeypatch,
) -> None:
    import src.backend.services.model_parameter_service as mod

    def fail_registry_lookup(**_kwargs):
        raise AssertionError("default capability lookup should not hydrate registry")

    monkeypatch.setattr(mod, "_registry_parameter_policy", fail_registry_lookup)

    capability = mod.build_model_parameter_capabilities(
        provider="openai",
        model="gpt-5.5",
        api_surface="responses",
    )
    params = capability["parameters"]["reasoning_effort"]

    assert capability["schema"] == "model_parameter_capabilities.v1"
    assert params["supported"] is True
    assert params["source"] == "provider_default_metadata"
    assert "low" in params["allowed_values"]
    assert params["provider_api_mapping"]["responses"] == {
        "type": "nested_object",
        "path": ["reasoning", "effort"],
    }
    assert mod.normalise_model_parameters_for_storage(
        {"reasoning_effort": "low"},
        provider="openai",
        model="gpt-5.5",
    ) == {"reasoning_effort": "low"}


def test_model_parameter_capabilities_can_include_registry_policy(
    monkeypatch,
) -> None:
    import src.backend.services.model_parameter_service as mod

    def registry_lookup(**_kwargs):
        return {
            "action": "allow",
            "allowed_values": ["low"],
            "fixed_value": None,
            "source": "vontology_graph",
        }

    monkeypatch.setattr(mod, "_registry_parameter_policy", registry_lookup)

    capability = mod.build_model_parameter_capabilities(
        provider="openai",
        model="gpt-5.5",
        api_surface="responses",
        include_registry=True,
    )
    params = capability["parameters"]["reasoning_effort"]

    assert params["supported"] is True
    assert params["allowed_values"] == ["low"]
    assert params["fixed_value"] is None
    assert params["read_only"] is False
    assert params["source"] == "vontology_graph"
    assert params["sources"] == ["provider_default_metadata", "vontology_graph"]
    assert params["registry_policy"]["allowed_values"] == ["low"]


def test_model_parameter_registry_override_drives_provider_mapping(monkeypatch) -> None:
    import src.backend.services.model_parameter_service as mod

    def registry_lookup(**_kwargs):
        return {
            "action": "allow",
            "allowed_values": ["future"],
            "fixed_value": None,
            "source": "vontology_graph",
        }

    monkeypatch.setattr(mod, "_registry_parameter_policy", registry_lookup)

    assert mod.normalise_model_parameters_for_storage(
        {"reasoning_effort": "future"},
        provider="openai",
        model="gpt-5.5",
        include_registry=True,
    ) == {"reasoning_effort": "future"}
    assert mod.openai_responses_kwargs_from_model_parameters(
        {"reasoning_effort": "future"},
        model="gpt-5.5",
    ) == {"reasoning": {"effort": "future"}}
    assert mod.openai_chat_completions_kwargs_from_model_parameters(
        {"reasoning_effort": "future"},
        model="gpt-5.5",
    ) == {"reasoning_effort": "future"}


def test_provider_mapping_scopes_registry_policy_to_selected_profile(
    monkeypatch,
) -> None:
    import src.backend.services.model_parameter_service as mod

    profile_ids: list[str | None] = []

    def registry_lookup(**kwargs):
        profile_ids.append(kwargs.get("profile_concept_id"))
        return {
            "action": "allow",
            "allowed_values": ["low"],
            "fixed_value": None,
            "source": "vontology_graph",
        }

    monkeypatch.setattr(mod, "_registry_parameter_policy", registry_lookup)

    assert mod.openai_responses_kwargs_from_model_parameters(
        {"reasoning_effort": "low"},
        model="gpt-5.6-terra",
        profile_concept_id="#V#selected_responses_profile",
    ) == {"reasoning": {"effort": "low"}}
    assert profile_ids
    assert set(profile_ids) == {"#V#selected_responses_profile"}


def test_gemini_37_reasoning_effort_maps_to_supported_thinking_levels(
    monkeypatch,
) -> None:
    import src.backend.services.model_parameter_service as mod

    monkeypatch.setattr(mod, "_registry_parameter_policy", lambda **_kwargs: None)

    capability = mod.build_model_parameter_capabilities(
        provider="gemini",
        model="gemini-3.7-flash",
        api_surface="interactions",
    )
    reasoning = capability["parameters"]["reasoning_effort"]
    assert reasoning["allowed_values"] == ["low", "medium", "high"]
    assert "minimal" not in reasoning["allowed_values"]
    assert reasoning["provider_api_mapping"]["interactions"] == {
        "type": "nested_object",
        "path": ["generation_config", "thinking_level"],
    }
    assert mod.gemini_kwargs_from_model_parameters(
        {"reasoning_effort": "high"},
        model="gemini-3.7-flash",
        api_surface="interactions",
    ) == {"generation_config": {"thinking_level": "high"}}
    assert mod.gemini_kwargs_from_model_parameters(
        {"reasoning_effort": "low"},
        model="gemini-3.7-flash",
        api_surface="gemini_generate_content",
    ) == {"thinking_config": {"thinking_level": "low"}}
    assert (
        mod.normalise_model_parameters_for_storage(
            {"reasoning_effort": "minimal"},
            provider="gemini",
            model="gemini-3.7-flash",
            api_surface="interactions",
        )
        == {}
    )


def test_registry_only_astra_capability_reaches_both_openai_transports(monkeypatch):
    import src.backend.services.model_parameter_service as mod

    policy = {'action': 'allow', 'allowed_values': ['low', 'high'], 'source': 'configured_registry'}
    monkeypatch.setattr(mod, '_registry_parameter_policy', lambda **kw: policy)
    capability = mod.build_model_parameter_capabilities(
        provider='openai', model='gpt-6-astra', include_registry=True,
    )['parameters']['reasoning_effort']
    assert capability['supported'] is True
    assert capability['allowed_values'] == ['low', 'high']
    assert mod.openai_responses_kwargs_from_model_parameters(
        {'reasoning_effort': 'high'}, model='gpt-6-astra'
    ) == {'reasoning': {'effort': 'high'}}
    assert mod.openai_chat_completions_kwargs_from_model_parameters(
        {'reasoning_effort': 'high'}, model='gpt-6-astra'
    ) == {'reasoning_effort': 'high'}
    policy['action'] = 'omit'
    assert mod.openai_responses_kwargs_from_model_parameters(
        {'reasoning_effort': 'high'}, model='gpt-6-astra'
    ) == {}
    monkeypatch.setattr(mod, '_registry_parameter_policy', lambda **kw: None)
    assert not mod.build_model_parameter_capabilities(
        provider='openai', model='gpt-6-astra', include_registry=True,
    )['parameters']['reasoning_effort']['supported']


def test_registry_only_model_without_value_constraint_gets_provider_domain(monkeypatch):
    import src.backend.services.model_parameter_service as mod

    policy = {'action': 'allow', 'source': 'configured_registry'}
    monkeypatch.setattr(mod, '_registry_parameter_policy', lambda **kw: policy)
    capability = mod.build_model_parameter_capabilities(
        provider='openai', model='gpt-6-astra', include_registry=True,
    )['parameters']['reasoning_effort']
    assert capability['allowed_values'] == list(mod.OPENAI_REASONING_EFFORT_VALUES)
    assert capability['sources'] == ['configured_registry']
    policy['fixed_value'] = 'registry-defined-effort'
    assert mod.openai_responses_kwargs_from_model_parameters(
        {'reasoning_effort': 'registry-defined-effort'}, model='gpt-6-astra'
    ) == {'reasoning': {'effort': 'registry-defined-effort'}}
