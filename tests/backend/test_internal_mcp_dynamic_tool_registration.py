from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.dynamic_tool_loader import (
    get_dynamic_tool_registration_status,
    invalidate_dynamic_tool_spec_cache,
    load_dynamic_method_definitions,
    reset_dynamic_tool_registration_status,
)
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema, SchemaValidationError
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _set_dynamic_tool_docs(monkeypatch, docs):
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find",
        lambda *_args, **_kwargs: docs,
    )
    invalidate_dynamic_tool_spec_cache()
    reset_dynamic_tool_registration_status()


def test_dynamic_loader_supports_fixed_payload_with_gateway_invoke(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_echo_proxy",
                "attributes": {
                    "mcp_tool_name": "echo_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "echo_base",
                    "dynamic_fixed_payload": {
                        "name": "fixed-name",
                        "fixed": "forced",
                    },
                },
            }
        ],
    )

    def _echo_handler(**kwargs):
        return {
            "success": True,
            "name": kwargs.get("name"),
            "value": kwargs.get("value"),
            "fixed": kwargs.get("fixed"),
        }

    base_definition = MethodDefinition(
        name="echo_base",
        handler=_echo_handler,
        input_schema=Schema(
            required={"name": str},
            optional={"value": str, "fixed": str},
            allow_unknown=False,
            description="Echo payload for dynamic proxy tests.",
        ),
        output_schema=Schema(
            required={"success": bool},
            optional={
                "name": (str, type(None)),
                "value": (str, type(None)),
                "fixed": (str, type(None)),
            },
            allow_unknown=False,
            description="Echo output.",
        ),
        category="read",
        description="Echo base tool.",
        ordinary_turn_excluded_reason="operator_only_test_surface",
    )

    load_result = load_dynamic_method_definitions(
        base_definitions={"echo_base": base_definition},
        protected_method_names={"echo_base"},
    )
    assert len(load_result.definitions) == 1
    dynamic_definition = load_result.definitions[0]
    assert dynamic_definition.name == "echo_proxy"
    assert dynamic_definition.input_schema.required == {}
    assert "name" not in dynamic_definition.input_schema.optional
    assert (
        dynamic_definition.ordinary_turn_excluded_reason == "operator_only_test_surface"
    )

    catalogue = MethodCatalogue()
    catalogue.register(base_definition)
    catalogue.register(dynamic_definition)
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke("echo_proxy", {"value": "hello"})
    assert result.payload["success"] is True
    assert result.payload["name"] == "fixed-name"
    assert result.payload["fixed"] == "forced"
    assert result.payload["value"] == "hello"

    try:
        gateway.invoke("echo_proxy", {"value": "hello", "name": "attempted-override"})
        assert False, "Expected SchemaValidationError for fixed key override attempt."
    except SchemaValidationError:
        pass


def test_dynamic_proxy_preserves_target_timing_policy(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_timing_proxy",
                "attributes": {
                    "mcp_tool_name": "timing_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "timing_base",
                },
            }
        ],
    )
    base_definition = MethodDefinition(
        name="timing_base",
        handler=lambda: {"success": True},
        input_schema=Schema(allow_unknown=False),
        category="write",
        successful_duration_bootstrap_sec=12.0,
    )

    definitions = load_dynamic_method_definitions(
        base_definitions={"timing_base": base_definition},
        protected_method_names={"timing_base"},
    ).definitions

    assert len(definitions) == 1
    proxy = definitions[0]
    assert proxy.successful_duration_bootstrap_sec == 12.0
    assert proxy.hard_timeout_enabled is False
    assert proxy.resolved_advisory_timeout(InternalMCPTransport()) == 15.0
    assert proxy.resolved_timeout(InternalMCPTransport()) is None


