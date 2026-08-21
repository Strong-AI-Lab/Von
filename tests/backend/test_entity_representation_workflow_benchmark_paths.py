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
                "requested_facts": [],
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
                "requested_facts": [],
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
                "requested_facts": [],
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
                "requested_facts": [],
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
            user_concept_id="#V#test_user",
        ),
        data={"prompt": prompt},
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("requires_user_affirmation") is False
    assert result.data.get("entity_core_representation_verified") is True
    assert result.data.get("entity_representation_verified") is True
    assert result.data.get("entity_representation_readback_verified") is True
    assert result.data.get("entity_representation_requested_facts_complete") is True
    assert result.data.get("entity_representation_unresolved_requested_facts") == []
    assert result.data.get("entity_representation_requested_fact_count") == 0
    assert result.data.get("entity_representation_requested_facts_payload_valid") is True
    assert result.data.get("entity_representation_coverage") == "complete"
    assert result.data.get("follow_up_required") is not True
    assert result.data.get("semantic_outcome") != "follow_up_required"
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
                            "requested_facts": [],
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
        "requested_facts": [],
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
        "requested_facts": [],
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
    assert result.data.get("entity_resolution_status") in {"resolved", "not_found"}
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


def test_person_core_prose_and_source_marker_cannot_complete_richer_request() -> None:
    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(
        PERSON_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    concept_id = "#V#existing_burkhard_wuensche_core_only"
    person_name = "Burkhard Wuensche"
    concept_service.create_concept(
        name=person_name,
        concept_id=concept_id,
        description="Computer scientist named in a staff source.",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        text="Computer scientist named in a staff source.",
        lang="en-NZ",
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasNote",
        text="Source-processing marker: staff page inspected.",
        lang="en-NZ",
        garbage_collect=True,
    )
    before = concept_service.get_concept_by_concept_id(concept_id)
    assert before is not None
    relationships_before = dict(before.get("relationships") or {})
    descriptions_before = get_texts_for_concept(
        concept_id,
        predicate="hasDescription",
        limit=20,
    )
    notes_before = get_texts_for_concept(
        concept_id,
        predicate="hasNote",
        limit=20,
    )

    requested_facts = [
        {
            "claim_text": "Burkhard Wuensche is academic staff.",
            "relation_hint": "role",
            "target_name": "Academic staff",
            "evidence_text": "Named in the source's academic-staff set.",
        },
        {
            "claim_text": (
                "Burkhard Wuensche is affiliated with the University of "
                "Auckland School of Computer Science."
            ),
            "relation_hint": "affiliation",
            "target_name": "University of Auckland School of Computer Science",
            "evidence_text": "Named on the School staff page.",
        },
        {
            "claim_text": "The source supplied a LinkedIn identifier for him.",
            "identifier_scheme": "LinkedIn",
            "identifier_value": "linkedin.example/burkhard-wuensche",
            "evidence_text": "LinkedIn profile link in the supplied source.",
        },
    ]
    payload = {
        "ready_to_materialise": True,
        "needs_user_affirmation": False,
        "entity_name": person_name,
        "entity_description": "Replacement prose must not count as enrichment.",
        "entity_aliases": ["Replacement Alias"],
        "entity_source_text": "Source-processing marker: replacement source.",
        "requested_facts": requested_facts,
        "response_text": "Provisional extraction response.",
    }

    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=30,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM([json.dumps(payload)]),
            user_namespace="#V#test_user",
        ),
        data={"prompt": "Represent this person's role, affiliation, and identifier."},
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("entity_representation_concept_id") == concept_id
    assert result.data.get("entity_representation_reused_existing") is True
    assert result.data.get("entity_core_representation_verified") is True
    assert result.data.get("entity_representation_readback_verified") is True
    assert result.data.get("entity_representation_verified") is False
    assert result.data.get("entity_representation_coverage") == "core_only"
    assert result.data.get("follow_up_required") is True
    assert result.data.get("semantic_outcome") == "follow_up_required"
    assert result.data.get("entity_representation_unresolved_requested_facts") == (
        requested_facts
    )
    assert "richer requested representation is not complete" in str(
        result.data.get("response_text") or ""
    )
    assert dict(
        (concept_service.get_concept_by_concept_id(concept_id) or {}).get(
            "relationships"
        )
        or {}
    ) == relationships_before
    assert get_texts_for_concept(
        concept_id,
        predicate="hasDescription",
        limit=20,
    ) == descriptions_before
    assert get_texts_for_concept(
        concept_id,
        predicate="hasNote",
        limit=20,
    ) == notes_before
    assert "Replacement Alias" not in {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            concept_id,
            predicate="hasName",
            limit=20,
        )
    }


