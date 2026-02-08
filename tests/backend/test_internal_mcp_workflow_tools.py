from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.integrations.internal_mcp.workflow_surface_capabilities import (
    tracked_workflow_surface_tool_names,
)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_workflow_list_definitions_exists_and_returns_data():
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_list_definitions" in methods

    handler = catalogue.get("workflow_list_definitions").handler
    result = handler(limit=10)

    assert result.get("success") is True
    assert "definitions" in result
    assert "count" in result
    assert "parity_inventory" in result
    assert "baseline_telemetry" in result
    assert "capability_matrix" in result
    assert result["count"] >= 0

    # Check default workflows are present (from registry)
    def_ids = [d["workflow_id"] for d in result["definitions"]]
    # We expect durable workflows to be registered by the factory
    # (rag_sync_workflow is registered in build_durable_workflow_registry)
    assert "#V#rag_text_relation_sync_workflow" in def_ids
    assert "#V#generate_considerations_workflow" in def_ids

    parity_inventory = result["parity_inventory"]
    assert isinstance(parity_inventory, dict)
    assert "counts" in parity_inventory
    assert "summary_text" in parity_inventory
    assert "diagnostics" in parity_inventory
    assert "parity_policy" in parity_inventory
    diagnostics = parity_inventory["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert "drift_detected" in diagnostics
    assert "severity" in diagnostics
    assert "reason_codes" in diagnostics

    baseline_telemetry = result["baseline_telemetry"]
    assert isinstance(baseline_telemetry, dict)
    assert "workflow_discovery_executable_hit_ratio" in baseline_telemetry
    assert "generic_fallback_mcp_invocations_total" in baseline_telemetry

    capability_matrix = result["capability_matrix"]
    assert isinstance(capability_matrix, dict)
    assert capability_matrix.get("docs_reference")
    surfaces = capability_matrix.get("surfaces") or {}
    assert "internal_mcp_gateway" in surfaces
    assert "vontology_mcp_stdio_server" in surfaces
    assert "vonrag_mcp_stdio_server" in surfaces


def test_workflow_list_definitions_gateway_invoke_success_path():
    gateway = _build_gateway()
    result = gateway.invoke("workflow_list_definitions", {"limit": 10})
    payload = result.payload
    assert payload.get("success") is True
    assert "definitions" in payload
    assert "parity_inventory" in payload
    assert "capability_matrix" in payload


def test_workflow_list_instances_gateway_invoke_error_path():
    gateway = _build_gateway()
    result = gateway.invoke("workflow_list_instances", {"status": "invalid-status"})
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_status"


def test_workflow_mcp_health_check_exists_and_runs():
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_mcp_health_check" in methods

    handler = catalogue.get("workflow_mcp_health_check").handler
    result = handler(include_introspection=False)
    assert result.get("success") is True
    checks = result.get("checks")
    assert isinstance(checks, list)
    assert len(checks) >= 3
    assert all(bool(check.get("ok")) for check in checks)


def test_workflow_mcp_health_check_gateway_invoke_success_path():
    gateway = _build_gateway()
    result = gateway.invoke(
        "workflow_mcp_health_check", {"include_introspection": False}
    )
    payload = result.payload
    assert payload.get("success") is True
    assert "checks" in payload
    assert "capability_matrix" in payload


def test_workflow_surface_capability_tools_exist_in_internal_catalogue():
    methods = set(build_default_catalogue().list_methods())
    tracked = set(tracked_workflow_surface_tool_names())
    missing = sorted(tracked - methods)
    assert not missing, f"Tracked workflow surface tools missing from catalogue: {missing}"
