from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.text_value_service import upsert_singleton_text_relation
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

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


def test_publish_canonical_graphs_preserves_step_prompt_links(
    _reset_mock_db: Any,
) -> None:
    workflow_id = "#V#prompt_link_publication_test_workflow"
    prompt_concept_id = "#V#prompt_link_publication_test_prompt"
    concept_service.create_concept(
        name="Prompt link publication test prompt",
        concept_id=prompt_concept_id,
        description="Prompt used to verify canonical step prompt publication.",
        parent_concept_ids=["#V#prompt_for_llm"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
    )
    upsert_singleton_text_relation(
        subject_concept_id=prompt_concept_id,
        predicate="hasContent",
        text="Return JSON only.",
        lang="en-NZ",
        garbage_collect=True,
    )

    specs = {
        workflow_id: authority_service._CanonicalWorkflowPublicationSpec(
            initial_state="infer",
            steps=(
                authority_service._CanonicalStepPublicationSpec(
                    state_id="infer",
                    action_id="llm.action",
                    prompt_concept_ids=(prompt_concept_id,),
                    execution_mode="llm",
                    validation_policy={"output_format": "json_value"},
                    next_state="complete",
                ),
                authority_service._CanonicalStepPublicationSpec(state_id="complete"),
            ),
        )
    }

    report = authority_service.publish_canonical_chat_workflow_graphs(
        target_workflow_ids=[workflow_id],
        publication_specs=specs,
        publication_definitions=authority_service._build_definition_map_from_publication_specs(
            specs
        ),
        publication_purposes={workflow_id: "Prompt publication test workflow."},
    )

    assert (report.get("counts") or {}).get("workflows_published") == 1
    definition = load_workflow_definition_from_vontology(workflow_id)
    assert definition is not None
    step_id = authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id="infer",
    )
    action = definition.states[step_id].actions[0]
    assert action.action_id == "llm.action"
    assert action.execution_mode == "llm"
    prompt_contract = action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == prompt_concept_id


def test_publish_canonical_graphs_removes_undeclared_step_prompt_links(
    _reset_mock_db: Any,
) -> None:
    workflow_id = "#V#prompt_link_reconciliation_test_workflow"
    prompt_concept_id = "#V#prompt_link_reconciliation_test_prompt"
    concept_service.create_concept(
        name="Prompt link reconciliation test prompt",
        concept_id=prompt_concept_id,
        description="Prompt used to verify exact canonical prompt reconciliation.",
        parent_concept_ids=["#V#prompt_for_llm"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
    )
    upsert_singleton_text_relation(
        subject_concept_id=prompt_concept_id,
        predicate="hasContent",
        text="Return JSON only.",
        lang="en-NZ",
        garbage_collect=True,
    )

    prompt_spec = authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="infer",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="infer",
                action_id="llm.action",
                prompt_concept_ids=(prompt_concept_id,),
                execution_mode="llm",
                validation_policy={"output_format": "json_value"},
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
        ),
    )
    authority_service.publish_canonical_chat_workflow_graphs(
        target_workflow_ids=[workflow_id],
        publication_specs={workflow_id: prompt_spec},
        publication_definitions=authority_service._build_definition_map_from_publication_specs(
            {workflow_id: prompt_spec}
        ),
        validate_after_publish=False,
    )

    deterministic_spec = authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="infer",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="infer",
                action_id="workflow_control.kr_relationship_resolution",
                execution_mode="deterministic",
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
        ),
    )
    report = authority_service.publish_canonical_chat_workflow_graphs(
        target_workflow_ids=[workflow_id],
        publication_specs={workflow_id: deterministic_spec},
        publication_definitions=authority_service._build_definition_map_from_publication_specs(
            {workflow_id: deterministic_spec}
        ),
        validate_after_publish=False,
    )

    assert (report.get("counts") or {}).get("workflows_published") == 1
    definition = load_workflow_definition_from_vontology(workflow_id)
    assert definition is not None
    step_id = authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id="infer",
    )
    action = definition.states[step_id].actions[0]
    assert action.action_id == "workflow_control.kr_relationship_resolution"
    assert action.execution_mode == "deterministic"
    assert action.prompt_contract is None
