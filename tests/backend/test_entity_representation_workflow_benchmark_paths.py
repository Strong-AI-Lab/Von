from __future__ import annotations

import json
from typing import Any

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services import concept_service
from src.backend.services.entity_representation_workflow_vontology_service import (
    COMPANY_REPRESENTATION_WORKFLOW_ID,
    ENTITY_REPRESENTATION_WORKFLOW_ID,
    EVENT_REPRESENTATION_WORKFLOW_ID,
    PERSON_REPRESENTATION_WORKFLOW_ID,
    PLACE_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_entity_representation_workflows,
)
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable import (
    entity_representation_workflow as entity_actions,
)
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)


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
    assert result.data.get("entity_representation_readback_verified") is True
    assert result.data.get("entity_representation_domain") == entity_domain
    assert result.data.get("entity_representation_materialised") is True
    assert result.data.get("entity_representation_reused_existing") is False

    concept_id = str(result.data.get("entity_representation_concept_id") or "").strip()
    assert concept_id
    assert result.data.get("entity_representation_readback_concept_id") == concept_id
    assert result.data.get("entity_resolution_status") == "not_found"
    has_name_readback = result.data.get("entity_representation_has_name_readback")
    assert isinstance(has_name_readback, list)
    assert any(
        isinstance(row, dict) and row.get("text") == expected_alias
        for row in has_name_readback
    )
    assert str(result.data.get("response_text") or "").startswith(
        f"Created {entity_domain} "
    )
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


def test_entity_wrapper_preserves_successful_child_clarification_outputs() -> None:
    """Conditional child outputs must not become unconditional write postconditions."""

    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(
        ENTITY_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    preflight = {
        "decision": "reuse",
        "entity_domain": "person",
        "workflow_template_id": "#V#workflow_creation_person_template",
        "selected_workflow_id": PERSON_REPRESENTATION_WORKFLOW_ID,
        "workflow_creation_prompt": None,
        "response_text": "Using the represented person workflow.",
    }
    child_clarification = {
        "ready_to_materialise": False,
        "needs_user_affirmation": True,
        "entity_name": "Ada Lovelace",
        "entity_description": "",
        "entity_aliases": [],
        "entity_source_text": (
            "Represent Ada Lovelace in the Vontology if not already represented."
        ),
        "response_text": "Please confirm the existing Ada identity.",
    }

    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=30,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM(
                [json.dumps(preflight), json.dumps(child_clarification)]
            ),
            user_namespace="#V#test_user",
        ),
        data={
            "prompt": (
                "Represent Ada Lovelace in the Vontology if not already "
                "represented, then read it back."
            )
        },
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("requires_user_affirmation") is True
    assert result.data.get("response_text") == (
        "Please confirm the existing Ada identity."
    )
    assert result.data.get("child_workflow_failed") is not True
    assert "metadata_write_context_key_missing" not in str(result.error or "")


@pytest.mark.parametrize(
    ("workflow_id", "entity_domain", "entity_type_id", "entity_name"),
    [
        (PERSON_REPRESENTATION_WORKFLOW_ID, "person", "#V#person", "Jane Example"),
        (
            COMPANY_REPRESENTATION_WORKFLOW_ID,
            "company",
            "#V#organisation",
            "Example Research Ltd",
        ),
        (
            EVENT_REPRESENTATION_WORKFLOW_ID,
            "event",
            "#V#event",
            "Example Systems Workshop",
        ),
        (
            PLACE_REPRESENTATION_WORKFLOW_ID,
            "place",
            "#V#place",
            "Example Research Park",
        ),
    ],
)
def test_existing_entity_reuse_is_read_only_and_reads_back_all_names(
    workflow_id: str,
    entity_domain: str,
    entity_type_id: str,
    entity_name: str,
) -> None:
    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(workflow_id)
    assert definition is not None

    concept_id = f"#V#existing_{entity_domain}_reuse_test"
    concept_service.create_concept(
        name=entity_name,
        concept_id=concept_id,
        description="Original represented description.",
        parent_concept_ids=[entity_type_id],
        create_as_instance=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        text="Original represented description.",
        lang="en-NZ",
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasNote",
        text="Original represented note.",
        lang="en-NZ",
        garbage_collect=True,
    )
    upsert_text_for_concept(
        subject_concept_id=concept_id,
        predicate="hasName",
        text=f"{entity_name} Alias",
        lang="en-NZ",
    )
    relationships_before = dict(
        (concept_service.get_concept_by_concept_id(concept_id) or {}).get(
            "relationships"
        )
        or {}
    )

    payload = {
        "ready_to_materialise": True,
        "needs_user_affirmation": False,
        "entity_name": entity_name,
        "entity_description": "Replacement description that must not be written.",
        "entity_aliases": ["Replacement Alias"],
        "entity_source_text": "Replacement note that must not be written.",
        "response_text": "Provisional extraction response.",
    }
    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=30,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM([json.dumps(payload)]),
        ),
        data={
            "prompt": (
                f"Represent {entity_name} in the Vontology if not already "
                "represented, then read it back."
            )
        },
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("entity_representation_concept_id") == concept_id
    assert result.data.get("entity_representation_reused_existing") is True
    assert result.data.get("entity_representation_materialised") is False
    assert result.data.get("entity_representation_verified") is True
    assert result.data.get("entity_representation_readback_verified") is True
    assert result.data.get("entity_representation_readback_concept_id") == concept_id
    assert result.data.get("entity_resolution_status") == "resolved"
    assert "without mutation" in str(result.data.get("response_text") or "")

    has_name_readback = result.data.get("entity_representation_has_name_readback")
    assert isinstance(has_name_readback, list)
    assert {str(row.get("text") or "") for row in has_name_readback} >= {
        entity_name,
        f"{entity_name} Alias",
    }
    assert "Replacement Alias" not in {
        str(row.get("text") or "") for row in has_name_readback
    }
    assert {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            concept_id,
            predicate="hasDescription",
            limit=20,
        )
    } == {"Original represented description."}
    assert {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            concept_id,
            predicate="hasNote",
            limit=20,
        )
    } == {"Original represented note."}
    assert (
        dict(
            (concept_service.get_concept_by_concept_id(concept_id) or {}).get(
                "relationships"
            )
            or {}
        )
        == relationships_before
    )


