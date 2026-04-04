from __future__ import annotations

import json
from typing import Any

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services import concept_service
from src.backend.services.entity_representation_workflow_vontology_service import (
    COMPANY_REPRESENTATION_WORKFLOW_ID,
    EVENT_REPRESENTATION_WORKFLOW_ID,
    PERSON_REPRESENTATION_WORKFLOW_ID,
    PLACE_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_entity_representation_workflows,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


class _QueuedLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def generate(
        self,
        prompt: str,
        context: Any = None,
        model: str | None = None,
        llm_params: dict[str, Any] | None = None,
    ) -> str:
        _ = (prompt, context, model, llm_params)
        if not self._responses:
            raise AssertionError("queued_llm_exhausted")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


@pytest.mark.parametrize(
    (
        "workflow_id",
        "entity_domain",
        "expected_type_id",
        "prompt",
        "payload",
        "expected_alias",
    ),
    [
        (
            PERSON_REPRESENTATION_WORKFLOW_ID,
            "person",
            "#V#person",
            "Represent Grace Hopper in the Vontology if she is not already represented.",
            {
                "ready_to_materialise": True,
                "needs_user_affirmation": False,
                "entity_name": "Grace Hopper",
                "entity_description": "Computer scientist and rear admiral.",
                "entity_aliases": ["Amazing Grace"],
                "entity_source_text": (
                    "Represent Grace Hopper in the Vontology if she is not already "
                    "represented."
                ),
                "response_text": "Representing Grace Hopper now.",
            },
            "Amazing Grace",
        ),
        (
            COMPANY_REPRESENTATION_WORKFLOW_ID,
            "company",
            "#V#organisation",
            "Represent OpenAI in the Vontology as a company.",
            {
                "ready_to_materialise": True,
                "needs_user_affirmation": False,
                "entity_name": "OpenAI",
                "entity_description": "AI research and deployment company.",
                "entity_aliases": ["OpenAI, Inc."],
                "entity_source_text": "Represent OpenAI in the Vontology as a company.",
                "response_text": "Representing OpenAI now.",
            },
            "OpenAI, Inc.",
        ),
        (
            EVENT_REPRESENTATION_WORKFLOW_ID,
            "event",
            "#V#event",
            "Represent the 2026 Neuro-Symbolic Systems Workshop in the Vontology as an event.",
            {
                "ready_to_materialise": True,
                "needs_user_affirmation": False,
                "entity_name": "2026 Neuro-Symbolic Systems Workshop",
                "entity_description": "Research workshop on neuro-symbolic systems.",
                "entity_aliases": ["NSS Workshop 2026"],
                "entity_source_text": (
                    "Represent the 2026 Neuro-Symbolic Systems Workshop in the "
                    "Vontology as an event."
                ),
                "response_text": "Representing the workshop now.",
            },
            "NSS Workshop 2026",
        ),
        (
            PLACE_REPRESENTATION_WORKFLOW_ID,
            "place",
            "#V#place",
            "Represent the Auckland Domain in the Vontology as a place.",
            {
                "ready_to_materialise": True,
                "needs_user_affirmation": False,
                "entity_name": "Auckland Domain",
                "entity_description": "Large public park in central Auckland.",
                "entity_aliases": ["Pukekawa"],
                "entity_source_text": (
                    "Represent the Auckland Domain in the Vontology as a place."
                ),
                "response_text": "Representing Auckland Domain now.",
            },
            "Pukekawa",
        ),
    ],
)
def test_supported_entity_representation_benchmark_cases_execute_end_to_end(
    workflow_id: str,
    entity_domain: str,
    expected_type_id: str,
    prompt: str,
    payload: dict[str, Any],
    expected_alias: str,
) -> None:
    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(workflow_id)
    assert definition is not None

    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=20,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM([json.dumps(payload)]),
            user_namespace="#V#test_user",
        ),
        data={"prompt": prompt},
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("requires_user_affirmation") is False
    assert result.data.get("entity_representation_verified") is True
    assert result.data.get("entity_representation_domain") == entity_domain

    concept_id = str(result.data.get("entity_representation_concept_id") or "").strip()
    assert concept_id
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None

    type_ids = (concept_doc.get("relationships") or {}).get("is_an_instance_of") or []
    assert expected_type_id in type_ids

    description_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        limit=20,
    )
    assert any(
        isinstance(row, dict)
        and (row.get("text") or "") == payload["entity_description"]
        for row in description_rows
    )

    note_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasNote",
        limit=20,
    )
    assert any(
        isinstance(row, dict)
        and (row.get("text") or "") == payload["entity_source_text"]
        for row in note_rows
    )

    alias_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasName",
        limit=20,
    )
    assert any(
        isinstance(row, dict) and (row.get("text") or "") == expected_alias
        for row in alias_rows
    )


def test_entity_representation_benchmark_ambiguity_case_stays_low_imposition() -> None:
    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(
        PERSON_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=20,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM(
                [
                    json.dumps(
                        {
                            "ready_to_materialise": False,
                            "needs_user_affirmation": True,
                            "entity_name": "",
                            "entity_description": "",
                            "entity_aliases": [],
                            "entity_source_text": (
                                "Please represent Jordan in the Vontology."
                            ),
                            "response_text": "Which Jordan do you mean?",
                        }
                    )
                ]
            ),
            user_namespace="#V#test_user",
        ),
        data={"prompt": "Please represent Jordan in the Vontology."},
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("requires_user_affirmation") is True
    assert not bool(result.data.get("entity_representation_verified"))
    assert not str(result.data.get("entity_representation_concept_id") or "").strip()
    assert result.data.get("response_text") == "Which Jordan do you mean?"
