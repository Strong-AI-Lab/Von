from __future__ import annotations

import re
from typing import Any


def _reset_mock_vontology_db(monkeypatch: Any) -> None:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is None:
        return
    for collection_name in ("concepts", "text_relations", "text_values"):
        try:
            db.drop_collection(collection_name)
        except Exception:
            pass


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


def test_source_processing_marker_metadata_is_vontology_internal(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        write_metadata = service.get_tool_metadata("record_source_processing_marker")
        write_surface = service.get_tool_dispatch_surface_metadata(
            "record_source_processing_marker"
        )
        read_metadata = service.get_tool_metadata("get_source_processing_marker")

        assert write_metadata.category == "vontology"
        assert write_metadata.operation_category == "write"
        assert write_surface is not None
        assert write_surface.surface_family == "knowledge_base"
        assert write_surface.external_surface is False
        assert read_metadata.operation_category == "read"
        assert read_metadata.evidence_role == "verification"
    finally:
        service.invalidate_cache()


def test_state_materialising_reports_are_classified_as_writes(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        assert (
            service.get_tool_operation_category("build_paper_recommendations")
            == "write"
        )
        assert (
            service.get_tool_operation_category(
                "episode_critique_build_benchmark"
            )
            == "write"
        )
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


def test_gmail_read_tools_are_prompt_required_evidence_from_vontology(monkeypatch):
    _reset_mock_vontology_db(monkeypatch)

    from src.backend.services.gmail_tool_evidence_contract_vontology_service import (
        bootstrap_gmail_tool_evidence_contract,
    )
    from src.backend.services import tool_metadata_service as service

    bootstrap_report = bootstrap_gmail_tool_evidence_contract()
    assert bootstrap_report["success"] is True

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


def test_gmail_read_tools_without_represented_roles_are_not_prompt_required(
    monkeypatch,
):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        list_metadata = service.get_tool_metadata("gmail_list_messages")
        message_metadata = service.get_tool_metadata("gmail_get_message")

        assert list_metadata.category == "gmail"
        assert list_metadata.operation_category is None
        assert list_metadata.evidence_role is None
        assert service.is_tool_prompt_required_evidence("gmail_list_messages") is False

        assert message_metadata.category == "gmail"
        assert message_metadata.operation_category is None
        assert message_metadata.evidence_role is None
        assert service.is_tool_prompt_required_evidence("gmail_get_message") is False
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


def test_jira_get_issue_identifier_binding_metadata_is_discoverable(monkeypatch):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", lambda: {})
    service.invalidate_cache()
    try:
        binding = service.get_tool_identifier_binding_metadata("jira_get_issue")

        assert binding is not None
        assert binding.identifier_argument_name == "issue_key"
        assert binding.identifier_source == "user_text"
        assert binding.identifier_max_count == 1
        assert binding.identifier_normalise == "upper"
        assert re.search(binding.identifier_pattern, "Tell me about JVNAUTOSCI-150")
    finally:
        service.invalidate_cache()


def test_vontology_concept_search_alias_inherits_schema_discovery_metadata(
    monkeypatch,
):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", dict)
    service.invalidate_cache()
    try:
        canonical = service.get_tool_metadata("search_concepts")
        alias = service.get_tool_metadata("vontology_concept_search")

        assert alias.tool_name == "vontology_concept_search"
        assert alias.description == canonical.description
        assert alias.planner_hint == canonical.planner_hint
        assert alias.operation_category == "read"
        assert alias.evidence_role == "search"
        assert "schema discovery" in (alias.planner_hint or "").lower()
        assert "relation-bearing lookup" in (alias.planner_hint or "")
        assert "PhD" not in (alias.description or "")
        assert "student" not in (alias.planner_hint or "").lower()
    finally:
        service.invalidate_cache()


def test_relation_read_hints_preserve_direction_neutral_completeness(
    monkeypatch,
):
    from src.backend.services import tool_metadata_service as service

    monkeypatch.setattr(service, "_load_from_vontology", dict)
    service.invalidate_cache()
    try:
        incidence = service.get_tool_metadata("get_predicate_incidence")
        relation_read = service.get_tool_metadata("find_relations_with_argument")

        assert "argument_index='any'" in (incidence.planner_hint or "")
        assert "exact predicate IDs" in (incidence.planner_hint or "")
        assert "one-direction" in (incidence.planner_hint or "")
        assert "argument_index='any'" in (relation_read.planner_hint or "")
        assert "complete negative" in (relation_read.planner_hint or "")
        assert "relation_kind='binary'" in (relation_read.planner_hint or "")
        assert "vontology_concept_search" in (relation_read.planner_hint or "")
        assert "broad unfiltered relation page" in (
            relation_read.planner_hint or ""
        )
        assert "PhD" not in (incidence.planner_hint or "")
        assert "student" not in (relation_read.planner_hint or "").lower()
    finally:
        service.invalidate_cache()


def test_vontology_concept_search_alias_inherits_represented_metadata(
    monkeypatch,
):
    from src.backend.services import tool_metadata_service as service

    represented = service.ToolMetadata(
        tool_name="search_concepts",
        concept_id="#V#search_concepts_tool",
        description="Represented schema discovery description.",
        planner_hint="Represented schema discovery planner hint.",
        category="vontology",
        operation_category="read",
        evidence_role="search",
    )
    monkeypatch.setattr(
        service,
        "_load_from_vontology",
        lambda: {"search_concepts": represented},
    )
    service.invalidate_cache()
    try:
        alias = service.get_tool_metadata("vontology_concept_search")

        assert alias.concept_id == "#V#search_concepts_tool"
        assert alias.description == "Represented schema discovery description."
        assert alias.planner_hint == "Represented schema discovery planner hint."
    finally:
        service.invalidate_cache()


def test_placeholder_description_cannot_override_registered_contract(
    monkeypatch,
):
    from src.backend.services import tool_metadata_service as service

    represented = service.ToolMetadata(
        tool_name="direct_read",
        concept_id="#V#direct_read_tool",
        description="Internal MCP metadata concept for direct_read.",
    )
    monkeypatch.setattr(
        service,
        "_load_from_vontology",
        lambda: {"direct_read": represented},
    )
    service.invalidate_cache()
    try:
        assert service.get_tool_description(
            "direct_read",
            fallback_description=(
                "Read grounded records for one anchor and return bounded evidence."
            ),
        ) == "Read grounded records for one anchor and return bounded evidence."
    finally:
        service.invalidate_cache()


def test_substantive_represented_description_remains_model_visible(
    monkeypatch,
):
    from src.backend.services import tool_metadata_service as service

    represented = service.ToolMetadata(
        tool_name="direct_read",
        concept_id="#V#direct_read_tool",
        description=(
            "Read grounded records for one anchor. Use for a bounded direct "
            "answer before escalating to a broader workflow."
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_from_vontology",
        lambda: {"direct_read": represented},
    )
    service.invalidate_cache()
    try:
        assert service.get_tool_description(
            "direct_read",
            fallback_description="Read records.",
        ) == represented.description
    finally:
        service.invalidate_cache()
