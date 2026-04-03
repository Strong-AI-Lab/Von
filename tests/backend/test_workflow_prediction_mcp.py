from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def test_workflow_build_prediction_envelope_invokes_service(monkeypatch):
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_prediction_service.build_workflow_prediction_envelope",
        lambda **kwargs: {
            "success": True,
            "schema_version": "workflow_prediction_envelope.v1",
            "workflow_id": kwargs["workflow_id"],
            "filters": {
                "namespace": kwargs.get("namespace"),
                "model": kwargs.get("model"),
                "provider": kwargs.get("provider"),
                "limit": kwargs.get("limit"),
            },
            "sample_window": {"candidate_trace_count": 2, "matched_trace_count": 1},
            "prediction_envelope": {
                "prediction_kind": "observed_history_envelope",
                "completion_rate": 1.0,
            },
        },
    )

    payload = gateway.invoke(
        "workflow_build_prediction_envelope",
        {
            "workflow_id": "#V#workflow_a",
            "namespace": "#V#user@org",
            "model": "gpt-5-mini",
            "provider": "openai",
            "limit": 12,
        },
    ).payload

    assert payload["success"] is True
    assert payload["workflow_id"] == "#V#workflow_a"
    assert payload["filters"]["namespace"] == "#V#user@org"
    assert payload["filters"]["model"] == "gpt-5-mini"
    assert payload["filters"]["provider"] == "openai"
    assert payload["filters"]["limit"] == 12
