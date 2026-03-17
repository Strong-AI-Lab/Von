from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_runtime_event_binding_authority_has_no_env_fallback() -> None:
    service_text = _read("src/backend/services/workflow_event_integration_service.py")
    assert "EVENT_WORKFLOW_ID_ENV_MAP" not in service_text
    assert "_workflow_id_for_event(" not in service_text


def test_event_binding_tools_no_longer_advertise_env_fallback() -> None:
    catalogue_text = _read("src/backend/integrations/internal_mcp/catalogue.py")
    manifest_text = _read("src/backend/mcp_server/vontology_mcp.json")

    assert "include_env_fallback" not in catalogue_text
    assert "environment fallback" not in catalogue_text.lower()
    assert "include_env_fallback" not in manifest_text
    assert "environment fallback" not in manifest_text.lower()
