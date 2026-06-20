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

    assert params["supported"] is True
    assert params["source"] == "provider_default_metadata"
    assert "low" in params["allowed_values"]
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

    assert capability["parameters"]["reasoning_effort"] == {
        "supported": True,
        "allowed_values": ["low"],
        "fixed_value": None,
        "read_only": False,
        "source": "vontology_graph",
    }