def test_dynamic_timeout_override_is_advisory_not_a_new_hard_boundary(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_timing_proxy",
                "attributes": {
                    "mcp_tool_name": "timing_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "timing_base",
                    "dynamic_timeout_sec": 9.0,
                },
            }
        ],
    )
    base_definition = MethodDefinition(
        name="timing_base",
        handler=lambda: {"success": True},
        input_schema=Schema(allow_unknown=False),
        category="write",
    )

    definitions = load_dynamic_method_definitions(
        base_definitions={"timing_base": base_definition},
        protected_method_names={"timing_base"},
    ).definitions

    assert len(definitions) == 1
    proxy = definitions[0]
    assert proxy.hard_timeout_enabled is False
    assert proxy.resolved_advisory_timeout(InternalMCPTransport()) == 9.0
    assert proxy.resolved_timeout(InternalMCPTransport()) is None


def test_dynamic_proxy_preserves_unfixed_target_schema_semantics(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_schema_proxy",
                "attributes": {
                    "mcp_tool_name": "schema_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "schema_base",
                    "dynamic_fixed_payload": {"mode": "safe"},
                },
            }
        ],
    )

    base_definition = MethodDefinition(
        name="schema_base",
        handler=lambda **kwargs: {"success": True, **kwargs},
        input_schema=Schema(
            required={"target": str, "mode": str},
            optional={"filters": list, "records": list, "scope": str},
            allow_unknown=False,
            aliases={
                "target_id": "target",
                "mode_alias": "mode",
            },
            batch_propagated_fields=("target", "mode", "filters"),
            enum_values={
                "mode": ("safe", "unsafe"),
                "scope": ("brief", "full"),
            },
            scalar_source_fields={
                "target": ("concept_id",),
                "mode": ("value",),
            },
            comma_separated_list_fields=("filters",),
            array_length_constraints={"filters": (1, 3)},
            array_item_schemas={
                "records": Schema(required={"field": str}, allow_unknown=False)
            },
        ),
        category="read",
    )

    load_result = load_dynamic_method_definitions(
        base_definitions={"schema_base": base_definition},
        protected_method_names={"schema_base"},
    )

    assert len(load_result.definitions) == 1
    dynamic_definition = load_result.definitions[0]
    schema = dynamic_definition.input_schema
    assert schema.aliases == {"target_id": "target"}
    assert schema.batch_propagated_fields == ("target", "filters")
    assert schema.enum_values == {"scope": ("brief", "full")}
    assert schema.scalar_source_fields == {"target": ("concept_id",)}
    assert schema.comma_separated_list_fields == ("filters",)
    assert schema.array_length_constraints == {"filters": (1, 3)}
    assert schema.array_item_schemas["records"].required == {"field": str}

    catalogue = MethodCatalogue()
    catalogue.register(base_definition)
    catalogue.register(dynamic_definition)
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "schema_proxy",
        {
            "target_id": {"concept_id": "#V#project"},
            "filters": "active, represented",
            "scope": "brief",
        },
    )
    assert result.payload == {
        "success": True,
        "target": "#V#project",
        "filters": ["active", "represented"],
        "scope": "brief",
        "mode": "safe",
    }


def test_dynamic_proxy_cannot_fix_a_trusted_bound_argument(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_mail_proxy",
                "attributes": {
                    "mcp_tool_name": "mail_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "mail_base",
                    "dynamic_fixed_payload": {
                        "profile": "globally-configured-profile",
                    },
                },
            }
        ],
    )
    base_definition = MethodDefinition(
        name="mail_base",
        handler=lambda **kwargs: {"success": True, **kwargs},
        input_schema=Schema(
            required={"profile": str},
            optional={"query": str},
            allow_unknown=False,
        ),
        category="read",
        ordinary_turn_trusted_argument_bindings={
            "profile": "gmail_profile",
        },
    )

    load_result = load_dynamic_method_definitions(
        base_definitions={"mail_base": base_definition},
        protected_method_names={"mail_base"},
    )

    assert load_result.definitions == ()
    assert load_result.status["failed_count"] == 1
    failure = load_result.status["failed"][0]
    assert failure["reason"] == "fixed_payload_overrides_trusted_argument"
    assert failure["conflicting_argument_names"] == ["profile"]


