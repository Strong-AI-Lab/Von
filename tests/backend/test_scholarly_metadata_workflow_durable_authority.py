from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.paper_representation_workflow_vontology_service import (
    SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows import workflow_concept_authority_service
from src.backend.workflows.durable import registry_factory
from src.backend.workflows.durable.durable_executor import DurableWorkflowExecutor
from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
from src.backend.workflows.durable.workflow_instance_submission_service import (
    invalidate_workflow_runnable_verification_cache,
    submit_verified_workflow_instance,
)
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)

_ACTOR_ID = "#V#durable_paper_actor"
_ORGANISATION_ID = "#V#durable_paper_org"
_NAMESPACE = f"{_ACTOR_ID}@{_ORGANISATION_ID.removeprefix('#V#')}"


@pytest.fixture
def _reset_durable_paper_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    workflow_concept_authority_service.clear_workflow_type_resolution_cache()
    invalidate_workflow_runnable_verification_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in (
            "concepts",
            "text_relations",
            "text_values",
            "ontology_authority_delegations",
            "ontology_mutation_receipts",
            "workflow_instances",
            "workflow_executions",
        ):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield
    invalidate_workflow_runnable_verification_cache()
    workflow_concept_authority_service.clear_workflow_type_resolution_cache()


def test_durable_submitted_metadata_workflow_uses_bound_private_actor_authority(
    _reset_durable_paper_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the normal persisted workflow route, without an injected grant."""

    bootstrap = bootstrap_canonical_paper_representation_workflows()
    assert (bootstrap.get("publication") or {}).get("counts", {}).get("errors") == 0
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None
    registry = registry_factory.build_durable_action_registry()
    manager = WorkflowInstanceManager()
    submission = submit_verified_workflow_instance(
        manager=manager,
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        user_id=_ACTOR_ID,
        org_id=_ORGANISATION_ID,
        namespace=_NAMESPACE,
        inputs={
            "paper_metadata": {
                "title": "Durably Bound Private Workflow Authority",
                "abstract": (
                    "The canonical durable route should complete this private "
                    "additive representation without delegation ceremony."
                ),
            },
            "require_representation_evidence_summary": False,
        },
        action_registry_override=registry,
    )
    assert submission.success is True, submission.to_dict()
    assert submission.instance_id is not None

    persisted = manager.get_instance(submission.instance_id)
    assert persisted is not None
    assert persisted.user_id == _ACTOR_ID
    assert persisted.org_id == _ORGANISATION_ID
    assert persisted.namespace == _NAMESPACE
    assert "ontology_delegation_id" not in persisted.inputs

    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda **_kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_parameters",
        lambda **_kwargs: {},
    )
    identity = build_workflow_definition_identity(
        workflow_id=definition.workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )

    result = DurableWorkflowExecutor(
        registry=registry,
        instance_manager=manager,
        max_transitions=50,
    ).run_durable(
        submission.instance_id,
        definition,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True, result.error
    assert "ontology_agent_delegation_required" not in str(result.error or "")
    paper_concept_id = result.data.get("paper_concept_id")
    assert isinstance(paper_concept_id, str) and paper_concept_id
    paper = concept_service.get_concept_by_concept_id(paper_concept_id)
    assert paper is not None
    descriptions = {
        str(row.get("text") or "")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasDescription",
            limit=20,
        )
    }
    assert (
        "The canonical durable route should complete this private additive "
        "representation without delegation ceremony."
    ) in descriptions
    terminal = manager.get_instance(submission.instance_id)
    assert terminal is not None
    # The worker owns the later instance-status transition; the durable
    # executor owns and has persisted the terminal checkpoint proved here.
    assert terminal.current_state == result.final_state
    assert terminal.workflow_data.get("paper_concept_id") == paper_concept_id