@pytest.mark.parametrize(
    ("workflow_id", "entity_domain", "entity_type_id", "entity_name", "requested_fact"),
    [
        (
            COMPANY_REPRESENTATION_WORKFLOW_ID,
            "company",
            "#V#organisation",
            "Ledger Research Ltd",
            {
                "claim_text": "Ledger Research Ltd is headquartered in Auckland.",
                "relation_hint": "headquarters",
                "target_name": "Auckland",
                "evidence_text": "The supplied company profile names Auckland.",
            },
        ),
        (
            EVENT_REPRESENTATION_WORKFLOW_ID,
            "event",
            "#V#event",
            "Ledger Systems Workshop",
            {
                "claim_text": "Ledger Systems Workshop is organised by Example Labs.",
                "relation_hint": "organiser",
                "target_name": "Example Labs",
                "evidence_text": "The supplied event page names Example Labs.",
            },
        ),
        (
            PLACE_REPRESENTATION_WORKFLOW_ID,
            "place",
            "#V#place",
            "Ledger Research Park",
            {
                "claim_text": "Ledger Research Park is located in Auckland.",
                "relation_hint": "located in",
                "target_name": "Auckland",
                "evidence_text": "The supplied place record names Auckland.",
            },
        ),
    ],
)
def test_nonperson_core_cannot_complete_richer_requested_facts(
    workflow_id: str,
    entity_domain: str,
    entity_type_id: str,
    entity_name: str,
    requested_fact: dict[str, str],
) -> None:
    bootstrap_canonical_entity_representation_workflows()
    definition = load_workflow_definition_from_vontology(workflow_id)
    assert definition is not None

    concept_id = f"#V#existing_{entity_domain}_requested_fact_ledger"
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
        text="Original represented source note.",
        lang="en-NZ",
        garbage_collect=True,
    )

    requested_facts = [requested_fact]
    payload = {
        "ready_to_materialise": True,
        "needs_user_affirmation": False,
        "entity_name": entity_name,
        "entity_description": "Replacement prose must not count as enrichment.",
        "entity_aliases": ["Replacement Alias"],
        "entity_source_text": "Replacement source marker must not count as a fact.",
        "requested_facts": requested_facts,
        "response_text": "Provisional extraction response.",
    }
    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=30,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM([json.dumps(payload)]),
            user_namespace="#V#test_user",
            user_concept_id="#V#test_user",
        ),
        data={"prompt": f"Represent {entity_name} and its grounded relationship."},
    )

    assert result.completed is True
    assert "failed" not in str(result.final_state or "").lower()
    assert result.data.get("entity_representation_concept_id") == concept_id
    assert result.data.get("entity_representation_reused_existing") is True
    assert result.data.get("entity_representation_materialised") is False
    assert result.data.get("entity_core_representation_verified") is True
    assert result.data.get("entity_representation_readback_verified") is True
    assert result.data.get("entity_representation_verified") is False
    assert result.data.get("entity_representation_coverage") == "core_only"
    assert result.data.get("entity_representation_requested_facts_complete") is False
    assert result.data.get("entity_representation_requested_fact_count") == 1
    assert result.data.get("entity_representation_requested_facts_payload_valid") is True
    assert result.data.get("entity_representation_unresolved_requested_facts") == (
        requested_facts
    )
    assert result.data.get("follow_up_required") is True
    assert result.data.get("semantic_outcome") == "follow_up_required"
    assert "richer requested representation is not complete" in str(
        result.data.get("response_text") or ""
    )
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
    } == {"Original represented source note."}
    assert "Replacement Alias" not in {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            concept_id,
            predicate="hasName",
            limit=20,
        )
    }


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
        "requested_facts": [],
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
                "requested_facts": [],
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