def test_exact_existing_entity_ambiguity_clarifies_without_mutation() -> None:
    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(
        PERSON_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    entity_name = "Alex Example"
    concept_ids = ("#V#alex_example_one", "#V#alex_example_two")
    for concept_id in concept_ids:
        concept_service.create_concept(
            name=entity_name,
            concept_id=concept_id,
            description=f"Original {concept_id}.",
            parent_concept_ids=["#V#person"],
            create_as_instance=True,
        )

    payload = {
        "ready_to_materialise": True,
        "needs_user_affirmation": False,
        "entity_name": entity_name,
        "entity_description": "This must not be written.",
        "entity_aliases": ["Must Not Be Written"],
        "entity_source_text": "This note must not be written.",
        "response_text": "Provisional extraction response.",
    }
    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=30,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM([json.dumps(payload)]),
        ),
        data={"prompt": f"Represent {entity_name} if absent, then read it back."},
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("requires_user_affirmation") is True
    assert result.data.get("entity_resolution_status") == "ambiguous"
    assert not result.data.get("entity_representation_materialised")
    assert not result.data.get("entity_representation_readback_verified")
    assert not result.data.get("entity_representation_concept_id")
    assert "multiple existing person concepts" in str(
        result.data.get("response_text") or ""
    )
    assert len(result.data.get("entity_resolution_candidates") or []) == 2
    for concept_id in concept_ids:
        assert not get_texts_for_concept(
            concept_id,
            predicate="hasNote",
            limit=20,
        )
        assert "Must Not Be Written" not in {
            str(row.get("text") or "")
            for row in get_texts_for_concept(
                concept_id,
                predicate="hasName",
                limit=20,
            )
        }


def test_materialise_primitive_existing_guard_is_read_only() -> None:
    concept_id = "#V#existing_person_primitive_guard"
    concept_service.create_concept(
        name="Existing Guard Person",
        concept_id=concept_id,
        description="Original description.",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasNote",
        text="Original note.",
        lang="en-NZ",
        garbage_collect=True,
    )

    result = entity_actions._handle_materialise_from_payload(
        WorkflowActionRequest(
            action_id=entity_actions.ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID,
            inputs={
                "entity_domain": "person",
                "entity_type_id": "#V#person",
                "entity_name": "Existing Guard Person",
                "entity_description": "Replacement description.",
                "entity_aliases": ["Replacement Alias"],
                "entity_source_text": "Replacement note.",
            },
            environment=WorkflowEnvironment(
                llm_client=_QueuedLLM([]),
                user_namespace="#V#test_user",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert result.outputs.get("entity_representation_concept_id") == concept_id
    assert result.outputs.get("entity_representation_reused_existing") is True
    assert result.outputs.get("entity_representation_materialised") is False
    assert "response_text" not in result.outputs
    assert {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            concept_id,
            predicate="hasNote",
            limit=20,
        )
    } == {"Original note."}
    assert "Replacement Alias" not in {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            concept_id,
            predicate="hasName",
            limit=20,
        )
    }
