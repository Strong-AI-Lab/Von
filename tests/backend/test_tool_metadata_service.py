from __future__ import annotations


def test_gmail_send_metadata_marks_external_surface_and_planner_hint(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        metadata = service.get_tool_metadata("gmail_send_message")
        surface = service.get_tool_dispatch_surface_metadata("gmail_send_message")

        assert metadata.category == "gmail"
        assert metadata.operation_category == "write"
        assert metadata.planner_hint is not None
        assert "allow_send=true" in metadata.planner_hint
        assert surface is not None
        assert surface.surface_family == "gmail"
        assert surface.evidence_surface_family == "gmail"
        assert surface.external_surface is True
    finally:
        service.invalidate_cache()


def test_gmail_read_tools_share_external_surface_metadata(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        surface = service.get_tool_dispatch_surface_metadata("gmail_list_messages")

        assert surface is not None
        assert surface.surface_family == "gmail"
        assert surface.evidence_surface_family == "gmail"
        assert surface.external_surface is True
    finally:
        service.invalidate_cache()
