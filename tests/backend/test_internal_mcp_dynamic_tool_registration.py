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
            optional={"name": (str, type(None)), "value": (str, type(None)), "fixed": (str, type(None))},
            allow_unknown=False,
            description="Echo output.",
        ),
        category="read",
        description="Echo base tool.",
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


def test_build_default_catalogue_registers_enabled_dynamic_tool(monkeypatch):
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
