"""DB-backed stdio E2E tests for renderer profile authoring + resolution."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.mcp_server import mcp_stdio_server
from src.backend.security.access_control import bypass_access_control

_RENDERER_CONCEPT_ID = "#V#renderer_profile_e2e_timeline_renderer"
_TASK_TYPE_ID = "#V#renderer_profile_e2e_task"
_TASK_INSTANCE_ID = "#V#renderer_profile_e2e_task_instance"


@pytest.fixture(autouse=True)
def clean_renderer_profile_e2e_data():
    db = TextValuesRepository.db()
    if db is None:
        yield
        return

    concepts = ConceptsRepository.collection()
    relations = TextRelationsRepository.collection()
    text_values = TextValuesRepository.collection()
    if concepts is None or relations is None or text_values is None:
        yield
        return

    concept_ids = [_RENDERER_CONCEPT_ID, _TASK_TYPE_ID, _TASK_INSTANCE_ID]

    def _cleanup() -> None:
        linked_text_ids = [
            rel.get("object_text_id")
            for rel in relations.find(
                {
                    "subject_concept_id": {"$in": concept_ids},
                    "predicate": "#V#has_renderer_profile_json",
                },
                projection={"object_text_id": 1},
            )
            if rel.get("object_text_id")
        ]
        concepts.delete_many({"concept_id": {"$in": concept_ids}})
        relations.delete_many({"subject_concept_id": {"$in": concept_ids}})
        if linked_text_ids:
            text_values.delete_many({"_id": {"$in": linked_text_ids}})

    _cleanup()
    yield
    _cleanup()


def _call_tool_json(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def _invoke() -> dict[str, Any]:
        with bypass_access_control():
            raw_response = await mcp_stdio_server.call_tool(name, arguments)
        response = cast(list[Any], raw_response)
        assert response
        text = getattr(response[0], "text", None)
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        return payload

    return asyncio.run(_invoke())


def test_stdio_upsert_and_resolve_renderer_profile_from_persisted_concepts() -> None:
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": _RENDERER_CONCEPT_ID, "relationships": {}})
    concepts.insert_one(
        {
            "concept_id": _TASK_TYPE_ID,
            "relationships": {"is_a_type_of": ["#V#thing"]},
        }
    )
    concepts.insert_one(
        {
            "concept_id": _TASK_INSTANCE_ID,
            "relationships": {
                "is_an_instance_of": [_TASK_TYPE_ID],
                "#V#has_start_time": ["#V#renderer_profile_e2e_time_value"],
            },
        }
    )

    upsert_payload = _call_tool_json(
        "upsert_renderer_profile",
        {
            "renderer_concept_id": _RENDERER_CONCEPT_ID,
            "renderer_profile": {
                "renderer_type": "timeline",
                "modalities": ["visual"],
                "applies_to_object_kinds": ["concept"],
                "applies_to_concept_type_ids": [_TASK_TYPE_ID],
                "required_predicates": ["#V#has_start_time"],
                "priority": 90,
            },
        },
    )
    assert upsert_payload.get("success") is True
    assert upsert_payload.get("renderer_concept_id") == _RENDERER_CONCEPT_ID

    resolve_payload = _call_tool_json(
        "renderer_resolve_applicability",
        {
            "renderer_definition_concept_ids": [_RENDERER_CONCEPT_ID],
            "request_payload": {"concept_id": _TASK_INSTANCE_ID},
        },
    )
    assert resolve_payload.get("success") is True

    selected = resolve_payload.get("selected_renderers") or []
    assert isinstance(selected, list)
    assert selected
    assert selected[0].get("renderer_id") == _RENDERER_CONCEPT_ID

    diagnostics = resolve_payload.get("diagnostics") or {}
    loading = diagnostics.get("renderer_definition_loading") or {}
    enrichment = diagnostics.get("request_payload_enrichment") or {}
    assert loading.get("loaded_definition_count") == 1
    assert _RENDERER_CONCEPT_ID in set(loading.get("loaded_concept_ids") or [])
    assert enrichment.get("concept_lookup_succeeded") is True
    assert "concept_type_ids" in set(enrichment.get("filled_fields") or [])
