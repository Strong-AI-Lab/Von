"""Gateway-path tests for renderer applicability MCP tool."""

from __future__ import annotations

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services import renderer_applicability_vontology_service


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _assert_schema_conformance(gateway: InternalMCPGateway, method: str, payload: dict) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def test_renderer_resolve_applicability_registered() -> None:
    methods = set(build_default_catalogue().list_methods())
    assert "renderer_resolve_applicability" in methods
    assert "upsert_renderer_profile" in methods


def test_renderer_resolve_applicability_gateway_success() -> None:
    gateway = _build_gateway()
    payload = gateway.invoke(
        "renderer_resolve_applicability",
        {
            "renderer_definitions": [
                {
                    "renderer_id": "#V#timeline_renderer",
                    "renderer_type": "timeline",
                    "modalities": ["visual"],
                    "applies_to_object_kinds": ["concept"],
                    "applies_to_concept_type_ids": ["#V#task"],
                    "required_predicates": ["#V#has_start_time"],
                    "priority": 90,
                },
                {
                    "renderer_id": "#V#narration_renderer",
                    "renderer_type": "narration",
                    "modalities": ["narrated_audio"],
                    "applies_to_object_kinds": ["concept"],
                    "priority": 10,
                },
            ],
            "request_payload": {
                "object_kind": "concept",
                "concept_type_ids": ["#V#task"],
                "present_predicates": ["#V#has_start_time"],
                "preferred_modalities": ["visual"],
            },
        },
    ).payload

    assert payload.get("success") is True
    assert payload.get("interpreted_object_kind") == "concept"
    selected = payload.get("selected_renderers") or []
    assert isinstance(selected, list)
    assert selected and selected[0].get("renderer_id") == "#V#timeline_renderer"
    _assert_schema_conformance(gateway, "renderer_resolve_applicability", payload)


def test_renderer_resolve_applicability_gateway_concept_definition_source(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        renderer_applicability_vontology_service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids, profile_text_predicates=None: (
            [
                {
                    "renderer_id": "#V#timeline_renderer",
                    "renderer_type": "timeline",
                    "modalities": ["visual"],
                    "applies_to_object_kinds": ["concept"],
                    "applies_to_concept_type_ids": ["#V#task"],
                    "required_predicates": ["#V#has_start_time"],
                    "priority": 90,
                }
            ],
            {
                "renderer_definition_source": "vontology_concept_text_relations",
                "requested_concept_ids": list(concept_ids),
                "loaded_concept_ids": ["#V#timeline_renderer"],
                "loaded_definition_count": 1,
            },
        ),
    )
    monkeypatch.setattr(
        renderer_applicability_vontology_service,
        "enrich_request_payload_from_concept",
        lambda request_payload: (
            {
                **request_payload,
                "concept_type_ids": ["#V#task"],
                "present_predicates": ["#V#has_start_time"],
                "object_kind": "concept",
            },
            {
                "concept_lookup_attempted": True,
                "concept_lookup_succeeded": True,
                "filled_fields": [
                    "concept_type_ids",
                    "present_predicates",
                    "object_kind",
                ],
            },
        ),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "renderer_resolve_applicability",
        {
            "renderer_definition_concept_ids": ["#V#timeline_renderer"],
            "request_payload": {"concept_id": "#V#task_123"},
        },
    ).payload

    assert payload.get("success") is True
    selected = payload.get("selected_renderers") or []
    assert selected and selected[0].get("renderer_id") == "#V#timeline_renderer"
    diagnostics = payload.get("diagnostics") or {}
    assert diagnostics.get("renderer_definition_inputs", {}).get("concept_id_count") == 1
    assert (
        diagnostics.get("renderer_definition_loading", {}).get("renderer_definition_source")
        == "vontology_concept_text_relations"
    )
    _assert_schema_conformance(gateway, "renderer_resolve_applicability", payload)


def test_renderer_resolve_applicability_gateway_error_schema() -> None:
    gateway = _build_gateway()
    payload = gateway.invoke(
        "renderer_resolve_applicability",
        {
            "renderer_definitions": [{}],
            "request_payload": {},
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_parameter"
    _assert_schema_conformance(gateway, "renderer_resolve_applicability", payload)


def test_renderer_resolve_applicability_reports_missing_or_malformed_concepts(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        renderer_applicability_vontology_service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids, profile_text_predicates=None: (
            [],
            {
                "renderer_definition_source": "vontology_concept_text_relations",
                "requested_concept_ids": list(concept_ids),
                "loaded_concept_ids": [],
                "unresolved_concept_ids": [],
                "missing_profile_concept_ids": ["#V#table_renderer"],
                "malformed_profile_concept_ids": ["#V#workflow_renderer"],
                "malformed_profile_count": 1,
                "malformed_profile_entries": [
                    {
                        "concept_id": "#V#workflow_renderer",
                        "predicate": "#V#has_renderer_profile_json",
                        "error": "json_decode_failed",
                    }
                ],
                "loaded_definition_count": 0,
            },
        ),
    )
    gateway = _build_gateway()
    payload = gateway.invoke(
        "renderer_resolve_applicability",
        {
            "renderer_definition_concept_ids": [
                "#V#table_renderer",
                "#V#workflow_renderer",
            ],
            "request_payload": {"object_kind": "concept"},
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "missing_parameter"
    details = payload.get("error_details") or {}
    loading = details.get("renderer_definition_loading") or {}
    assert loading.get("malformed_profile_concept_ids") == ["#V#workflow_renderer"]
    assert loading.get("missing_profile_concept_ids") == ["#V#table_renderer"]
    assert "canonical_renderer_profile_concept_ids" in details
    _assert_schema_conformance(gateway, "renderer_resolve_applicability", payload)


def test_upsert_renderer_profile_gateway_success(monkeypatch) -> None:
    monkeypatch.setattr(
        renderer_applicability_vontology_service,
        "upsert_renderer_profile",
        lambda **kwargs: {
            "success": True,
            "renderer_concept_id": kwargs["renderer_concept_id"],
            "predicate": "#V#has_renderer_profile_json",
            "language": "en-NZ",
            "renderer_profile": kwargs["renderer_profile"],
            "text_relation": {"kept_relation_id": "rel_1"},
        },
    )
    gateway = _build_gateway()
    payload = gateway.invoke(
        "upsert_renderer_profile",
        {
            "renderer_concept_id": "#V#timeline_renderer",
            "renderer_profile": {
                "renderer_type": "timeline",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
            },
        },
    ).payload

    assert payload.get("success") is True
    assert payload.get("renderer_concept_id") == "#V#timeline_renderer"
    _assert_schema_conformance(gateway, "upsert_renderer_profile", payload)


def test_upsert_renderer_profile_gateway_error_schema(monkeypatch) -> None:
    monkeypatch.setattr(
        renderer_applicability_vontology_service,
        "upsert_renderer_profile",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("bad profile")),
    )
    gateway = _build_gateway()
    payload = gateway.invoke(
        "upsert_renderer_profile",
        {
            "renderer_concept_id": "#V#timeline_renderer",
            "renderer_profile": {},
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_parameter"
    _assert_schema_conformance(gateway, "upsert_renderer_profile", payload)
