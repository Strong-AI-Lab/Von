"""Execution/continuity contracts; scripted synthesis is not model-efficacy evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import concept_service
from src.backend.services.lab_status_digest_workflow_vontology_service import (
    LAB_STATUS_DIGEST_WORKFLOW_ID,
    bootstrap_canonical_lab_status_digest_workflow,
)
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_mcp_tool_actions import (
    register_workflow_mcp_tool_actions,
)
from test_lab_status_digest_workflow_vontology_service import (
    _reset_mock_db,
)  # noqa: F401

ACTOR = "#V#continuity_test_operator"
PRODUCT = "#V#continuity_test_kestrel"
OTHER = "#V#continuity_test_tern"


class DigestWorld:
    """Fictional source adapter with the real text read/write gateway handlers."""

    def __init__(self):
        self.calls = []
        self.jira_rows = []
        self.syntheses = []
        self.readback_corruption = False
        catalogue = MethodCatalogue()
        defaults = build_default_catalogue()
        for name in (
            "fetch_concept_content",
            "get_text_relations",
            "upsert_singleton_text_relation",
            "get_text_relations_summary",
            "jira_search",
            "repo_dossier_git_metadata",
        ):
            method = defaults.get(name)
            real_handler = method.handler

            def handler(_name=name, _handler=real_handler, **kwargs):
                self.calls.append((_name, dict(kwargs)))
                if _name == "jira_search":
                    return {
                        "issues": [
                            {
                                **row,
                                "fields": {
                                    key: value
                                    for key, value in row.get("fields", {}).items()
                                    if key in kwargs["fields"]
                                },
                            }
                            for row in self.jira_rows
                        ],
                        "isLast": False,
                        "nextPageToken": "more",
                    }
                if _name == "repo_dossier_git_metadata":
                    return {
                        "success": True,
                        "commits": [],
                        "evidence_gap": "fictional repository",
                    }
                result = _handler(**kwargs)
                if (
                    _name == "upsert_singleton_text_relation"
                    and self.readback_corruption
                ):
                    upsert_singleton_text_relation(
                        subject_concept_id=kwargs["concept_id"],
                        predicate="hasContent",
                        text="stale content from another update",
                        lang="en-NZ",
                    )
                return result

            catalogue.register(replace(method, handler=handler))
        self.gateway = InternalMCPGateway(
            catalogue=catalogue, transport=InternalMCPTransport(), enabled=True
        )
        self.registry = ActionRegistry()
        register_workflow_mcp_tool_actions(self.registry)
        register_control_flow_actions(self.registry)

    def synthesise(self, request):
        self.syntheses.append(dict(request.data))
        return WorkflowActionResult(
            status="success",
            outputs={
                "validated_json": {
                    "schema_version": "status_digest_synthesis.v1",
                    "digest_markdown": request.data["fixture_next_text"],
                    "work_product_id": request.data.get(
                        "fixture_reported_product", request.data["work_product_id"]
                    ),
                    "evidence_window": "fictional checkpoint",
                    "unavailable_source_classes": [],
                },
            },
        )

    def run(self, product, text, **inputs):
        definition = load_workflow_definition_from_vontology(
            LAB_STATUS_DIGEST_WORKFLOW_ID
        )
        with override_current_actor(ACTOR):
            return WorkflowExecutor(registry=self.registry).run(
                definition,
                environment=WorkflowEnvironment(
                    llm_client=None,
                    gateway=self.gateway,
                    user_concept_id=ACTOR,
                    user_namespace=ACTOR,
                ),
                data={
                    "prompt": "Maintain this responsibility.",
                    "work_product_id": product,
                    "fixture_next_text": text,
                    **inputs,
                },
            )


@pytest.fixture
def world(monkeypatch, request):
    request.getfixturevalue("_reset_mock_db")
    report = bootstrap_canonical_lab_status_digest_workflow()
    assert report["success"], report
    for cid in (ACTOR, PRODUCT, OTHER):
        concept_service.create_concept(
            name=cid[3:],
            concept_id=cid,
            created_by_concept_id=ACTOR if cid != ACTOR else None,
        )
    world = DigestWorld()
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step", world.synthesise
    )
    return world


def texts(product):
    with override_current_actor(ACTOR):
        return [
            row["text"]
            for row in get_texts_for_concept(product, predicate="hasContent")
        ]


def test_fresh_execution_recovers_prior_product_and_preserves_separate_scope(world):
    first = "Bo statement [TASK-1] open. Internal 23 October; external 30 October."
    a = world.run(PRODUCT, first, jira_jql="key = TASK-1")
    assert a.completed, (a.error, a.data)
    assert a.data["text_effect_readback_verified"] is True
    assert texts(PRODUCT) == [first]
    b = world.run(OTHER, "Tern has different commitments.", jira_jql="key = TASK-2")
    assert b.completed, b.error
    changed = "Bo statement [TASK-1] open. Internal 20 October supersedes 23 [office-v2]; external 30 October. Cost unknown."
    c = world.run(
        PRODUCT, changed, jira_jql="key = TASK-1", evidence_sources=["office-v2"]
    )
    assert c.completed, (c.error, c.data)
    prior = world.syntheses[-1]["prior_digest_evidence"]
    assert first in str(prior)
    assert "Tern has different commitments." not in str(prior)
    assert texts(PRODUCT) == [changed]
    assert texts(OTHER) == ["Tern has different commitments."]
    queries = [payload["jql"] for tool, payload in world.calls if tool == "jira_search"]
    assert queries == ["key = TASK-1", "key = TASK-2", "key = TASK-1"]
    writes = [
        payload
        for tool, payload in world.calls
        if tool == "upsert_singleton_text_relation"
    ]
    assert [row["concept_id"] for row in writes] == [PRODUCT, OTHER, PRODUCT]
    assert all(row["provenance"]["work_product"] == row["concept_id"] for row in writes)


def test_stale_nonempty_readback_does_not_complete(world):
    world.readback_corruption = True
    result = world.run(PRODUCT, "New premise and revised readiness.")
    assert not result.completed
    assert result.data["text_effect_readback_verified"] is False
    assert (
        result.data["text_effect_readback_failure_code"]
        == "text_effect_exact_readback_missing"
    )


def test_synthesis_receives_stored_brief_and_jira_acceptance_evidence(world):
    # A descriptive concept header is not the maintained document. The live
    # scheduled failure had both, unlike the earlier content-only fixture.
    prior = "Previous checkpoint.\n" + ("Evidence and unresolved commitments.\n" * 160)
    prior += "TASK-1 remains unaccepted; TASK-2 awaits the replication result."
    with override_current_actor(ACTOR):
        for predicate, text in (
            ("hasDescription", "A source-linked readiness brief for this project."),
            ("hasContent", prior),
        ):
            upsert_singleton_text_relation(
                subject_concept_id=PRODUCT,
                predicate=predicate,
                text=text,
                lang="en-NZ",
            )
    description = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Candidate REJECTED despite issue Done."}
                ],
            }
        ],
    }
    world.jira_rows = [
        {
            "key": "TASK-1",
            "fields": {"status": {"name": "Done"}, "description": description},
        }
    ]
    result = world.run(PRODUCT, "Updated checkpoint.", jira_jql="key = TASK-1")
    assert result.completed, (result.error, result.data)
    synthesis = world.syntheses[-1]
    assert prior in [
        row["text"] for row in synthesis["prior_digest_evidence"]["relations"]
    ]
    assert (
        synthesis["jira_evidence"]["issues"][0]["fields"]["description"] == description
    )


def test_supplied_product_id_does_not_grant_another_actors_write_authority(world):
    private = "#V#continuity_other_actor_product"
    concept_service.create_concept(
        name="Other actor product",
        concept_id=private,
        created_by_concept_id="#V#another_actor",
    )
    result = world.run(private, "Should not be written")
    assert not result.completed
    # Text retrieval may return no visible rows rather than a concept lookup
    # error. The canonical write boundary still denies the foreign product.
    assert world.syntheses[-1]["prior_digest_evidence"]["relations"] == []
    with override_current_actor("#V#another_actor"):
        assert get_texts_for_concept(private, predicate="hasContent") == []


def test_due_schedule_runs_same_capability_with_continuity_inputs(world, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from src.backend.workflows.durable.models import WorkflowSchedule
    from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
    from src.backend.workflows.durable.scheduler import WorkflowScheduler
    from src.backend.workflows.durable.worker import DurableWorkflowWorker
    from src.backend.workflows.durable.registry_factory import (
        build_vontology_workflow_registry_snapshot,
    )
    from src.backend.server import utils_flask

    concept_service.create_concept(
        name="triggers workflow", concept_id="#V#triggers_workflow"
    )
    manager = WorkflowInstanceManager()
    checkpoint = datetime.now(timezone.utc) - timedelta(minutes=1)
    inputs = {
        "prompt": "Maintain Kestrel readiness.",
        "work_product_id": PRODUCT,
        "jira_jql": "key = TASK-1",
        "evidence_sources": ["office-v2"],
        "fixture_next_text": "Internal checkpoint revised; TASK-1 remains open.",
        "requested_model": "gpt-5.6-luna",
        "requested_client_type": "openai",
    }
    schedule = WorkflowSchedule.create_once(
        LAB_STATUS_DIGEST_WORKFLOW_ID,
        run_at=checkpoint,
        user_id=ACTOR,
        org_id=None,
        namespace=ACTOR,
        default_inputs=inputs,
    )
    with override_current_actor(ACTOR):
        saved_id = manager.create_schedule(schedule)
        saved = concept_service.get_concept_by_concept_id(saved_id)
        assert manager.get_schedule(saved_id).workflow_id, saved
        scheduler = WorkflowScheduler(manager)
        scheduler._process_due_schedules()
        instances = manager.list_instances(
            user_id=ACTOR, namespace=ACTOR, workflow_id=LAB_STATUS_DIGEST_WORKFLOW_ID
        )
    assert len(instances) == 1
    instance = instances[0]
    assert instance.schedule_id == saved_id
    assert instance.inputs["evidence_sources"] == ["office-v2"]
    assert instance.inputs["schedule_occurrence_id"]
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client", lambda **kw: None
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_parameters",
        lambda **kw: {},
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
        lambda: world.gateway,
    )
    monkeypatch.setattr(
        utils_flask,
        "_durable_workflow_registry",
        build_vontology_workflow_registry_snapshot(
            workflow_ids=[LAB_STATUS_DIGEST_WORKFLOW_ID]
        ),
    )
    worker = DurableWorkflowWorker(
        registry=world.registry,
        instance_manager=manager,
        definition_loader=utils_flask._get_durable_definition_loader(),
    )
    completed = []
    worker.set_callbacks(on_completed=lambda _id, result: completed.append(result))
    claimed = manager.find_and_claim_instance(
        worker.worker_id, worker_build_identity=worker.worker_build_identity
    )
    assert claimed is not None
    worker._process_instance(claimed)
    canonical = manager.get_instance(instance.instance_id)
    assert canonical.status.value == "completed", canonical.error
    assert len(completed) == 1
    result = completed[0]
    assert result.completed, (result.error, result.data)
    assert texts(PRODUCT) == [inputs["fixture_next_text"]]
    assert (
        world.syntheses[-1]["schedule_occurrence_id"]
        == instance.inputs["schedule_occurrence_id"]
    )
    scheduler._process_due_schedules()
    assert (
        len(
            manager.list_instances(
                user_id=ACTOR,
                namespace=ACTOR,
                workflow_id=LAB_STATUS_DIGEST_WORKFLOW_ID,
            )
        )
        == 1
    )


@pytest.mark.usefixtures("_reset_mock_db")
def test_reviewed_v7_upgrade_preserves_old_authored_prompt_and_digest():
    from pathlib import Path
    from src.backend.services.workflow_repo_seed_bootstrap import (
        bootstrap_repo_seed_workflow_bundle,
    )
    from src.backend.services import (
        lab_status_digest_workflow_vontology_service as service,
    )
    from src.backend.services.workflow_prompt_authority_service import (
        ensure_prompt_concept_support,
        WorkflowPromptConceptSpec,
        DEFAULT_PROMPT_TYPE_ID,
    )

    old_id = "#V#lab_status_digest_prompt"
    ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=old_id,
                name="Old digest prompt",
                description="Prior authority",
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source="test_fixture",
    )
    upsert_singleton_text_relation(
        subject_concept_id=old_id,
        predicate="hasContent",
        text="Locally authored old prompt",
        lang="en-NZ",
    )
    old = bootstrap_repo_seed_workflow_bundle(
        asset_path=Path(__file__).parents[1]
        / "fixtures/workflows/lab_status_digest_v7.json",
        target_workflow_ids=(LAB_STATUS_DIGEST_WORKFLOW_ID,),
    )
    assert old["publication"]["counts"]["errors"] == 0, old
    upsert_singleton_text_relation(
        subject_concept_id=service.LAB_STATUS_DIGEST_WORK_PRODUCT_ID,
        predicate="hasContent",
        text="Outstanding historical commitment",
        lang="en-NZ",
    )
    upgraded = service.bootstrap_canonical_lab_status_digest_workflow()
    assert upgraded["success"], upgraded
    assert upgraded["publication"]["counts"]["workflows_published"] == 1, upgraded
    assert (
        get_texts_for_concept(old_id, predicate="hasContent")[0]["text"]
        == "Locally authored old prompt"
    )
    assert (
        get_texts_for_concept(
            service.LAB_STATUS_DIGEST_WORK_PRODUCT_ID, predicate="hasContent"
        )[0]["text"]
        == "Outstanding historical commitment"
    )


def test_model_reported_identity_cannot_redirect_the_selected_write(world):
    result = world.run(
        PRODUCT, "Selected responsibility updated.", fixture_reported_product=OTHER
    )
    assert result.completed, result.error
    assert result.data["work_product_id"] == PRODUCT
    assert texts(PRODUCT) == ["Selected responsibility updated."]
    assert texts(OTHER) == []
