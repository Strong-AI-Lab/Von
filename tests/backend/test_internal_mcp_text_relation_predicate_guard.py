from __future__ import annotations

import asyncio
import json
from typing import Any

from src.backend.integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    build_default_catalogue,
)
from src.backend.mcp_server import mcp_stdio_server


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _decode_stdio_payload(result: object) -> dict[str, Any]:
    items = result if isinstance(result, list) else []
    first = items[0]
    return json.loads(getattr(first, "text"))


def test_upsert_singleton_text_relation_rejects_missing_vontology_predicate(
    monkeypatch,
) -> None:
    gateway = _build_gateway()

    def _missing_concept(_concept_id: str) -> dict[str, Any]:
        raise RuntimeError("concept not found")

    def _unexpected_upsert(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("text relation should not be persisted")

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _missing_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        _unexpected_upsert,
    )

    payload = gateway.invoke(
        "upsert_singleton_text_relation",
        {
            "concept_id": "#V#target_concept",
            "predicate": "#V#made_up_field_name",
            "text": "Supplied metadata value",
        },
    ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "predicate_concept_not_found"
    assert payload["error_details"]["predicate"] == "#V#made_up_field_name"
    assert "Do not invent ad-hoc field-name predicates" in " ".join(
        payload.get("suggestions") or []
    )


def test_upsert_singleton_text_relation_accepts_core_predicate_concept_alias(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    calls: list[dict[str, Any]] = []

    def _fake_upsert(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "success": True,
            "concept_id": kwargs["subject_concept_id"],
            "predicate": kwargs["predicate"],
            "language": kwargs["lang"],
            "kept_relation_id": "rel-1",
            "replaced_relation_ids": [],
            "replaced_count": 0,
            "relation_created": True,
            "text_value_id": "text-1",
        }

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        _fake_upsert,
    )

    payload = gateway.invoke(
        "upsert_singleton_text_relation",
        {
            "concept_id": "#V#represented_item",
            "predicate": "#V#hasDescription",
            "text": "Abstract text.",
        },
    ).payload

    assert payload["success"] is True
    assert payload["predicate"] == "hasDescription"
    assert payload["input_predicate"] == "#V#hasDescription"
    assert payload["predicate_concept_id"] == "#V#hasDescription"
    assert calls[0]["predicate"] == "hasDescription"


def test_upsert_text_relation_accepts_existing_custom_predicate_concept(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    calls: list[dict[str, Any]] = []

    def _get_concept(concept_id: str) -> dict[str, Any]:
        assert concept_id == "#V#has_external_identifier"
        return {
            "concept_id": "#V#has_external_identifier",
            "kind": "predicate",
        }

    def _fake_upsert(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "text_value_id": "text-1",
            "relation_id": "rel-1",
            "relation_created": True,
        }

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _get_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _fake_upsert,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    payload = gateway.invoke(
        "upsert_text_relation",
        {
            "concept_id": "#V#represented_item",
            "predicate": "#V#has_external_identifier",
            "text": "external-id-123",
        },
    ).payload

    assert payload["success"] is True
    assert payload["predicate"] == "#V#has_external_identifier"
    assert payload["predicate_concept_id"] == "#V#has_external_identifier"
    assert calls[0]["predicate"] == "#V#has_external_identifier"


def test_stdio_upsert_singleton_text_relation_rejects_missing_vontology_predicate(
    monkeypatch,
) -> None:
    def _missing_concept(_concept_id: str) -> dict[str, Any]:
        raise RuntimeError("concept not found")

    def _unexpected_upsert(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("text relation should not be persisted")

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _missing_concept,
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "upsert_singleton_text_relation",
        _unexpected_upsert,
    )

    async def _runner() -> dict[str, Any]:
        result = await mcp_stdio_server._handle_upsert_singleton_text_relation(
            {
                "concept_id": "#V#target_concept",
                "predicate": "#V#made_up_field_name",
                "text": "Supplied metadata value",
            }
        )
        return _decode_stdio_payload(result)

    payload = asyncio.run(_runner())

    assert payload["success"] is False
    assert payload["error_code"] == "predicate_concept_not_found"
    assert payload["error_details"]["predicate"] == "#V#made_up_field_name"
