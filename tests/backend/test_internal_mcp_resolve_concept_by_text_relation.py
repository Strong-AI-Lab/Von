from __future__ import annotations

from typing import Any

from src.backend.integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    build_default_catalogue,
)
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    override_current_actor,
)


def _resolved_payload(**_kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "status": "resolved",
        "predicate": "#V#has_email",
        "text": "alice@example.test",
        "instance_of": "#V#person",
        "resolved_concept_id": "#V#person_alice",
        "candidates": [],
        "candidate_count": 1,
        "candidate_count_is_lower_bound": False,
        "candidates_truncated": False,
        "resolution_complete": True,
    }


def test_catalogue_registers_bounded_read_contract() -> None:
    catalogue = build_default_catalogue()
    definition = catalogue.get("resolve_concept_by_text_relation")
    snapshot = catalogue.snapshot()["resolve_concept_by_text_relation"]

    assert definition.category == "read"
    assert definition.hard_timeout_enabled is False
    assert definition.resolved_timeout(InternalMCPTransport()) is None
    assert snapshot["input_schema"]["required"] == ["predicate", "text"]
    assert snapshot["input_schema"]["optional"] == [
        "instance_of",
        "max_results",
    ]
    assert snapshot["output_schema"]["required"] == [
        "candidate_count",
        "candidate_count_is_lower_bound",
        "candidates",
        "candidates_truncated",
        "instance_of",
        "predicate",
        "resolution_complete",
        "resolved_concept_id",
        "status",
        "success",
        "text",
    ]


def test_gateway_binds_trusted_actor_and_ignores_payload_identity(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def resolve(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        captured["actor"] = (
            get_effective_user_concept_id(),
            get_effective_organisation_concept_id(),
        )
        return _resolved_payload()

    monkeypatch.setattr(
        "src.backend.services.text_relation_resolution_service."
        "resolve_concept_by_text_relation",
        resolve,
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    with override_current_actor("#V#trusted_user", "#V#trusted_org"):
        result = gateway.invoke(
            "resolve_concept_by_text_relation",
            {
                "predicate": "#V#has_email",
                "text": "alice@example.test",
                "instance_of": "#V#person",
                "max_results": 4,
                "user_id": "#V#spoofed_user",
                "org_id": "#V#spoofed_org",
            },
        ).payload

    assert result["status"] == "resolved"
    assert captured == {
        "predicate": "#V#has_email",
        "text": "alice@example.test",
        "instance_of": "#V#person",
        "max_results": 4,
        "actor": ("#V#trusted_user", "#V#trusted_org"),
    }


def test_payload_identity_alone_does_not_create_actor_authority(monkeypatch) -> None:
    captured_actor: list[tuple[str | None, str | None]] = []

    def resolve(**_kwargs: Any) -> dict[str, Any]:
        captured_actor.append(
            (
                get_effective_user_concept_id(),
                get_effective_organisation_concept_id(),
            )
        )
        return _resolved_payload()

    monkeypatch.setattr(
        "src.backend.services.text_relation_resolution_service."
        "resolve_concept_by_text_relation",
        resolve,
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "resolve_concept_by_text_relation",
        {
            "predicate": "#V#has_email",
            "text": "alice@example.test",
            "user_id": "#V#payload_user",
            "org_id": "#V#payload_org",
        },
    ).payload

    assert result["status"] == "resolved"
    assert captured_actor == [(None, None)]


def test_default_metadata_marks_internal_exact_resolution_as_search(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service as metadata_service

    monkeypatch.setattr(metadata_service, "_load_from_vontology", dict)
    metadata_service.invalidate_cache()
    try:
        metadata = metadata_service.get_tool_metadata(
            "resolve_concept_by_text_relation"
        )
        exposure = metadata_service.get_tool_surface_exposure_metadata(
            "resolve_concept_by_text_relation"
        )

        assert metadata.category == "vontology"
        assert metadata.operation_category == "read"
        assert metadata.evidence_role == "search"
        assert metadata.dispatch_surface_family == "knowledge_base"
        assert metadata.external_surface is False
        assert "never choose" in (metadata.planner_hint or "")
        assert exposure.expose_in_vontology_stdio is False
        assert exposure.expose_in_manifest is False
    finally:
        metadata_service.invalidate_cache()