def test_dynamic_proxy_cannot_bypass_ordinary_turn_fixed_argument(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_unsafe_reset_proxy",
                "attributes": {
                    "mcp_tool_name": "unsafe_reset_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "diagnostics_base",
                    "dynamic_fixed_payload": {"clear": True},
                },
            },
            {
                "concept_id": "#V#dynamic_safe_reset_proxy",
                "attributes": {
                    "mcp_tool_name": "safe_reset_proxy",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "diagnostics_base",
                    "dynamic_fixed_payload": {"reset": False},
                },
            },
        ],
    )
    base_definition = MethodDefinition(
        name="diagnostics_base",
        handler=lambda **kwargs: {"success": True, **kwargs},
        input_schema=Schema(
            optional={"reset": bool, "query": str},
            aliases={"clear": "reset"},
            allow_unknown=True,
        ),
        category="read",
        ordinary_turn_fixed_arguments={"reset": False},
    )

    load_result = load_dynamic_method_definitions(
        base_definitions={"diagnostics_base": base_definition},
        protected_method_names={"diagnostics_base"},
    )

    definitions = {
        definition.name: definition for definition in load_result.definitions
    }
    assert (
        definitions["unsafe_reset_proxy"].ordinary_turn_excluded_reason
        == "dynamic_proxy_overrides_ordinary_turn_fixed_argument"
    )
    assert definitions["safe_reset_proxy"].ordinary_turn_excluded_reason is None
    assert definitions["safe_reset_proxy"].ordinary_turn_fixed_arguments == {
        "reset": False
    }


def test_build_default_catalogue_registers_enabled_dynamic_tool(monkeypatch):
    from src.backend.services import settings_service

    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_get_context_alias",
                "attributes": {
                    "mcp_tool_name": "get_context_dynamic",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "get_context",
                },
            }
        ],
    )
    # This is a dynamic-registration test, not an integration test for the
    # settings store. Keep the proxied read deterministic when Mongo is absent
    # so the MCP hard deadline is tested independently.
    monkeypatch.setattr(
        settings_service,
        "resolve_llm_setting",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        settings_service,
        "get_preferred_language",
        lambda: "en-NZ",
    )
    monkeypatch.setattr(
        settings_service,
        "get_setting",
        lambda _name: None,
    )

    catalogue = build_default_catalogue()
    assert "get_context_dynamic" in set(catalogue.list_methods())

    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke("get_context_dynamic", {})
    assert result.payload.get("language")

    diagnostics = gateway.get_diagnostics()
    registration = diagnostics.get("dynamic_tool_registration") or {}
    assert registration.get("loaded_count") == 1
    assert registration.get("failed_count") == 0
    loaded_entries = registration.get("loaded")
    assert isinstance(loaded_entries, list)
    assert loaded_entries
    assert loaded_entries[0].get("tool_name") == "get_context_dynamic"


def test_dynamic_tool_with_invalid_spec_is_rejected_and_reported(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_invalid_missing_target",
                "attributes": {
                    "mcp_tool_name": "invalid_dynamic_tool",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                },
            }
        ],
    )

    catalogue = build_default_catalogue()
    assert "invalid_dynamic_tool" not in set(catalogue.list_methods())

    status = get_dynamic_tool_registration_status()
    assert status.get("failed_count") == 1
    failed_entries = status.get("failed")
    assert isinstance(failed_entries, list)
    assert failed_entries
    assert failed_entries[0].get("reason") == "missing_target_tool_name"


def test_dynamic_tool_cannot_override_builtin_name(monkeypatch):
    _set_dynamic_tool_docs(
        monkeypatch,
        [
            {
                "concept_id": "#V#dynamic_conflict_get_context",
                "attributes": {
                    "mcp_tool_name": "get_context",
                    "dynamic_registration_enabled": True,
                    "dynamic_registration_approved": True,
                    "dynamic_target_tool_name": "get_context",
                },
            }
        ],
    )

    catalogue = build_default_catalogue()
    methods = set(catalogue.list_methods())
    assert "get_context" in methods

    status = get_dynamic_tool_registration_status()
    assert status.get("loaded_count") == 0
    assert status.get("failed_count") == 1
    failed_entries = status.get("failed")
    assert isinstance(failed_entries, list)
    assert failed_entries
    assert failed_entries[0].get("reason") == "protected_name_conflict"
