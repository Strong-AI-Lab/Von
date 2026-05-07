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


def test_gmail_create_label_metadata_marks_external_write_surface(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        metadata = service.get_tool_metadata("gmail_create_label")
        surface = service.get_tool_dispatch_surface_metadata("gmail_create_label")

        assert metadata.category == "gmail"
        assert metadata.operation_category == "write"
        assert metadata.planner_hint is not None
        assert "allow_mutation=true" in metadata.planner_hint
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


def test_gmail_read_tools_are_prompt_required_evidence(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        list_metadata = service.get_tool_metadata("gmail_list_messages")
        message_metadata = service.get_tool_metadata("gmail_get_message")

        assert list_metadata.operation_category == "read"
        assert list_metadata.evidence_role == "search"
        assert service.is_tool_prompt_required_evidence("gmail_list_messages") is True

        assert message_metadata.operation_category == "read"
        assert message_metadata.evidence_role == "verification"
        assert service.is_tool_prompt_required_evidence("gmail_get_message") is True
    finally:
        service.invalidate_cache()


def test_jira_get_transitions_metadata_is_discoverable(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        metadata = service.get_tool_metadata("jira_get_transitions")
        surface = service.get_tool_dispatch_surface_metadata("jira_get_transitions")

        assert metadata.category == "jira"
        assert metadata.operation_category == "read"
        assert metadata.evidence_role == "verification"
        assert metadata.description is not None
        assert "transition list" in metadata.description
        assert metadata.planner_hint is not None
        assert "transition list" in metadata.planner_hint
        assert "transition IDs" in metadata.planner_hint
        assert surface is not None
        assert surface.surface_family == "jira"
        assert surface.evidence_surface_family == "jira"
        assert surface.external_surface is True
    finally:
        service.invalidate_cache()
