"""Regression tests for deterministic create_concepts duplicate guard."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp.catalogue import _create_concepts
from src.backend.mcp_server import mcp_stdio_server


@pytest.fixture(autouse=True)
def seed_core_types():
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    seed_docs = [
        {
            "concept_id": "#V#thing",
            "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
        },
        {
            "concept_id": "#V#abstract_object",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        },
        {
            "concept_id": "#V#durable_workflow",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        },
        {
            "concept_id": "#V#predicate",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        },
    ]
    for doc in seed_docs:
        concepts.update_one({"concept_id": doc["concept_id"]}, {"$setOnInsert": doc}, upsert=True)

    yield

    concepts.delete_many({"concept_id": {"$regex": "^#V#duplicate_guard_"}})


def test_create_concepts_blocks_duplicate_workflow_instance():
    payload = {
        "parent_id": "#V#durable_workflow",
        "concepts": [{"name": "duplicate_guard_workflow_instance", "kind": "instance"}],
    }
    first = _create_concepts(**payload)
    assert first.get("successful") == 1
    assert first.get("already_existed") == 0
    created_id = first["created_concept_ids"][0]
    assert created_id == "#V#duplicate_guard_workflow_instance"

    second = _create_concepts(**payload)
    assert second.get("successful") == 0
    assert second.get("already_existed") == 1
    item = (second.get("results") or [{}])[0]
    assert item.get("error_code") == "already_exists"
    assert item.get("existing_concept_id") == created_id
    assert item.get("duplicate_prevented") is True
    assert item.get("duplicate_guard_scope") == "workflow_instance"

    docs = list(
        ConceptsRepository.find({"concept_id": {"$regex": "^#V#duplicate_guard_workflow_instance"}})
    )
    ids = sorted(
        str(doc.get("concept_id")).strip()
        for doc in docs
        if isinstance(doc, dict) and isinstance(doc.get("concept_id"), str)
    )
    assert ids == ["#V#duplicate_guard_workflow_instance"]


def test_create_concepts_blocks_duplicate_non_workflow_instance_by_default():
    payload = {
        "parent_id": "#V#abstract_object",
        "concepts": [{"name": "duplicate_guard_regular_instance", "kind": "instance"}],
    }
    first = _create_concepts(**payload)
    assert first.get("successful") == 1
    assert first.get("already_existed") == 0
    assert first["created_concept_ids"][0] == "#V#duplicate_guard_regular_instance"

    second = _create_concepts(**payload)
    assert second.get("successful") == 0
    assert second.get("already_existed") == 1
    item = (second.get("results") or [{}])[0]
    assert item.get("error_code") == "already_exists"
    assert item.get("existing_concept_id") == "#V#duplicate_guard_regular_instance"
    assert item.get("duplicate_prevented") is True
    assert item.get("duplicate_guard_scope") == "instance"


def test_create_concepts_can_allow_legacy_suffix_when_requested():
    payload = {
        "parent_id": "#V#abstract_object",
        "concepts": [{"name": "duplicate_guard_legacy_opt_in", "kind": "instance"}],
        "allow_duplicate_instances": True,
    }
    first = _create_concepts(**payload)
    assert first.get("successful") == 1
    assert first.get("already_existed") == 0
    assert first["created_concept_ids"][0] == "#V#duplicate_guard_legacy_opt_in"

    second = _create_concepts(**payload)
    assert second.get("successful") == 1
    assert second.get("already_existed") == 0
    assert second["created_concept_ids"][0] == "#V#duplicate_guard_legacy_opt_in_2"


def test_stdio_create_concepts_blocks_duplicate_workflow_instance():
    async def _invoke() -> dict:
        raw_response = await mcp_stdio_server.call_tool(
            "create_concepts",
            {
                "parent_id": "#V#durable_workflow",
                "concepts": [
                    {"name": "duplicate_guard_stdio_workflow", "kind": "instance"}
                ],
            },
        )
        response = cast(list[Any], raw_response)
        assert response
        text = getattr(response[0], "text", None)
        assert isinstance(text, str)
        return json.loads(text)

    first = asyncio.run(_invoke())
    assert first.get("successful") == 1
    assert first.get("already_existed") == 0
    assert first.get("created_concept_ids") == ["#V#duplicate_guard_stdio_workflow"]

    second = asyncio.run(_invoke())
    assert second.get("successful") == 0
    assert second.get("already_existed") == 1
    item = (second.get("results") or [{}])[0]
    assert item.get("error_code") == "already_exists"
    assert item.get("duplicate_prevented") is True
    assert item.get("existing_concept_id") == "#V#duplicate_guard_stdio_workflow"


def test_stdio_create_concepts_can_allow_legacy_suffix_when_requested():
    async def _invoke() -> dict:
        raw_response = await mcp_stdio_server.call_tool(
            "create_concepts",
            {
                "parent_id": "#V#abstract_object",
                "concepts": [
                    {"name": "duplicate_guard_stdio_legacy_opt_in", "kind": "instance"}
                ],
                "allow_duplicate_instances": True,
            },
        )
        response = cast(list[Any], raw_response)
        assert response
        text = getattr(response[0], "text", None)
        assert isinstance(text, str)
        return json.loads(text)

    first = asyncio.run(_invoke())
    assert first.get("successful") == 1
    assert first.get("already_existed") == 0
    assert first.get("created_concept_ids") == ["#V#duplicate_guard_stdio_legacy_opt_in"]

    second = asyncio.run(_invoke())
    assert second.get("successful") == 1
    assert second.get("already_existed") == 0
    assert second.get("created_concept_ids") == [
        "#V#duplicate_guard_stdio_legacy_opt_in_2"
    ]
