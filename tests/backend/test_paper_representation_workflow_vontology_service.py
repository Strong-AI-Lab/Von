from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.arxiv_ingestion_testing_service import (
    cleanup_arxiv_paper_ingestion_test_artifacts,
    prepare_arxiv_paper_ingestion_test_fixture,
    verify_arxiv_paper_ingestion_test_result,
)
from src.backend.services.paper_representation_workflow_vontology_service import (
    ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID,
    PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
    SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
    SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
    SCHOLARLY_PUBLIC_AUTHOR_REPRESENTATION_WORKFLOW_ID,
    SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
    SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
    diff_canonical_paper_representation_workflow_repo_seed_bundle,
    export_canonical_paper_representation_workflow_repo_seed_bundle,
)
from src.backend.services.text_value_service import (
    delete_text_relation,
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable import registry_factory
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.paper_representation_workflow import (
    register_paper_representation_actions,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    evaluate_transition_condition_spec,
)
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_contract,
    resolve_workflow_launch_input_contract,
    resolve_workflow_publication_lifecycle,
    resolve_workflow_routing_profile,
)
from src.backend.workflows.workflow_launch_input_contracts import (
    resolve_workflow_launch_inputs,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PAPER_REPO_SEED_ASSET_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "paper_representation_workflow_seed_bundle.json"
)
_PAPER_SEED_INPUT_PATHS: tuple[Path, ...] = (
    _PAPER_REPO_SEED_ASSET_PATH,
    _PAPER_REPO_SEED_ASSET_PATH.with_name(
        "prompt_scholarly_article_metadata_extraction_seed.md"
    ),
    _PAPER_REPO_SEED_ASSET_PATH.with_name(
        "prompt_scholarly_article_representation_evidence_summary_seed.md"
    ),
    _PAPER_REPO_SEED_ASSET_PATH.with_name(
        "prompt_scholarly_article_outcome_explanation_seed.md"
    ),
)
_PAPER_MOCK_COLLECTION_NAMES: tuple[str, ...] = (
    "concepts",
    "text_relations",
    "text_values",
    "scoped_knowledge_assertions",
    "ontology_mutation_receipts",
)
_canonical_seed_snapshot: dict[str, dict[str, Any]] | None = None
_canonical_seed_snapshot_fingerprint: str | None = None
_canonical_seed_fixture_bootstrap_count = 0
_LIVE_ARXIV_ACCEPTANCE_PRIMARY_PAPER_ENV = "VON_LIVE_ARXIV_ACCEPTANCE_PAPER"
_LIVE_ARXIV_ACCEPTANCE_SAMPLE_ENV = "VON_LIVE_ARXIV_ACCEPTANCE_SAMPLE"
_LIVE_ARXIV_ACCEPTANCE_RUN_ENV = "VON_RUN_LIVE_ARXIV_WORKFLOW_ACCEPTANCE"
_LIVE_ARXIV_ACCEPTANCE_BATCH_RUN_ENV = "VON_RUN_LIVE_ARXIV_WORKFLOW_ACCEPTANCE_BATCH"
_LIVE_ARXIV_ACCEPTANCE_PRIMARY_PAPER = "2505.14396"
_LIVE_ARXIV_ACCEPTANCE_SAMPLE: tuple[str, ...] = (
    "2505.14396",
    "2402.18144",
    "2404.12494",
    "2411.04983",
    "2603.01896",
    "2603.21702",
    "2602.20478",
    "2510.06248",
    "2512.23959",
    "2506.16596",
)
_LIVE_ARXIV_ACCEPTANCE_USER_ID = "#V#michael_witbrock"
_LIVE_ARXIV_ACCEPTANCE_ORG_ID = "#V#university_of_auckland_strong_ai_lab"
_LIVE_ARXIV_ACCEPTANCE_NAMESPACE = f"{_LIVE_ARXIV_ACCEPTANCE_USER_ID}@{_LIVE_ARXIV_ACCEPTANCE_ORG_ID.removeprefix('#V#')}"


class _EvidenceSummaryLLM:
    def generate(self, prompt: str, context=None, model=None, llm_params=None) -> str:
        assert "scholarly article representation workflow" in prompt.lower()
        return json.dumps(
            {
                "response_text": (
                    "Created the paper concept and read back the supplied "
                    "metadata evidence."
                ),
                "observations": [
                    {
                        "label": "paper_metadata_readback",
                        "verdict": "pass",
                        "expected_outcome": "paper concept metadata read-back",
                        "observed_outcome": "workflow context includes article read-back",
                    }
                ],
                "metadata_verification": {
                    "paper_concept_id": "from_context",
                    "author_concept_ids": "from_context",
                    "doi": "from_context",
                    "source_uri": "from_context",
                    "readback_present": True,
                },
                "verification_passed": True,
                "reasoning": "The workflow context contains read-back evidence.",
            }
        )


def _upsert_workflow_json_text(
    *,
    workflow_id: str,
    predicate: str,
    payload: dict[str, Any],
) -> None:
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate=predicate,
        text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"source": "test_paper_representation_workflow_vontology_service"},
        garbage_collect=True,
    )


def _delete_workflow_text_relations(
    *,
    workflow_id: str,
    predicate: str,
) -> None:
    rows = get_texts_for_concept(workflow_id, predicate=predicate, limit=20)
    assert rows, {"workflow_id": workflow_id, "predicate": predicate}
    for row in rows:
        relation_id = str((row or {}).get("relation_id") or "").strip()
        assert relation_id, {"workflow_id": workflow_id, "predicate": predicate}
        delete_text_relation(workflow_id, relation_id, garbage_collect=True)


def _snapshot_concept_text_relations(
    concept_id: str,
) -> list[tuple[str, str, str, str]]:
    return sorted(
        (
            str(row.get("predicate") or ""),
            str(row.get("text") or ""),
            str(row.get("lang") or ""),
            json.dumps(row.get("context") or {}, sort_keys=True),
        )
        for row in get_texts_for_concept(concept_id, limit=200)
    )


def _live_acceptance_enabled(*, batch: bool = False) -> bool:
    env_name = (
        _LIVE_ARXIV_ACCEPTANCE_BATCH_RUN_ENV
        if batch
        else _LIVE_ARXIV_ACCEPTANCE_RUN_ENV
    )
    return os.getenv(env_name) == "1"


def _skip_live_acceptance(*, batch: bool = False) -> None:
    if batch:
        pytest.skip(
            f"Set {_LIVE_ARXIV_ACCEPTANCE_BATCH_RUN_ENV}=1 to run live arXiv batch acceptance."
        )
    pytest.skip(f"Set {_LIVE_ARXIV_ACCEPTANCE_RUN_ENV}=1 to run live arXiv acceptance.")


def _dedupe_case_entries(entries: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw_entry in entries:
        entry = str(raw_entry).strip()
        if not entry:
            continue
        lowered = entry.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(entry)
    return deduped


def _parse_live_arxiv_acceptance_entries(
    value: str | None,
    *,
    default_entries: tuple[str, ...],
) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return list(default_entries)

    entries: list[str] = []
    for line in value.replace("\r", "\n").split("\n"):
        for item in line.split(","):
            text = item.strip()
            if text:
                entries.append(text)
    return _dedupe_case_entries(entries) or list(default_entries)


def _resolve_live_arxiv_acceptance_primary_paper() -> str:
    entries = _parse_live_arxiv_acceptance_entries(
        os.getenv(_LIVE_ARXIV_ACCEPTANCE_PRIMARY_PAPER_ENV),
        default_entries=(_LIVE_ARXIV_ACCEPTANCE_PRIMARY_PAPER,),
    )
    return entries[0]


def _resolve_live_arxiv_acceptance_sample() -> list[str]:
    return _parse_live_arxiv_acceptance_entries(
        os.getenv(_LIVE_ARXIV_ACCEPTANCE_SAMPLE_ENV),
        default_entries=_LIVE_ARXIV_ACCEPTANCE_SAMPLE,
    )


def _normalise_live_arxiv_source_uri(paper_ref: str) -> str:
    text = str(paper_ref).strip()
    if "://" in text:
        return text
    return f"https://arxiv.org/abs/{text}"


def _build_live_arxiv_prompt_text(paper_ref: str) -> str:
    return f"Represent this paper {_normalise_live_arxiv_source_uri(paper_ref)}"


def _prepare_live_arxiv_acceptance_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "0")

    bootstrap_canonical_paper_representation_workflows()
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None
    return definition


def _execute_live_arxiv_acceptance_case(
    *,
    definition: Any,
    paper_ref: str,
) -> dict[str, Any]:
    prompt_text = _build_live_arxiv_prompt_text(paper_ref)
    source_uri = _normalise_live_arxiv_source_uri(paper_ref)
    fixture = prepare_arxiv_paper_ingestion_test_fixture(
        prompt_text=prompt_text,
        source_uri=source_uri,
        arxiv_id=None if "://" in paper_ref else paper_ref,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        timeout_seconds=45.0,
        repair_existing_artifacts=True,
    )
    if fixture.get("success") is not True:
        return {
            "paper_ref": paper_ref,
            "stage": "prepare_fixture",
            "success": False,
            "fixture": fixture,
        }

    result: Any | None = None
    verification: dict[str, Any] | None = None
    cleanup: dict[str, Any] | None = None

    try:
        result = WorkflowExecutor(
            registry=registry_factory.build_durable_action_registry(),
            max_transitions=40,
        ).run(
            definition,
            environment=WorkflowEnvironment(
                llm_client=_EvidenceSummaryLLM(),
                user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
                user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
                org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
            ),
            data={
                "prompt": fixture["prompt_text"],
                "arxiv_id": fixture["arxiv_id"],
                "source_uri": fixture["source_uri"],
                "original_filename": fixture["original_filename"],
            },
        )

        verification = verify_arxiv_paper_ingestion_test_result(
            workflow_execution={
                "completed": result.completed,
                "final_state": result.final_state,
                "error": result.error,
                "final_status": (
                    "completed"
                    if result.final_state
                    == authority_service._step_concept_id(
                        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                        state_id="completed",
                    )
                    else "failed"
                ),
                "outputs": dict(result.data),
            },
            arxiv_id=fixture["arxiv_id"],
            source_uri=fixture["source_uri"],
            expected_title=fixture["expected_title"],
            expected_summary=fixture["expected_summary"],
            expected_publication_date=fixture["expected_publication_date"],
            expected_author_names=fixture["expected_author_names"],
            expected_author_concept_ids=fixture["expected_author_concept_ids"],
            expected_topic_labels=fixture["expected_topic_labels"],
            expected_topic_concept_ids=fixture["expected_topic_concept_ids"],
            paper_concept_id=fixture["paper_concept_id"],
        )
    except Exception as exc:
        cleanup = cleanup_arxiv_paper_ingestion_test_artifacts(
            paper_concept_id=fixture["paper_concept_id"],
            file_copy_concept_id=None,
            author_concept_ids=fixture["expected_author_concept_ids"],
            topic_concept_ids=fixture["expected_topic_concept_ids"],
            preexisting_author_concept_ids=fixture["preexisting_author_concept_ids"],
            preexisting_topic_concept_ids=fixture["preexisting_topic_concept_ids"],
        )
        return {
            "paper_ref": paper_ref,
            "arxiv_id": fixture.get("arxiv_id"),
            "source_uri": fixture.get("source_uri"),
            "success": False,
            "stage": "workflow_execution_exception",
            "error": f"{type(exc).__name__}: {exc}",
            "fixture": fixture,
            "cleanup": cleanup,
        }

    cleanup = cleanup_arxiv_paper_ingestion_test_artifacts(
        paper_concept_id=verification.get("paper_concept_id")
        or fixture["paper_concept_id"],
        file_copy_concept_id=verification.get("file_copy_concept_id"),
        author_concept_ids=fixture["expected_author_concept_ids"],
        topic_concept_ids=fixture["expected_topic_concept_ids"],
        preexisting_author_concept_ids=fixture["preexisting_author_concept_ids"],
        preexisting_topic_concept_ids=fixture["preexisting_topic_concept_ids"],
    )

    success = (
        result.completed is True
        and result.final_state
        == authority_service._step_concept_id(
            workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
            state_id="completed",
        )
        and verification.get("verification_passed") is True
        and cleanup.get("cleanup_passed") is True
    )
    return {
        "paper_ref": paper_ref,
        "arxiv_id": fixture.get("arxiv_id"),
        "source_uri": fixture.get("source_uri"),
        "success": success,
        "stage": "complete" if success else "verification_or_cleanup",
        "workflow_completed": result.completed,
        "workflow_final_state": result.final_state,
        "workflow_error": result.error,
        "verification_passed": verification.get("verification_passed"),
        "cleanup_passed": cleanup.get("cleanup_passed"),
        "fixture": fixture,
        "verification": verification,
        "cleanup": cleanup,
    }


def _paper_seed_input_fingerprint(
    paths: tuple[Path, ...] = _PAPER_SEED_INPUT_PATHS,
) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _invalidate_paper_workflow_test_caches() -> None:
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        invalidate_workflow_runnable_verification_cache,
    )
    from src.backend.workflows.workflow_action_contracts import (
        invalidate_workflow_action_contract_resolution_cache,
    )

    authority_service.clear_workflow_type_resolution_cache()
    invalidate_workflow_discovery_executability_caches()
    invalidate_workflow_action_contract_resolution_cache()
    invalidate_workflow_runnable_verification_cache(
        reason="paper_workflow_test_fixture_boundary"
    )
    registry_factory._resolve_subworkflow_definition.cache_clear()
    registry_factory.invalidate_shared_workflow_registry_read_only()


def _drop_paper_mock_collections(db: Any) -> None:
    for collection_name in _PAPER_MOCK_COLLECTION_NAMES:
        db.drop_collection(collection_name)


def _snapshot_paper_mock_collections(db: Any) -> dict[str, dict[str, Any]]:
    return {
        collection_name: {
            "documents": copy.deepcopy(list(db[collection_name].find({}))),
            "indexes": copy.deepcopy(db[collection_name].index_information()),
        }
        for collection_name in _PAPER_MOCK_COLLECTION_NAMES
    }


def _restore_paper_mock_collections(
    db: Any,
    snapshot: dict[str, dict[str, Any]],
) -> None:
    _drop_paper_mock_collections(db)
    for collection_name in _PAPER_MOCK_COLLECTION_NAMES:
        collection = db[collection_name]
        collection_snapshot = snapshot[collection_name]
        for index_name, raw_spec in collection_snapshot["indexes"].items():
            if index_name == "_id_":
                continue
            spec = copy.deepcopy(raw_spec)
            keys = spec.pop("key")
            spec.pop("v", None)
            spec.pop("ns", None)
            collection.create_index(keys, name=index_name, **spec)
        documents = copy.deepcopy(collection_snapshot["documents"])
        if documents:
            collection.insert_many(documents)


def _get_canonical_seed_snapshot(db: Any) -> dict[str, dict[str, Any]]:
    global _canonical_seed_fixture_bootstrap_count
    global _canonical_seed_snapshot
    global _canonical_seed_snapshot_fingerprint

    fingerprint = _paper_seed_input_fingerprint()
    if (
        _canonical_seed_snapshot is not None
        and _canonical_seed_snapshot_fingerprint != fingerprint
    ):
        raise AssertionError(
            "Paper workflow seed inputs changed after the worker-local canonical "
            "fixture was built; start a new pytest process to rebuild authority."
        )
    if _canonical_seed_snapshot is None:
        _drop_paper_mock_collections(db)
        _invalidate_paper_workflow_test_caches()
        report = bootstrap_canonical_paper_representation_workflows()
        publication_counts = (report.get("publication") or {}).get("counts") or {}
        assert publication_counts.get("errors") == 0, json.dumps(
            report, sort_keys=True, default=str
        )
        assert publication_counts.get("workflows_published") == 8, json.dumps(
            report, sort_keys=True, default=str
        )
        _canonical_seed_snapshot = _snapshot_paper_mock_collections(db)
        _canonical_seed_snapshot_fingerprint = fingerprint
        _canonical_seed_fixture_bootstrap_count += 1
    assert _canonical_seed_fixture_bootstrap_count == 1
    return _canonical_seed_snapshot


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.services import ontology_publication_authority_service

    monkeypatch.setattr(
        ontology_publication_authority_service,
        "resolve_live_semantic_roles",
        lambda actor_id: (
            (
                ontology_publication_authority_service.AuthorityRoleEvidence(
                    role=(
                        ontology_publication_authority_service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
                    ),
                    actor_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
                    organisation_concept_id=None,
                    relation_id="test:paper-workflow-global-authority",
                    revision="test-revision-1",
                ),
            )
            if actor_id == _LIVE_ARXIV_ACCEPTANCE_USER_ID
            else ()
        ),
    )

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        _drop_paper_mock_collections(db)
    _invalidate_paper_workflow_test_caches()
    yield
    _invalidate_paper_workflow_test_caches()


@pytest.fixture
def _canonical_seeded_mock_db(_reset_mock_db: Any):
    from src.backend.db.mongo_client import get_db

    db = get_db()
    assert db is not None
    snapshot = _get_canonical_seed_snapshot(db)
    _restore_paper_mock_collections(db, snapshot)
    _invalidate_paper_workflow_test_caches()
    assert _snapshot_paper_mock_collections(db) == snapshot
    yield snapshot
    _restore_paper_mock_collections(db, snapshot)
    _invalidate_paper_workflow_test_caches()
    assert _snapshot_paper_mock_collections(db) == snapshot


def test_bootstrap_materialises_paper_representation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_paper_representation_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 8, json.dumps(
        report, sort_keys=True, default=str
    )
    assert counts.get("errors") == 0
    support_concepts = report.get("support_concepts") or {}
    assert support_concepts.get("errors") == []
    publication_scope_profiles = report.get("publication_scope_profiles") or {}
    assert publication_scope_profiles.get("success") is True
    created_support_ids = {
        *(support_concepts.get("created_concept_ids") or []),
        *(publication_scope_profiles.get("created_concept_ids") or []),
    }
    assert "#V#scholarly_article" in created_support_ids
    assert "#V#person" in created_support_ids
    assert "#V#research_topic" in created_support_ids
    assert "#V#paper_on_arxiv" in created_support_ids
    assert "#V#paper_under_preparation" in created_support_ids
    assert "#V#authored_by" in created_support_ids
    assert "#V#about" in created_support_ids
    assert "#V#has_doi" in created_support_ids
    assert "#V#has_source_uri" in created_support_ids
    for predicate_id in (
        "#V#authored_by",
        "#V#about",
        "#V#has_doi",
        "#V#has_source_uri",
    ):
        predicate_doc = concept_service.get_concept_by_concept_id(predicate_id)
        assert predicate_doc is not None
        assert "#V#predicate" in (
            (predicate_doc.get("relationships") or {}).get("is_an_instance_of") or []
        )
    prompt_support = report.get("prompt_support") or {}
    assert prompt_support.get("success") is True
    metadata_prompt_rows = get_texts_for_concept(
        "#V#prompt_scholarly_article_metadata_extraction",
        predicate="hasContent",
        limit=2,
    )
    assert metadata_prompt_rows
    evidence_prompt_rows = get_texts_for_concept(
        "#V#prompt_scholarly_article_representation_evidence_summary",
        predicate="hasContent",
        limit=2,
    )
    assert evidence_prompt_rows
    outcome_prompt_rows = get_texts_for_concept(
        "#V#prompt_scholarly_article_outcome_explanation",
        predicate="hasContent",
        limit=2,
    )
    assert outcome_prompt_rows

    from src.backend.workflows.outcome_explanation_prompt_support import (
        resolve_workflow_outcome_explanation_prompt_support,
    )

    outcome_prompt_support = resolve_workflow_outcome_explanation_prompt_support(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert outcome_prompt_support is not None
    assert set(outcome_prompt_support["prompts"]) >= {
        "default",
        "failed",
        "not_started",
        "partial",
    }
    assert outcome_prompt_support["prompts"]["failed"]["prompt_concept_id"] == (
        "#V#prompt_scholarly_article_outcome_explanation"
    )

    metadata_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert metadata_definition is not None

    public_author_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_PUBLIC_AUTHOR_REPRESENTATION_WORKFLOW_ID
    )
    assert public_author_definition is not None

    scholarly_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_definition is not None

    arxiv_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert arxiv_definition is not None
    under_preparation_definition = load_workflow_definition_from_vontology(
        PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID
    )
    assert under_preparation_definition is not None
    source_neutral_definition = load_workflow_definition_from_vontology(
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID
    )
    assert source_neutral_definition is not None
    item_definition = load_workflow_definition_from_vontology(
        SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID
    )
    assert item_definition is not None
    public_title_definition = load_workflow_definition_from_vontology(
        PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID
    )
    assert public_title_definition is not None
    source_initial_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        state_id="normalise_reference_set",
    )
    source_dispatch_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        state_id="dispatch_reference_items",
    )
    assert source_neutral_definition.initial_state == source_initial_state_id
    source_initial_action = source_neutral_definition.states[
        source_initial_state_id
    ].actions[0]
    assert source_initial_action.action_id == "paper_reference.normalise_reference_set"
    source_dispatch_action = source_neutral_definition.states[
        source_dispatch_state_id
    ].actions[0]
    assert source_dispatch_action.action_id == "workflow_control.for_each"
    assert source_dispatch_action.inputs["workflow_id"] == (
        SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID
    )
    assert source_dispatch_action.inputs["success_policy"] == "allow_partial"
    source_dispatch_transitions = {
        transition.reason: transition
        for transition in source_neutral_definition.states[
            source_dispatch_state_id
        ].transitions
    }
    assert source_dispatch_transitions[
        "paper_reference_items_produced_success"
    ].condition_spec == {
        "key": "for_each_success_count",
        "kind": "context_compare",
        "operator": "gt",
        "value": 0,
    }
    assert source_dispatch_transitions[
        "paper_reference_items_produced_success"
    ].to_state == authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        state_id="completed",
    )
    assert source_dispatch_transitions[
        "paper_reference_items_produced_no_success"
    ].to_state == authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        state_id="failed",
    )
    assert (
        source_neutral_definition.metadata["terminal_success_contract"][
            "failed_terminal_error_code"
        ]
        == "paper_reference_ingestion_no_items_succeeded"
    )
    source_required_effects = source_neutral_definition.metadata.get(
        "required_effects_contract"
    )
    assert isinstance(source_required_effects, dict)
    assert {
        effect.get("postcondition_strategy")
        for effect in source_required_effects.get("required_effects") or []
        if isinstance(effect, dict)
    } == {"execution_observed"}

    item_prepare_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
        state_id="prepare_reference_context",
    )
    item_prepare_transitions = {
        transition.reason: transition
        for transition in item_definition.states[item_prepare_state_id].transitions
    }
    assert item_prepare_transitions["arxiv_reference"].to_state == (
        authority_service._step_concept_id(
            workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
            state_id="ingest_arxiv_reference",
        )
    )
    assert item_prepare_transitions["doi_reference"].to_state == (
        authority_service._step_concept_id(
            workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
            state_id="ingest_metadata_reference",
        )
    )
    assert item_prepare_transitions["paper_under_preparation_reference"].to_state == (
        authority_service._step_concept_id(
            workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
            state_id="ingest_under_preparation_reference",
        )
    )
    assert item_prepare_transitions["file_copy_reference"].to_state == (
        authority_service._step_concept_id(
            workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
            state_id="ingest_file_copy_reference",
        )
    )
    ingest_arxiv_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
        state_id="ingest_arxiv_reference",
    )
    ingest_arxiv_state = item_definition.states[ingest_arxiv_state_id]
    assert ingest_arxiv_state.actions[0].action_id == "workflow_invoke_subworkflow"
    assert ingest_arxiv_state.metadata["subworkflow_contract"]["workflow_id"] == (
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    ingest_under_preparation_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
        state_id="ingest_under_preparation_reference",
    )
    assert (
        item_definition.states[ingest_under_preparation_state_id].metadata[
            "subworkflow_contract"
        ]["workflow_id"]
        == PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID
    )
    under_preparation_scope_state_id = authority_service._step_concept_id(
        workflow_id=PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID,
        state_id="resolve_under_preparation_scope",
    )
    under_preparation_scope_action = under_preparation_definition.states[
        under_preparation_scope_state_id
    ].actions[0]
    assert under_preparation_scope_action.action_id == (
        "resolve_publication_scope_profile"
    )
    assert under_preparation_scope_action.inputs["plane"] == "instance"
    assert under_preparation_scope_action.inputs["type_concept_ids"] == [
        "#V#paper_under_preparation"
    ]
    under_preparation_create_state_id = authority_service._step_concept_id(
        workflow_id=PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID,
        state_id="create_under_preparation_concept",
    )
    under_preparation_create_action = under_preparation_definition.states[
        under_preparation_create_state_id
    ].actions[0]
    assert under_preparation_create_action.action_id == "create_concepts"
    assert under_preparation_create_action.inputs["parent_id"] == (
        "#V#paper_under_preparation"
    )
    assert under_preparation_create_action.inputs["scope_mode"]["$context_key"] == (
        "paper_instance_scope_mode"
    )
    ingest_metadata_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
        state_id="ingest_metadata_reference",
    )
    assert (
        item_definition.states[ingest_metadata_state_id].metadata[
            "subworkflow_contract"
        ]["workflow_id"]
            == PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID
        )
    ingest_file_state_id = authority_service._step_concept_id(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
        state_id="ingest_file_copy_reference",
    )
    assert (
        item_definition.states[ingest_file_state_id].metadata["subworkflow_contract"][
            "workflow_id"
        ]
        == SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    public_title_prepare_state_id = authority_service._step_concept_id(
        workflow_id=PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
        state_id="prepare_title_search",
    )
    public_title_decide_fallback_state_id = authority_service._step_concept_id(
        workflow_id=PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
        state_id="decide_metadata_fallback",
    )
    public_title_reject_fallback_state_id = authority_service._step_concept_id(
        workflow_id=PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
        state_id="reject_insufficient_bibliographic_identity",
    )
    public_title_ingest_fallback_state_id = authority_service._step_concept_id(
        workflow_id=PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
        state_id="ingest_metadata_fallback",
    )
    public_title_prepare_state = public_title_definition.states[
        public_title_prepare_state_id
    ]
    assert {
        mapping.get("context_key")
        for mapping in public_title_prepare_state.metadata.get(
            "tool_output_context_mappings", []
        )
    }.issuperset(
        {
            "public_paper_title_search_required",
            "bibliographic_fallback_sufficient",
            "bibliographic_fallback_basis",
        }
    )
    assert any(
        transition.to_state == public_title_decide_fallback_state_id
        and transition.reason == "stable_bibliographic_identity_already_available"
        for transition in public_title_prepare_state.transitions
    )
    public_title_decide_state = public_title_definition.states[
        public_title_decide_fallback_state_id
    ]
    assert any(
        transition.to_state == public_title_ingest_fallback_state_id
        and transition.reason == "sufficient_bibliographic_identity_available"
        and transition.condition_spec
        == {
            "kind": "context_flag",
            "key": "bibliographic_fallback_sufficient",
            "expected": True,
        }
        for transition in public_title_decide_state.transitions
    )
    assert any(
        transition.to_state == public_title_reject_fallback_state_id
        for transition in public_title_decide_state.transitions
    )
    assert (
        public_title_definition.states[public_title_reject_fallback_state_id]
        .actions[0]
        .action_id
        == "paper_reference.fail_item"
    )
    route_arxiv_targets_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="route_arxiv_targets",
    )
    dispatch_arxiv_targets_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="dispatch_arxiv_target_set",
    )
    normalise_arxiv_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_arxiv_source",
    )
    assert arxiv_definition.initial_state == route_arxiv_targets_state_id
    route_arxiv_targets_state = arxiv_definition.states[route_arxiv_targets_state_id]
    assert (
        route_arxiv_targets_state.actions[0].action_id == "workflow_control.context_set"
    )
    route_transitions = {
        transition.reason: transition
        for transition in route_arxiv_targets_state.transitions
    }
    assert route_transitions["dispatch_multiple_arxiv_targets"].to_state == (
        dispatch_arxiv_targets_state_id
    )
    assert route_transitions["single_arxiv_target"].to_state == normalise_arxiv_state_id
    dispatch_action = arxiv_definition.states[dispatch_arxiv_targets_state_id].actions[
        0
    ]
    assert dispatch_action.action_id == "workflow_control.for_each"
    assert (
        dispatch_action.inputs["workflow_id"] == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert dispatch_action.inputs["items_context_key"] == "arxiv_ids"
    assert dispatch_action.inputs["item_context_key"] == "arxiv_id"
    metadata_terminal_contract = metadata_definition.metadata.get(
        "terminal_success_contract"
    )
    assert isinstance(metadata_terminal_contract, dict)
    assert metadata_terminal_contract.get("success_statuses") == ["completed"]
    metadata_launch_contract = metadata_definition.metadata.get("launch_input_contract")
    assert isinstance(metadata_launch_contract, dict)
    assert metadata_launch_contract.get("schema_version") == (
        "workflow_launch_input_contract.v1"
    )
    assert metadata_launch_contract.get("required_inputs") == []
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "doi"
        and item.get("required") is False
        for item in metadata_launch_contract.get("input_mappings") or []
    )
    metadata_exemplars, metadata_exemplars_source = (
        resolve_workflow_discovery_exemplars(
            SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
        )
    )
    assert metadata_exemplars_source.startswith("text_relation:")
    assert metadata_exemplars is not None
    assert "represent doi article" in metadata_exemplars.get("keywords", [])
    metadata_exemplar_text = json.dumps(metadata_exemplars, sort_keys=True).lower()
    assert "dl.acm.org/doi/full" in metadata_exemplar_text
    assert "paper concept authors doi" in metadata_exemplar_text
    assert "scholarly article source url" in metadata_exemplar_text
    assert "workflow_execute scholarly metadata" in metadata_exemplar_text
    assert metadata_definition.initial_state.endswith("_decide_metadata_extraction")
    decide_metadata_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="decide_metadata_extraction",
    )
    extract_metadata_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="extract_metadata_from_prompt",
    )
    decide_metadata_transitions = {
        transition.reason: transition
        for transition in metadata_definition.states[
            decide_metadata_state_id
        ].transitions
    }
    assert (
        decide_metadata_transitions["prompt_text_available"].to_state
        == extract_metadata_state_id
    )

    extract_metadata_action = metadata_definition.states[
        extract_metadata_state_id
    ].actions[0]
    assert extract_metadata_action.action_id == "llm.action"
    assert extract_metadata_action.execution_mode == "llm"
    assert extract_metadata_action.prompt_contract is not None
    assert extract_metadata_action.prompt_contract["requested_prompt_concept_ids"] == [
        "#V#prompt_scholarly_article_metadata_extraction"
    ]
    extract_mappings = (
        metadata_definition.states[extract_metadata_state_id].metadata.get(
            "tool_output_context_mappings"
        )
        or []
    )
    assert any(
        mapping.get("tool_output_field") == "validated_json.author_names"
        and mapping.get("context_key") == "extracted_author_names"
        for mapping in extract_mappings
    )
    normalise_metadata_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_metadata_context",
    )
    normalise_metadata_action = metadata_definition.states[
        normalise_metadata_state_id
    ].actions[0]
    assignments = normalise_metadata_action.inputs.get("assignments")
    assert isinstance(assignments, list)
    assert any(
        isinstance(item, dict)
        and item.get("key") == "title"
        and "paper_metadata.title" in item.get("value_from_context_options", [])
        and "extracted_title" in item.get("value_from_context_options", [])
        and "source_uri" in item.get("value_from_context_options", [])
        for item in assignments
    )
    resolve_paper_scope_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="resolve_paper_instance_scope",
    )
    assert (
        metadata_definition.states[normalise_metadata_state_id].transitions[0].to_state
        == resolve_paper_scope_state_id
    )
    resolve_paper_scope_action = metadata_definition.states[
        resolve_paper_scope_state_id
    ].actions[0]
    assert resolve_paper_scope_action.action_id == "resolve_publication_scope_profile"
    assert resolve_paper_scope_action.inputs.get("plane") == "instance"
    assert resolve_paper_scope_action.inputs.get("type_concept_ids") == [
        "#V#scholarly_article"
    ]
    assert resolve_paper_scope_action.inputs.get("source_kind") == (
        "public_scholarly_metadata"
    )
    normalise_external_identity_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_external_identity",
    )
    assert any(
        transition.to_state == normalise_external_identity_state_id
        for transition in metadata_definition.states[
            resolve_paper_scope_state_id
        ].transitions
    )
    normalise_external_identity_state = metadata_definition.states[
        normalise_external_identity_state_id
    ]
    assert normalise_external_identity_state.actions[0].action_id == (
        "scholarly_paper.normalise_external_identity"
    )
    external_identity_transitions = {
        transition.reason: transition
        for transition in normalise_external_identity_state.transitions
    }
    create_external_identity_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="create_article_by_external_identity",
    )
    assert (
        external_identity_transitions["canonical_external_identity_available"].to_state
        == create_external_identity_state_id
    )
    attach_metadata_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="attach_metadata",
    )
    assert (
        external_identity_transitions["global_external_identity_resolved"].to_state
        == attach_metadata_state_id
    )
    assert (
        external_identity_transitions["supplied_global_article_validated"].to_state
        == attach_metadata_state_id
    )
    create_external_identity_state = metadata_definition.states[
        create_external_identity_state_id
    ]
    create_external_identity_action = create_external_identity_state.actions[0]
    assert create_external_identity_action.action_id == "create_concepts"
    assert (
        create_external_identity_action.inputs.get("scope_mode", {}).get("$context_key")
        == "paper_instance_scope_mode"
    )
    assert create_external_identity_action.inputs.get("duplicate_resolution_mode") == (
        "canonical_id_only"
    )
    assert create_external_identity_action.inputs["concepts"][0]["concept_id"] == {
        "$context_key": "paper_external_identity_concept_id"
    }
    assert (
        "external_identifiers"
        not in (create_external_identity_action.inputs["concepts"][0])
    )
    assert create_external_identity_state.metadata.get("mutation_authority") == {
        "maximum_level": "additive_vontology",
        "reason_code": "scholarly_article_external_identity_additive_writes",
        "schema_version": "workflow_step_mutation_authority.v1",
    }
    resolve_existing_article_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="resolve_existing_article",
    )
    reject_title_reuse_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="reject_unverified_title_reuse",
    )
    resolve_existing_article_transitions = {
        transition.reason: transition
        for transition in metadata_definition.states[
            resolve_existing_article_state_id
        ].transitions
    }
    assert (
        resolve_existing_article_transitions[
            "title_match_without_stable_identity_requires_review"
        ].to_state
        == reject_title_reuse_state_id
    )
    optional_text_metadata_guards = {
        "attach_metadata": ("public_title_present", "title"),
        "decide_summary_metadata": ("public_summary_present", "summary"),
        "decide_publication_date_metadata": (
            "public_publication_date_present",
            "publication_date",
        ),
    }
    for state_name, (reason, context_key) in optional_text_metadata_guards.items():
        state_id = authority_service._step_concept_id(
            workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
            state_id=state_name,
        )
        transitions = {
            transition.reason: transition
            for transition in metadata_definition.states[state_id].transitions
        }
        condition_spec = transitions[reason].condition_spec
        assert condition_spec == {
            "kind": "all",
            "conditions": [
                {
                    "kind": "context_exists",
                    "key": context_key,
                    "expected": True,
                },
                {
                    "kind": "context_is_null",
                    "key": context_key,
                    "expected": False,
                },
            ],
        }
        assert (
            evaluate_transition_condition_spec(
                context={context_key: None}, condition_spec=condition_spec
            )
            is False
        )
        assert (
            evaluate_transition_condition_spec(
                context={context_key: "public metadata"},
                condition_spec=condition_spec,
            )
            is True
        )
    resolve_topics_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="resolve_topics",
    )
    decide_arxiv_identity_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="decide_arxiv_identity",
    )
    attach_arxiv_identity_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="attach_arxiv_identity",
    )
    assert any(
        transition.to_state == decide_arxiv_identity_state_id
        for transition in metadata_definition.states[
            resolve_topics_state_id
        ].transitions
    )
    decide_arxiv_transitions = {
        transition.reason: transition
        for transition in metadata_definition.states[
            decide_arxiv_identity_state_id
        ].transitions
    }
    assert (
        decide_arxiv_transitions["arxiv_id_present"].to_state
        == attach_arxiv_identity_state_id
    )
    assert decide_arxiv_transitions["arxiv_id_present"].condition_spec == {
        "kind": "all",
        "conditions": [
            {
                "kind": "context_exists",
                "key": "arxiv_id",
                "expected": True,
            },
            {
                "kind": "context_is_null",
                "key": "arxiv_id",
                "expected": False,
            },
        ],
    }
    attach_arxiv_action = metadata_definition.states[
        attach_arxiv_identity_state_id
    ].actions[0]
    assert attach_arxiv_action.action_id == "upsert_text_relation"
    assert attach_arxiv_action.inputs.get("text") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_scholarly_article_metadata_representation_workflow_attach_arxiv_identity_arxiv_id_to_text_parameter",
        "$required": True,
    }
    attach_doi_identity_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="attach_doi_identity",
    )
    attach_source_uri_identity_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="attach_source_uri_identity",
    )
    attach_doi_action = metadata_definition.states[
        attach_doi_identity_state_id
    ].actions[0]
    assert attach_doi_action.action_id == "upsert_text_relation"
    assert attach_doi_action.inputs.get("predicate") == "#V#has_doi"
    attach_source_uri_action = metadata_definition.states[
        attach_source_uri_identity_state_id
    ].actions[0]
    assert attach_source_uri_action.action_id == "upsert_text_relation"
    assert attach_source_uri_action.inputs.get("predicate") == "#V#has_source_uri"
    create_article_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="create_article_concept",
    )
    create_article_state = metadata_definition.states[create_article_state_id]
    create_article_action = create_article_state.actions[0]
    assert create_article_action.action_id == "create_concepts"
    assert create_article_state.metadata.get("mutation_authority") == {
        "maximum_level": "additive_vontology",
        "reason_code": "scholarly_article_metadata_representation_additive_writes",
        "schema_version": "workflow_step_mutation_authority.v1",
    }
    read_back_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="read_back_article",
    )
    read_back_text_relations_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="read_back_article_text_relations",
    )
    summary_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="summarise_representation_evidence",
    )
    completed_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        state_id="completed",
    )
    read_back_transitions = metadata_definition.states[read_back_state_id].transitions
    assert any(
        transition.to_state == read_back_text_relations_state_id
        for transition in read_back_transitions
    )
    read_back_text_relations_action = metadata_definition.states[
        read_back_text_relations_state_id
    ].actions[0]
    assert read_back_text_relations_action.action_id == "get_text_relations_summary"
    assert read_back_text_relations_action.inputs.get("predicates") == [
        "hasName",
        "hasDescription",
        "#V#has_doi",
        "#V#has_source_uri",
        "#V#has_publication_date",
        "#V#has_topic_labels",
    ]
    read_back_text_relations_transitions = {
        transition.reason: transition
        for transition in metadata_definition.states[
            read_back_text_relations_state_id
        ].transitions
    }
    assert (
        read_back_text_relations_transitions["next_step"].to_state == completed_state_id
    )
    summary_transition = read_back_text_relations_transitions[
        "representation_evidence_summary_required"
    ]
    assert summary_transition.to_state == summary_state_id
    assert summary_transition.condition_spec == {
        "expected": True,
        "key": "require_representation_evidence_summary",
        "kind": "context_flag",
    }
    summary_action = metadata_definition.states[summary_state_id].actions[0]
    assert summary_action.action_id == "llm.action"
    assert summary_action.execution_mode == "llm"
    assert summary_action.prompt_contract is not None
    assert summary_action.prompt_contract["requested_prompt_concept_ids"] == [
        "#V#prompt_scholarly_article_representation_evidence_summary"
    ]
    summary_mappings = (
        metadata_definition.states[summary_state_id].metadata.get(
            "tool_output_context_mappings"
        )
        or []
    )
    assert any(
        mapping.get("tool_output_field") == "validated_json.observations"
        and mapping.get("context_key") == "observations"
        for mapping in summary_mappings
    )
    assert any(
        mapping.get("tool_output_field") == "validated_json.response_text"
        and mapping.get("context_key") == "response_text"
        for mapping in summary_mappings
    )

    scholarly_terminal_contract = scholarly_definition.metadata.get(
        "terminal_success_contract"
    )
    assert isinstance(scholarly_terminal_contract, dict)
    assert scholarly_terminal_contract.get("success_statuses") == ["completed"]
    arxiv_terminal_contract = arxiv_definition.metadata.get("terminal_success_contract")
    assert isinstance(arxiv_terminal_contract, dict)
    assert arxiv_terminal_contract.get("success_statuses") == ["completed"]
    explicit_launch_contract = arxiv_definition.metadata.get("launch_contract")
    assert isinstance(explicit_launch_contract, dict)
    assert explicit_launch_contract.get("schema_version") == "launch_contract.v1"
    assert explicit_launch_contract.get("preconditions") == [
        {
            "type": "context_key_present",
            "key": "prompt",
            "required": True,
        }
    ]
    arxiv_launch_contract = arxiv_definition.metadata.get("launch_input_contract")
    assert isinstance(arxiv_launch_contract, dict)
    assert arxiv_launch_contract.get("schema_version") == (
        "workflow_launch_input_contract.v1"
    )
    assert arxiv_launch_contract.get("required_inputs") == ["prompt"]
    assert arxiv_launch_contract.get("excluded_ambient_input_keys") == [
        "arxiv_id",
        "arxiv_ids",
        "source_uri",
        "source_uris",
        "paper_url",
        "paper_urls",
        "url",
        "urls",
        "paper_concept_id",
        "paper_concept_ids",
        "file_copy_concept_id",
        "file_copy_concept_ids",
        "concept_id",
        "concept_ids",
    ]
    resolved_launch_contract, resolved_launch_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert resolved_launch_contract == arxiv_launch_contract
    assert resolved_launch_source.startswith("text_relation:")
    resolved_explicit_launch_contract, resolved_explicit_launch_source = (
        resolve_workflow_launch_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert resolved_explicit_launch_contract == explicit_launch_contract
    assert resolved_explicit_launch_source.startswith("text_relation:")
    input_mappings = arxiv_launch_contract.get("input_mappings")
    assert isinstance(input_mappings, list)
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "prompt"
        and item.get("source_expression") == "inputs.prompt"
        and item.get("required") is True
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression") == "inputs.arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression") == "inputs.prompt"
        and item.get("extractor") == "arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression") == "inputs.augmented_context"
        and item.get("extractor") == "arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_ids"
        and item.get("source_expression") == "inputs.arxiv_id"
        and item.get("extractor") == "arxiv_id_list"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_ids"
        and item.get("source_expression") == "inputs.augmented_context"
        and item.get("extractor") == "arxiv_id_list"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression") == "inputs.conversation_situation"
        and item.get("extractor") == "arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_ids"
        and item.get("source_expression") == "inputs.conversation_situation"
        and item.get("extractor") == "arxiv_id_list"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression")
        == "inputs.turn_expected_outcome_contract.summary"
        and item.get("extractor") == "arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression")
        == "inputs.workflow_discovery_result.discovery_query_input"
        and item.get("extractor") == "arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_ids"
        and item.get("source_expression") == "inputs.prompt"
        and item.get("extractor") == "arxiv_id_list"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_ids"
        and item.get("source_expression")
        == "inputs.workflow_discovery_result.discovery_query_input"
        and item.get("extractor") == "arxiv_id_list"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "source_uri"
        and item.get("source_expression") == "inputs.source_uri"
        and item.get("required") is False
        for item in input_mappings
    )
    launch_input_rows = get_texts_for_concept(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        limit=5,
    )
    explicit_launch_rows = get_texts_for_concept(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#has_launch_contract",
        limit=5,
    )
    assert launch_input_rows
    assert explicit_launch_rows
    routing_profile, routing_source = resolve_workflow_routing_profile(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(routing_profile, dict)
    assert routing_source.startswith("text_relation:")
    assert isinstance(routing_profile.get("role"), str)
    assert isinstance(routing_profile.get("authoring_intent_required"), bool)
    assert isinstance(routing_profile.get("explicit_workflow_context_required"), bool)

    discovery_exemplars, discovery_source = resolve_workflow_discovery_exemplars(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(discovery_exemplars, dict)
    assert discovery_source.startswith("text_relation:")
    assert isinstance(discovery_exemplars.get("keywords"), list)
    assert discovery_exemplars.get("keywords")

    delegate_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="delegate_to_general_paper_workflow",
    )
    delegate_state = arxiv_definition.states[delegate_state_id]
    contract = delegate_state.metadata["subworkflow_contract"]
    assert contract["workflow_id"] == (
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert "file_copy_concept_id" in contract["provided_inputs"]
    assert "paper_concept_id" in contract["provided_inputs"]
    assert "arxiv_id" in contract["provided_inputs"]
    assert "publication_date" in contract["provided_inputs"]
    assert "author_names" in contract["provided_inputs"]
    assert "topic_labels" in contract["provided_inputs"]
    delegate_action = delegate_state.actions[0]
    assert (
        delegate_action.inputs.get("require_representation_evidence_summary") is False
    )
    assert delegate_action.inputs.get("paper_concept_id") == {
        "$context_key": "paper_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_delegate_to_general_paper_workflow_paper_concept_id_to_paper_concept_id_parameter",
        "$required": False,
    }

    fetch_metadata_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="fetch_arxiv_metadata",
    )
    normalise_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_arxiv_source",
    )
    inspect_existing_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="inspect_existing_state",
    )
    normalise_state = arxiv_definition.states[normalise_state_id]
    normalise_action = normalise_state.actions[0]
    assert normalise_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_normalise_arxiv_source_arxiv_id_to_arxiv_id_parameter",
        "$required": False,
    }
    assert normalise_action.inputs.get("source_uri") == {
        "$context_key": "source_uri",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_normalise_arxiv_source_source_uri_to_source_uri_parameter",
        "$required": False,
    }
    assert normalise_action.inputs.get("prompt") == {
        "$context_key": "prompt",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_normalise_arxiv_source_prompt_to_prompt_parameter",
        "$required": False,
    }
    normalise_transitions = {
        transition.reason: transition.to_state
        for transition in normalise_state.transitions
    }
    assert normalise_transitions["next_step"] == inspect_existing_state_id
    inspect_existing_state = arxiv_definition.states[inspect_existing_state_id]
    inspect_existing_action = inspect_existing_state.actions[0]
    assert inspect_existing_action.action_id == "arxiv.inspect_existing_state"
    assert inspect_existing_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_inspect_existing_state_arxiv_id_to_arxiv_id_parameter",
        "$required": True,
    }
    inspect_existing_transitions = {
        transition.reason: transition.to_state
        for transition in inspect_existing_state.transitions
    }
    assert inspect_existing_transitions["next_step"] == fetch_metadata_state_id
    inspect_output_mappings = inspect_existing_state.metadata[
        "tool_output_context_mappings"
    ]
    assert {
        (mapping["tool_output_field"], mapping["context_key"])
        for mapping in inspect_output_mappings
    } == {
        ("arxiv_id", "arxiv_id"),
        ("paper_concept_id", "paper_concept_id"),
        ("file_copy_concept_id", "file_copy_concept_id"),
        ("inspected_existing_state", "inspected_existing_state"),
    }
    fetch_metadata_action = arxiv_definition.states[fetch_metadata_state_id].actions[0]
    assert fetch_metadata_action.action_id == "get_paper_metadata"
    assert fetch_metadata_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_fetch_arxiv_metadata_arxiv_id_to_arxiv_id_parameter",
        "$required": True,
    }
    download_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="download_or_finalise",
    )
    download_action = arxiv_definition.states[download_state_id].actions[0]
    assert "materialise_scholarly_representation" not in download_action.inputs
    build_pdf_import_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="build_arxiv_pdf_import_request",
    )
    import_pdf_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="import_arxiv_pdf_from_url",
    )
    download_state = arxiv_definition.states[download_state_id]
    assert download_state.metadata["retry_policy"]["max_attempts"] == 2
    assert all(
        mapping["context_key"] != "arxiv_mcp_failure_details"
        for mapping in download_state.metadata["tool_output_context_mappings"]
    )
    download_transitions = {
        transition.reason: transition for transition in download_state.transitions
    }
    assert download_transitions["download_or_finalise_succeeded"].to_state == (
        delegate_state_id
    )
    recovery_transition = download_transitions["on_failure"]
    assert recovery_transition.to_state == build_pdf_import_state_id
    assert recovery_transition.condition_spec == {
        "conditions": [
            {
                "expected": True,
                "key": "last_action_failed",
                "kind": "context_flag",
            },
            {
                "key": (
                    "last_action_outputs.result.error_details."
                    "recommended_recovery_action"
                ),
                "kind": "context_value_in",
                "values": ["download_from_source", "reacquire_pdf_from_source"],
            },
        ],
        "kind": "all",
    }
    assert (
        "finalise_cached_pdf"
        not in recovery_transition.condition_spec["conditions"][1]["values"]
    )
    build_pdf_import_action = arxiv_definition.states[
        build_pdf_import_state_id
    ].actions[0]
    assert build_pdf_import_action.action_id == "workflow_control.context_template"
    assert build_pdf_import_action.inputs["assignments"][0]["key"] == "arxiv_pdf_url"
    import_reason_variable = build_pdf_import_action.inputs["assignments"][2][
        "variables"
    ]["failure_kind"]
    assert import_reason_variable["value_from_context_options"] == [
        "last_action_outputs.result.error_code",
        "last_action_outputs.result.error_details.external_provider_failure_kind",
    ]
    import_pdf_action = arxiv_definition.states[import_pdf_state_id].actions[0]
    assert import_pdf_action.action_id == "import_url_file_copy"
    assert import_pdf_action.inputs["url"] == {
        "$context_key": "arxiv_pdf_url",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_import_arxiv_pdf_from_url_arxiv_pdf_url_to_url_parameter",
        "$required": True,
    }
    assert import_pdf_action.inputs["type_concept_id"] == "#V#arxiv_pdf_file"
    assert import_pdf_action.inputs["timeout_seconds"] == 30
    import_pdf_transitions = {
        transition.reason: transition
        for transition in arxiv_definition.states[import_pdf_state_id].transitions
    }
    assert import_pdf_transitions["on_failure"].to_state == (
        authority_service._step_concept_id(
            workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
            state_id="record_pdf_import_skipped",
        )
    )
    import_pdf_mappings = arxiv_definition.states[import_pdf_state_id].metadata[
        "tool_output_context_mappings"
    ]
    assert any(
        mapping["tool_output_field"] == "result.concept_id"
        and mapping["context_key"] == "file_copy_concept_id"
        for mapping in import_pdf_mappings
    )
    record_pdf_import_skipped_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="record_pdf_import_skipped",
    )
    record_pdf_import_skipped_action = arxiv_definition.states[
        record_pdf_import_skipped_state_id
    ].actions[0]
    assert record_pdf_import_skipped_action.action_id == "workflow_control.context_set"
    assert record_pdf_import_skipped_action.inputs.get("assignments")[:3] == [
        {"key": "pdf_acquisition_status", "value": "unavailable"},
        {"key": "pdf_acquisition_optional", "value": True},
        {
            "key": "pdf_acquisition_failure_stage",
            "value": "import_arxiv_pdf_from_url",
        },
    ]
    optional_pdf_diagnostics = record_pdf_import_skipped_action.inputs.get(
        "assignments"
    )[3:]
    assert {assignment["key"] for assignment in optional_pdf_diagnostics} == {
        "arxiv_pdf_import_error",
        "arxiv_pdf_import_message",
        "arxiv_pdf_import_status_code",
        "arxiv_pdf_import_final_url",
    }
    assert all(
        "skip_if_unresolved" not in assignment
        for assignment in optional_pdf_diagnostics
    )
    decide_acquisition_mode_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="decide_acquisition_mode",
    )
    finalise_cached_pdf_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="finalise_cached_pdf",
    )
    recover_partial_cache_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="recover_partial_cache_state",
    )
    delegate_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="delegate_to_general_paper_workflow",
    )
    delegate_action = arxiv_definition.states[delegate_state_id].actions[0]
    assert delegate_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_delegate_to_general_paper_workflow_file_copy_concept_id_to_file_copy_concept_id_parameter",
        "$required": False,
    }
    assert delegate_action.inputs.get("require_file_copy") is False
    decide_acquisition_mode_action = arxiv_definition.states[
        decide_acquisition_mode_state_id
    ].actions[0]
    assert decide_acquisition_mode_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_decide_acquisition_mode_file_copy_concept_id_to_file_copy_concept_id_parameter",
        "$required": False,
    }
    decide_transitions = {
        transition.reason: transition
        for transition in arxiv_definition.states[
            decide_acquisition_mode_state_id
        ].transitions
    }
    assert (
        decide_transitions["existing_file_copy_available"].to_state == delegate_state_id
    )
    assert decide_transitions["existing_file_copy_available"].condition_spec == {
        "key": "acquisition_mode",
        "kind": "context_value_equals",
        "value": "existing_file_copy",
    }
    assert (
        decide_transitions["finalise_cached_pdf"].to_state
        == finalise_cached_pdf_state_id
    )
    assert decide_transitions["recover_partial_cache"].to_state == (
        recover_partial_cache_state_id
    )
    assert decide_transitions["download_from_source"].to_state == download_state_id

    finalise_action = arxiv_definition.states[finalise_cached_pdf_state_id].actions[0]
    assert finalise_action.action_id == "finalise_cached_paper"
    assert finalise_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_finalise_cached_pdf_arxiv_id_to_arxiv_id_parameter",
        "$required": True,
    }

    recover_action = arxiv_definition.states[recover_partial_cache_state_id].actions[0]
    assert recover_action.action_id == "workflow_control.context_set"
    assert recover_action.inputs.get("assignments") == [
        {"key": "cache_recovery_required", "value": True},
        {"key": "cache_recovery_action", "value": "reacquire_partial_cache"},
        {"key": "cache_recovery_reason", "value_from_context": "cache_state"},
        {
            "key": "cache_recovery_markdown_path",
            "value_from_context": "cached_markdown_path",
        },
    ]
    verify_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="verify_arxiv_path",
    )
    verify_action = arxiv_definition.states[verify_state_id].actions[0]
    assert verify_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_verify_arxiv_path_file_copy_concept_id_to_file_copy_concept_id_parameter",
        "$required": False,
    }
    assert verify_action.inputs.get("require_file_copy") is False

    scholarly_delegate_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="delegate_to_metadata_workflow",
    )
    scholarly_delegate_state = scholarly_definition.states[scholarly_delegate_state_id]
    scholarly_delegate_contract = scholarly_delegate_state.metadata[
        "subworkflow_contract"
    ]
    assert scholarly_delegate_contract["workflow_id"] == (
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert "file_copy_concept_id" in scholarly_delegate_contract["provided_inputs"]
    scholarly_verify_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="verify_representation",
    )
    scholarly_verify_action = scholarly_definition.states[
        scholarly_verify_state_id
    ].actions[0]
    assert scholarly_verify_action.inputs.get("publication_date") == {
        "$context_key": "publication_date",
        "$mapping_concept_id": "#V#workflow_mapping_scholarly_paper_representation_workflow_verify_representation_publication_date_to_publication_date_parameter",
        "$required": True,
    }
    scholarly_normalise_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_inputs",
    )
    scholarly_normalise_action = scholarly_definition.states[
        scholarly_normalise_state_id
    ].actions[0]
    assert scholarly_normalise_action.inputs.get("paper_concept_id") == {
        "$context_key": "paper_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_scholarly_paper_representation_workflow_normalise_inputs_paper_concept_id_to_paper_concept_id_parameter",
        "$required": False,
    }

    scholarly_concept = concept_service.get_concept_by_concept_id(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_concept is not None
    scholarly_types = (scholarly_concept.get("relationships") or {}).get(
        "is_an_instance_of"
    ) or []
    assert "#V#ai_workflow" in scholarly_types
    assert "#V#durable_workflow" in scholarly_types

    step_concept = concept_service.get_concept_by_concept_id(delegate_state_id)
    assert step_concept is not None
    step_types = (step_concept.get("relationships") or {}).get(
        "is_an_instance_of"
    ) or []
    assert "#V#workflow_step" in step_types


def _build_stub_representation_definition(
    *,
    workflow_id: str,
    action_id: str,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="represent",
        states={
            "represent": WorkflowStateSpec(
                state_id="represent",
                actions=(WorkflowActionInvocation(action_id=action_id),),
                terminal=True,
            )
        },
    )


def _build_source_neutral_execution_registry(
    *,
    parent_definition: WorkflowDefinition,
    item_definition: WorkflowDefinition,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ActionRegistry, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    child_definitions = {
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID: parent_definition,
        SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID: item_definition,
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID: _build_stub_representation_definition(
            workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
            action_id="stub.represent_arxiv_paper",
        ),
        PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID: (
            _build_stub_representation_definition(
                workflow_id=PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID,
                action_id="stub.represent_under_preparation_paper",
            )
        ),
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID: (
            _build_stub_representation_definition(
                workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
                action_id="stub.represent_metadata_paper",
            )
        ),
        PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID: (
            _build_stub_representation_definition(
                workflow_id=PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
                action_id="stub.represent_metadata_paper",
            )
        ),
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID: _build_stub_representation_definition(
            workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
            action_id="stub.represent_file_copy_paper",
        ),
    }

    def _loader(workflow_id: str) -> WorkflowDefinition | None:
        return child_definitions.get(workflow_id)

    def _actor_scoped_resolver(
        workflow_id: str,
        **_kwargs: Any,
    ) -> registry_factory.WorkflowDefinitionAuthorityResolution:
        definition = child_definitions.get(workflow_id)
        return registry_factory.WorkflowDefinitionAuthorityResolution(
            workflow_id=workflow_id,
            registry=None,
            registration=None,
            definition=definition,
            registration_source="vontology" if definition is not None else "unknown",
            known_workflow_ids=(),
            error_code=None
            if definition is not None
            else "workflow_concept_not_accessible",
            diagnostics={
                "actor_scoped_authority_required": True,
                "shared_registry_definition_trusted": False,
            },
        )

    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _actor_scoped_resolver,
    )

    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=_loader)
    register_subworkflow_actions(registry, definition_loader=_loader)
    register_paper_representation_actions(registry)

    def _stub_handler(label: str):
        def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
            calls.append(
                {
                    "label": label,
                    "inputs": dict(request.inputs),
                    "data": dict(request.data),
                }
            )
            paper_concept_id = f"#V#stub_{label}_paper_{len(calls)}"
            return WorkflowActionResult(
                status="success",
                outputs={
                    "paper_concept_id": paper_concept_id,
                    "file_copy_concept_id": request.inputs.get("file_copy_concept_id")
                    or f"#V#stub_{label}_file_{len(calls)}",
                    "article_readback": {
                        "concept_id": paper_concept_id,
                        "label": label,
                    },
                },
            )

        return _handle

    registry.register(
        ActionSpec(
            action_id="stub.represent_arxiv_paper",
            handler=_stub_handler("arxiv"),
        )
    )
    registry.register(
        ActionSpec(
            action_id="stub.represent_metadata_paper",
            handler=_stub_handler("metadata"),
        )
    )
    registry.register(
        ActionSpec(
            action_id="stub.represent_under_preparation_paper",
            handler=_stub_handler("under_preparation"),
        )
    )
    registry.register(
        ActionSpec(
            action_id="stub.represent_file_copy_paper",
            handler=_stub_handler("file_copy"),
        )
    )

    return registry, calls


def _load_source_neutral_test_definitions() -> tuple[
    WorkflowDefinition, WorkflowDefinition
]:
    parent_definition = load_workflow_definition_from_vontology(
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID
    )
    item_definition = load_workflow_definition_from_vontology(
        SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID
    )
    assert parent_definition is not None
    assert item_definition is not None
    return parent_definition, item_definition


def test_source_neutral_paper_reference_workflow_fans_out_mixed_references_with_partial_success(
    _canonical_seeded_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_definition, item_definition = _load_source_neutral_test_definitions()
    registry, calls = _build_source_neutral_execution_registry(
        parent_definition=parent_definition,
        item_definition=item_definition,
        monkeypatch=monkeypatch,
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent_definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_reference_set": {
                "schema_version": "paper_reference_set.v1",
                "items": [
                    {
                        "reference_kind": "arxiv",
                        "arxiv_id": "2603.24621",
                        "source_uri": "https://arxiv.org/abs/2603.24621",
                    },
                    {
                        "reference_kind": "doi",
                        "doi": "10.1145/3743093.3770985",
                        "source_uri": "https://doi.org/10.1145/3743093.3770985",
                    },
                    {
                        "reference_kind": "file_copy",
                        "file_copy_concept_id": "#V#uploaded_scholarly_pdf",
                    },
                    {
                        "reference_kind": "metadata",
                        "paper_metadata": {
                            "title": "A Source Neutral Paper",
                            "authors": ["Ada Lovelace"],
                        },
                    },
                    {
                        "reference_kind": "unsupported_reference",
                        "source_uri": "urn:example:not-a-paper",
                    },
                ],
                "source_context": {
                    "source_kind": "direct_prompt",
                    "provenance_required": True,
                },
            }
        },
    )

    assert result.completed is True, json.dumps(
        {
            key: value
            for key, value in result.data.items()
            if key.startswith("last_") or "scope" in key or "error" in key
        },
        sort_keys=True,
        default=str,
    )
    assert result.final_state.endswith("_completed")
    assert result.data["paper_reference_item_count"] == 5
    assert result.data["for_each_success_count"] == 4
    assert result.data["for_each_error_count"] == 1
    assert result.data["for_each_partial_success"] is True
    assert [call["label"] for call in calls] == [
        "arxiv",
        "metadata",
        "file_copy",
        "metadata",
    ]
    assert calls[0]["data"]["arxiv_id"] == "2603.24621"
    assert calls[1]["data"]["doi"] == "10.1145/3743093.3770985"
    assert calls[2]["data"]["file_copy_concept_id"] == "#V#uploaded_scholarly_pdf"
    assert calls[3]["data"]["paper_metadata"]["title"] == "A Source Neutral Paper"
    errors = [
        item
        for item in result.data["iteration_results"]
        if item.get("completed") is False
    ]
    assert len(errors) == 1
    assert errors[0]["error"] == "paper_reference_kind_unsupported"


def test_source_neutral_paper_reference_workflow_fails_when_no_item_succeeds(
    _canonical_seeded_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_definition, item_definition = _load_source_neutral_test_definitions()
    registry, calls = _build_source_neutral_execution_registry(
        parent_definition=parent_definition,
        item_definition=item_definition,
        monkeypatch=monkeypatch,
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent_definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_references": [
                {
                    "reference_kind": "unsupported_reference",
                    "source_uri": "urn:example:not-a-paper",
                }
            ]
        },
    )

    assert result.completed is False
    assert result.final_state.endswith("_failed")
    assert result.error == "paper_reference_ingestion_no_items_succeeded"
    assert result.result_envelope is not None
    assert result.result_envelope["diagnostics"]["error"] == (
        "paper_reference_ingestion_no_items_succeeded"
    )
    assert result.data["paper_reference_item_count"] == 1
    assert result.data["for_each_success_count"] == 0
    assert result.data["for_each_error_count"] == 1
    assert result.data["iteration_results"][0]["error"] == (
        "paper_reference_kind_unsupported"
    )
    assert calls == []


def test_source_neutral_workflow_routes_explicit_submission_to_org_workflow_but_public_id_wins(
    _canonical_seeded_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_definition, item_definition = _load_source_neutral_test_definitions()
    registry, calls = _build_source_neutral_execution_registry(
        parent_definition=parent_definition,
        item_definition=item_definition,
        monkeypatch=monkeypatch,
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent_definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_references": [
                {
                    "reference_kind": "metadata",
                    "title": "A Private Conference Submission",
                    "paper_type_concept_id": "#V#paper_under_preparation",
                    "paper_lifecycle_state": "under_review",
                    "submission_identifier": "17045",
                    "submission_venue": "NeurIPS 2026",
                },
                {
                    "reference_kind": "arxiv",
                    "arxiv_id": "2608.12345",
                    "title": "A Public Preprint",
                    "paper_type_concept_id": "#V#paper_under_preparation",
                    "paper_lifecycle_state": "submitted",
                },
            ]
        },
    )

    assert result.completed is True
    assert [call["label"] for call in calls] == ["under_preparation", "arxiv"]
    assert calls[0]["data"]["submission_identifier"] == "17045"
    assert calls[0]["data"]["paper_lifecycle_state"] == "under_review"
    assert calls[1]["data"]["arxiv_id"] == "2608.12345"


def test_source_neutral_paper_reference_workflow_preview_mode_does_not_delegate(
    _canonical_seeded_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_definition, item_definition = _load_source_neutral_test_definitions()
    registry, calls = _build_source_neutral_execution_registry(
        parent_definition=parent_definition,
        item_definition=item_definition,
        monkeypatch=monkeypatch,
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent_definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "preview_only": True,
            "paper_references": [
                {"reference_kind": "arxiv", "arxiv_id": "2603.24621"},
                {
                    "reference_kind": "doi",
                    "doi": "10.1145/3743093.3770985",
                },
            ],
        },
    )

    assert result.completed is True
    assert result.data["for_each_success_count"] == 2
    assert result.data["for_each_error_count"] == 0
    assert calls == []
    statuses = [
        item.get("result", {}).get("paper_reference_item_status")
        for item in result.data["iteration_results"]
    ]
    assert statuses == ["preview", "preview"]


def test_source_neutral_paper_reference_workflow_fails_closed_without_references(
    _canonical_seeded_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_definition, item_definition = _load_source_neutral_test_definitions()
    registry, _calls = _build_source_neutral_execution_registry(
        parent_definition=parent_definition,
        item_definition=item_definition,
        monkeypatch=monkeypatch,
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent_definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={},
    )

    assert result.completed is False
    assert result.error == "paper_reference_set_missing"
    assert result.data["last_action_outputs"]["paper_reference_error_code"] == (
        "paper_reference_set_missing"
    )


def test_source_neutral_paper_reference_launch_contract_and_exemplars_are_source_neutral(
    _canonical_seeded_mock_db: Any,
) -> None:
    launch_contract, launch_contract_source = resolve_workflow_launch_input_contract(
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID
    )
    assert launch_contract is not None
    assert launch_contract_source.startswith("text_relation:")
    resolution = resolve_workflow_launch_inputs(
        workflow_id=SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        contract=launch_contract,
        inputs={
            "prompt": (
                "请处理 https://arxiv.org/abs/2603.24621 and "
                "https://doi.org/10.1145/3743093.3770985"
            ),
            "source_context": {"source_kind": "direct_prompt"},
        },
    )
    assert resolution.unresolved_required_inputs == ()
    assert resolution.resolved_inputs["arxiv_ids"] == ["2603.24621"]
    assert "prompt" in resolution.resolved_inputs
    assert resolution.resolved_inputs["source_context"] == {
        "source_kind": "direct_prompt"
    }
    exemplars, source = resolve_workflow_discovery_exemplars(
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID
    )
    assert source.startswith("text_relation:")
    assert exemplars is not None
    exemplar_text = json.dumps(exemplars, ensure_ascii=False)
    assert "请把这些 arXiv 和 DOI 论文加入知识库" in exemplar_text
    assert "Tafadhali ingiza" in exemplar_text
    assert "Tēnā whakaurua" in exemplar_text
    assert "non-mail paper ingestion" in exemplars.get("keywords", [])

    routing_profile, routing_profile_source = resolve_workflow_routing_profile(
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID
    )
    assert routing_profile_source.startswith("text_relation:")
    assert routing_profile is not None
    assert routing_profile.get("prefer_existing_capability") is True
    routing_profile_rows = get_texts_for_concept(
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        limit=1,
    )
    assert routing_profile_rows
    routing_profile_text = routing_profile_rows[0].get("text") or ""
    assert "any user language" in routing_profile_text
    assert "Do not select it as the top-level route" in routing_profile_text
    assert "email-source convergence parent" in routing_profile_text


def test_direct_paper_workflows_defer_mail_discovery_to_email_parent() -> None:
    seed_payload = json.loads(
        _PAPER_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8")
    )
    workflows = {
        item["workflow_id"]: item
        for item in seed_payload.get("workflows") or []
        if isinstance(item, dict)
    }

    arxiv_workflow = workflows[ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID]
    source_neutral_workflow = workflows[
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID
    ]

    assert "supplied independently of mail discovery" in arxiv_workflow["description"]
    assert "choose the email-convergence parent" in arxiv_workflow["description"]
    assert "choose the email-convergence parent" in source_neutral_workflow[
        "description"
    ]


def test_bootstrap_skips_republication_when_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_paper_representation_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 8

    second_report = bootstrap_canonical_paper_representation_workflows()
    second_authority = second_report.get("authority_contract") or {}
    second_preflight = second_report.get("materialisation_preflight") or {}
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_authority == {
        "canonical_source": "vontology",
        "repo_seed_role": "startup_seed_publication_and_repair_only",
        "request_path_dependency_allowed": False,
    }
    assert second_preflight.get("already_current") is True
    assert second_preflight.get("drift_detected") is False
    assert second_preflight.get("drift_workflow_ids") == []
    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_publication.get("materialisation_status") == "current"
    assert second_publication.get("drift_detected") is False
    assert second_publication.get("issue_codes") == []
    assert second_preflight.get("bundle_snapshot_drift_detected") is False
    assert second_preflight.get("bundle_snapshot_drift_workflow_ids") == []
    assert second_preflight.get("bundle_snapshot_issue_codes") == []
    assert second_publication.get("bundle_snapshot_drift_detected") is False
    assert second_publication.get("bundle_snapshot_drift_workflow_ids") == []
    assert second_publication.get("bundle_snapshot_issue_codes") == []
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
    assert second_report.get("typed_workflow_ids") == []
    assert second_report.get("typed_step_ids") == []


def test_bootstrap_migrates_reviewed_v35_outcome_prompt_map_gap(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    _delete_workflow_text_relations(
        workflow_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowOutcomeExplanationPromptMapJson",
    )
    upsert_singleton_text_relation(
        subject_concept_id=SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        text=json.dumps(
            {
                "schema_version": "workflow_repo_seed_version.v1",
                "seed_version": "35",
                "family_id": "paper_representation_workflow_seed_bundle",
                "source_tag": "JVNAUTOSCI-2244",
                "authority_payload_sha256": (
                    "ee4466476050ae4d69bad80fdedd658e299d5734aaa28ccf937be0426a7a3c0a"
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v35_outcome_prompt_map_gap"},
        garbage_collect=True,
    )
    invalidate_workflow_discovery_executability_caches()

    repair_report = bootstrap_canonical_paper_representation_workflows()
    publication = repair_report.get("publication") or {}
    adjudication = (
        (repair_report.get("repo_seed_version_gate") or {}).get(
            "authority_adjudication_by_workflow"
        )
        or {}
    ).get(SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID) or {}

    assert repair_report.get("success") is True
    assert publication.get("materialisation_status") == "repo_seed_version_refresh"
    assert (publication.get("counts") or {}).get("errors") == 0
    assert adjudication.get("observed_authority_payload_sha256") == (
        "103f9fe684d88329878358645945b21eaf502676067ae39d233be432d1e743ed"
    )
    assert adjudication.get("reason") == "exact_reviewed_legacy_migration"
    assert adjudication.get("publication_authorised") is True

    prompt_map_rows = get_texts_for_concept(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowOutcomeExplanationPromptMapJson",
        limit=2,
    )
    assert prompt_map_rows
    marker_rows = get_texts_for_concept(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        limit=2,
    )
    assert any(
        json.loads(row.get("text") or "{}").get("seed_version") == "36"
        for row in marker_rows
        if isinstance(row.get("text"), str)
    )


def test_bootstrap_seed_version_refresh_repairs_old_arxiv_launch_contract(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    old_launch_contract = {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": ["prompt"],
        "input_mappings": [
            {
                "target_context_key": "prompt",
                "source_expression": "inputs.prompt",
                "extractor": "identity",
                "required": True,
            },
            {
                "target_context_key": "arxiv_id",
                "source_expression": "inputs.arxiv_id",
                "extractor": "identity",
                "required": False,
            },
        ],
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        payload=old_launch_contract,
    )
    invalidate_workflow_discovery_executability_caches()
    reviewed_legacy_report = bootstrap_canonical_paper_representation_workflows()
    reviewed_legacy_gate = reviewed_legacy_report.get("repo_seed_version_gate") or {}
    reviewed_legacy_adjudication = (
        reviewed_legacy_gate.get("authority_adjudication_by_workflow") or {}
    ).get(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID) or {}
    reviewed_legacy_sha256 = str(
        reviewed_legacy_adjudication.get("observed_authority_payload_sha256") or ""
    ).strip()
    assert reviewed_legacy_sha256
    assert reviewed_legacy_adjudication.get("reason") == (
        "live_authority_requires_explicit_migration"
    )
    upsert_singleton_text_relation(
        subject_concept_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        text=json.dumps(
            {
                "schema_version": "workflow_repo_seed_version.v1",
                "seed_version": "8",
                "family_id": "paper_representation_workflow_seed_bundle",
                "source_tag": "JVNAUTOSCI-2192",
                "authority_payload_sha256": reviewed_legacy_sha256,
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_seed_version_refresh"},
        garbage_collect=True,
    )
    invalidate_workflow_discovery_executability_caches()

    repair_report = bootstrap_canonical_paper_representation_workflows()
    preflight = repair_report.get("materialisation_preflight") or {}
    publication = repair_report.get("publication") or {}
    version_gate = publication.get("repo_seed_version_gate") or {}

    assert publication.get("materialisation_status") == "repo_seed_version_refresh"
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        publication.get("repo_seed_version_refresh_workflow_ids") or []
    )
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        version_gate.get("refresh_workflow_ids") or []
    )
    status = (preflight.get("repo_seed_version_gate") or {}).get(
        "status_by_workflow_id", {}
    ).get(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID) or {}
    assert status.get("reason") == "repo_seed_version_newer"

    refreshed_launch_contract, refreshed_launch_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert (
        refreshed_launch_source == "text_relation:#V#hasWorkflowLaunchInputContractJson"
    )
    assert isinstance(refreshed_launch_contract, dict)
    arxiv_sources = {
        mapping.get("source_expression")
        for mapping in refreshed_launch_contract.get("input_mappings") or []
        if mapping.get("target_context_key") == "arxiv_id"
    }
    assert {
        "inputs.arxiv_id",
        "inputs.augmented_context",
        "inputs.conversation_situation",
        "inputs.turn_expected_outcome_contract.summary",
        "inputs.turn_expected_outcome_contract_state.fields.summary",
        "inputs.workflow_discovery_result.discovery_query_input",
    }.issubset(arxiv_sources)
    arxiv_list_sources = {
        mapping.get("source_expression")
        for mapping in refreshed_launch_contract.get("input_mappings") or []
        if mapping.get("target_context_key") == "arxiv_ids"
    }
    assert {
        "inputs.arxiv_ids",
        "inputs.arxiv_id",
        "inputs.prompt",
        "inputs.augmented_context",
        "inputs.conversation_situation",
        "inputs.turn_expected_outcome_contract.summary",
        "inputs.turn_expected_outcome_contract_state.fields.summary",
        "inputs.workflow_discovery_result.discovery_query_input",
    }.issubset(arxiv_list_sources)

    marker_rows = get_texts_for_concept(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        limit=5,
    )
    marker_payloads = [
        json.loads(row.get("text") or "{}")
        for row in marker_rows
        if isinstance(row.get("text"), str)
    ]
    assert any(payload.get("seed_version") == "36" for payload in marker_payloads)

    refreshed_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert refreshed_definition is not None
    required_effects_contract = refreshed_definition.metadata.get(
        "required_effects_contract"
    )
    assert isinstance(required_effects_contract, dict)
    assert required_effects_contract.get("contract_id") == (
        "arxiv_paper_representation_readback"
    )
    required_effect = required_effects_contract["required_effects"][0]
    assert required_effect["required_tools"] == [
        "scholarly_paper.verify_representation"
    ]
    assert required_effect["required_payload_fields"] == ["paper_concept_id"]
    assert required_effect["targets_extractor"] == "arxiv_id_list"
    assert (
        "workflow_discovery_result.discovery_query_input"
        in (required_effect["targets_source_expressions"])
    )


def test_arxiv_launch_contract_explicit_singular_target_is_not_widened_by_ambient_context(
    _canonical_seeded_mock_db: Any,
) -> None:
    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert launch_source == "text_relation:#V#hasWorkflowLaunchInputContractJson"
    assert isinstance(launch_contract, dict)

    resolution = resolve_workflow_launch_inputs(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        contract=launch_contract,
        contract_source=launch_source,
        inputs={
            "prompt": "Represent the paper I selected and keep its evidence.",
            "arxiv_id": "2605.26340",
            "augmented_context": [
                {
                    "role": "assistant",
                    "content": (
                        "Earlier evidence also mentioned arXiv:2607.27844, "
                        "arXiv:2607.28607, and arXiv:2607.15776."
                    ),
                }
            ],
        },
    )

    assert resolution.unresolved_required_inputs == ()
    assert resolution.resolved_inputs["arxiv_id"] == "2605.26340"
    assert resolution.resolved_inputs["arxiv_ids"] == ["2605.26340"]
    mappings = resolution.diagnostics.get("mappings") or []
    assert any(
        mapping.get("target_context_key") == "arxiv_ids"
        and mapping.get("source_expression") == "inputs.arxiv_id"
        and mapping.get("resolution_reason") == "resolved"
        for mapping in mappings
    )
    assert any(
        mapping.get("target_context_key") == "arxiv_ids"
        and mapping.get("source_expression") == "inputs.augmented_context"
        and mapping.get("resolution_reason") == "target_already_resolved"
        for mapping in mappings
    )


def test_arxiv_launch_contract_explicit_batch_target_remains_authoritative(
    _canonical_seeded_mock_db: Any,
) -> None:
    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert launch_source == "text_relation:#V#hasWorkflowLaunchInputContractJson"
    assert isinstance(launch_contract, dict)

    explicit_batch = ["2605.26340", "2607.27844"]
    resolution = resolve_workflow_launch_inputs(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        contract=launch_contract,
        contract_source=launch_source,
        inputs={
            "prompt": "Represent these two selected papers.",
            "arxiv_id": "2605.26340",
            "arxiv_ids": explicit_batch,
            "augmented_context": [
                {
                    "role": "assistant",
                    "content": (
                        "Unrelated earlier evidence mentioned arXiv:2607.28607 "
                        "and arXiv:2607.15776."
                    ),
                }
            ],
        },
    )

    assert resolution.unresolved_required_inputs == ()
    assert resolution.resolved_inputs["arxiv_id"] == "2605.26340"
    assert resolution.resolved_inputs["arxiv_ids"] == explicit_batch
    mappings = resolution.diagnostics.get("mappings") or []
    assert any(
        mapping.get("target_context_key") == "arxiv_ids"
        and mapping.get("source_expression") == "inputs.arxiv_ids"
        and mapping.get("resolution_reason") == "resolved"
        for mapping in mappings
    )
    assert any(
        mapping.get("target_context_key") == "arxiv_ids"
        and mapping.get("source_expression") == "inputs.arxiv_id"
        and mapping.get("resolution_reason") == "target_already_resolved"
        for mapping in mappings
    )


def test_arxiv_launch_contract_resolves_deictic_paper_ids_from_prior_context(
    _canonical_seeded_mock_db: Any,
) -> None:
    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert launch_source == "text_relation:#V#hasWorkflowLaunchInputContractJson"
    assert isinstance(launch_contract, dict)

    resolution = resolve_workflow_launch_inputs(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        contract=launch_contract,
        contract_source=launch_source,
        inputs={
            "prompt": "Now represent all three papers",
            "augmented_context": [
                {
                    "role": "user",
                    "content": (
                        "List the three emails you found in a previous turn "
                        "wiht arXiv papers"
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "[2605.03042] ARIS: Autonomous Research via "
                        "Adversarial Multi-Agent Collaboration\n"
                        "[2202.04044] Aging and the Narrowing of Scientific "
                        "Innovation\n"
                        "[2605.00347] Odysseus: Scaling VLMs to 100+ Turn "
                        "Decision-Making in Games via Reinforcement Learning"
                    ),
                },
            ],
            "turn_expected_outcome_contract_state": {
                "fields": {
                    "summary": (
                        "Represent the three arXiv papers as durable Vontology "
                        "scholarly-article artefacts, with verified concept IDs "
                        "and read-back."
                    )
                }
            },
            "workflow_discovery_result": {
                "discovery_query_input": (
                    "Success target: represent the three arXiv papers as "
                    "durable Vontology scholarly-article concepts."
                )
            },
        },
    )

    assert resolution.unresolved_required_inputs == ()
    assert resolution.resolved_inputs["prompt"] == "Now represent all three papers"
    assert resolution.resolved_inputs["arxiv_id"] == "2605.03042"
    assert resolution.resolved_inputs["arxiv_ids"] == [
        "2605.03042",
        "2202.04044",
        "2605.00347",
    ]
    assert resolution.diagnostics["status"] == "resolved"
    assert any(
        mapping.get("target_context_key") == "arxiv_ids"
        and mapping.get("source_expression") == "inputs.augmented_context"
        and mapping.get("resolution_reason") == "resolved"
        for mapping in resolution.diagnostics.get("mappings") or []
    )


@pytest.mark.parametrize("import_succeeds", [True, False])
@pytest.mark.parametrize(
    ("download_error_code", "download_error_details", "expected_import_reason"),
    [
        (
            "external_arxiv_mcp_download_failed",
            {
                "provider": "external_third_party",
                "external_provider": "arxiv-mcp-server",
                "external_provider_operation": "download_paper",
                "external_provider_failure_kind": "provider_error",
                "external_provider_retryable": True,
                "recovery_hint": "import_pdf_url",
                "recommended_recovery_action": "download_from_source",
            },
            "external_arxiv_mcp_download_failed",
        ),
        (
            "arxiv_acquisition_unavailable",
            {
                "acquisition_stage": "proxy_initialisation",
                "provider_invoked": False,
                "exception_type": "RuntimeError",
                "recommended_recovery_action": "download_from_source",
            },
            "arxiv_acquisition_unavailable",
        ),
        (
            "arxiv_acquisition_unavailable",
            {
                "acquisition_stage": "proxy_initialisation",
                "provider_invoked": False,
                "exception_type": "RuntimeError",
                "recommended_recovery_action": "reacquire_pdf_from_source",
            },
            "arxiv_acquisition_unavailable",
        ),
    ],
)
def test_arxiv_workflow_routes_recoverable_acquisition_failure_to_url_import(
    _canonical_seeded_mock_db: Any,
    import_succeeds: bool,
    download_error_code: str,
    download_error_details: dict[str, Any],
    expected_import_reason: str,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    registry = ActionRegistry()
    register_control_flow_actions(registry)
    download_calls: list[dict[str, Any]] = []
    import_calls: list[dict[str, Any]] = []
    subworkflow_calls: list[dict[str, Any]] = []
    verify_calls: list[dict[str, Any]] = []

    def _normalise_source(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "arxiv_id": request.inputs.get("arxiv_id"),
                "source_uri": request.inputs.get("source_uri"),
                "verification_profile": "arxiv",
            },
        )

    def _get_metadata(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "id": "2406.15341",
                    "title": "GenoTEX",
                    "summary": "Benchmark for automated gene expression analysis.",
                    "authors": ["Haoyang Liu"],
                    "categories": ["cs.LG"],
                    "publication_date": "2024-06-21",
                    "source_uri": "https://arxiv.org/abs/2406.15341",
                }
            },
        )

    def _inspect_existing_state(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "arxiv_id": request.inputs.get("arxiv_id"),
                "paper_concept_id": None,
                "file_copy_concept_id": None,
                "inspected_existing_state": True,
            },
        )

    def _decide_acquisition(
        _request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "acquisition_mode": "download_from_source",
                "acquisition_required": True,
                "arxiv_id": "2406.15341",
            },
        )

    def _download_failure(request: WorkflowActionRequest) -> WorkflowActionResult:
        download_calls.append(dict(request.inputs))
        return WorkflowActionResult(
            status="failed",
            error=download_error_code,
            outputs={
                "result": {
                    "success": False,
                    "error_code": download_error_code,
                    "error_details": dict(download_error_details),
                }
            },
        )

    def _import_url(request: WorkflowActionRequest) -> WorkflowActionResult:
        import_calls.append(dict(request.inputs))
        if not import_succeeds:
            return WorkflowActionResult(
                status="failed",
                error="remote_request_failed",
                outputs={
                    "result": {
                        "success": False,
                        "error": "remote_request_failed",
                        "message": "Remote artefact request failed: timed out",
                        "final_url": request.inputs.get("url"),
                        "status_code": None,
                    }
                },
            )
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "concept_id": "#V#arxiv_pdf_file_from_workflow_url_import",
                    "final_url": request.inputs.get("url"),
                    "storage": {"backend": "test"},
                }
            },
        )

    def _delegate_to_scholarly(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        subworkflow_calls.append(dict(request.inputs))
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_concept_id": "#V#paper_on_arxiv_2406_15341_test",
                    "author_concept_ids": ["#V#person_haoyang_liu_test"],
                    "topic_concept_ids": ["#V#research_topic_cs_lg_test"],
                    "article_readback": {
                        "concept_id": "#V#paper_on_arxiv_2406_15341_test"
                    },
                }
            },
        )

    def _verify_representation(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        verify_calls.append(dict(request.inputs))
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": True,
                "paper_concept_id": request.inputs.get("paper_concept_id"),
                "file_copy_concept_id": request.inputs.get("file_copy_concept_id"),
                "scholarly_representation_verified": True,
                "verification_failures": [],
            },
        )

    for action_id, handler in {
        "arxiv.normalise_source": _normalise_source,
        "arxiv.inspect_existing_state": _inspect_existing_state,
        "get_paper_metadata": _get_metadata,
        "arxiv.decide_acquisition_mode": _decide_acquisition,
        "download_paper": _download_failure,
        "import_url_file_copy": _import_url,
        "workflow_invoke_subworkflow": _delegate_to_scholarly,
        "scholarly_paper.verify_representation": _verify_representation,
    }.items():
        registry.register(ActionSpec(action_id=action_id, handler=handler))

    result = WorkflowExecutor(registry=registry, max_transitions=20).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "prompt": "https://arxiv.org/abs/2406.15341",
            "arxiv_id": "2406.15341",
            "source_uri": "https://arxiv.org/abs/2406.15341",
        },
    )

    assert result.completed is True
    assert result.final_state.endswith("_completed")
    assert len(download_calls) == 2
    assert import_calls == [
        {
            "filename": "2406.15341.pdf",
            "index_in_rag": False,
            "max_bytes": 104857600,
            "max_redirects": 5,
            "source_system": "arxiv_pdf_url_workflow_fallback",
            "timeout_seconds": 30,
            "type_concept_id": "#V#arxiv_pdf_file",
            "url": "https://arxiv.org/pdf/2406.15341.pdf",
        }
    ]
    assert subworkflow_calls[0]["file_copy_concept_id"] == (
        "#V#arxiv_pdf_file_from_workflow_url_import" if import_succeeds else None
    )
    assert subworkflow_calls[0]["require_file_copy"] is False
    assert subworkflow_calls[0]["paper_metadata"]["title"] == "GenoTEX"
    assert verify_calls[0]["file_copy_concept_id"] == (
        "#V#arxiv_pdf_file_from_workflow_url_import" if import_succeeds else None
    )
    assert verify_calls[0]["require_file_copy"] is False
    assert result.data["arxiv_pdf_url"] == "https://arxiv.org/pdf/2406.15341.pdf"
    assert result.data["arxiv_pdf_import_reason"] == expected_import_reason
    if import_succeeds:
        assert "pdf_acquisition_status" not in result.data
    else:
        assert result.data["pdf_acquisition_status"] == "unavailable"
        assert result.data["pdf_acquisition_optional"] is True
        assert result.data["pdf_acquisition_failure_stage"] == (
            "import_arxiv_pdf_from_url"
        )
        assert result.data["arxiv_pdf_import_error"] == "remote_request_failed"
        assert result.data["arxiv_pdf_import_message"] == (
            "Remote artefact request failed: timed out"
        )


def test_metadata_workflow_executes_direct_scholarly_article_representation(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_EvidenceSummaryLLM(),
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            # Parent workflows preserve optional mapped fields as explicit nulls.
            # A null file-copy value must not enter the private file-link branch.
            "file_copy_concept_id": None,
            "paper_metadata": {
                "title": "Composable Identity Control for Multi-Character Illustration",
                "abstract": "A paper about composable identity control.",
                "authors": ["Zhongsheng Wang", "Ming Lin"],
                "keywords": ["computer vision", "story illustration"],
                "doi": "https://doi.org/10.1145/3743093.3770985",
                "publication_date": "2025-12-01",
            },
            "source_uri": "https://dl.acm.org/doi/full/10.1145/3743093.3770985",
        },
    )

    assert result.completed is True, json.dumps(
        {
            key: value
            for key, value in result.data.items()
            if key.startswith("last_") or "scope" in key or "error" in key
        },
        sort_keys=True,
        default=str,
    )
    assert result.final_state.endswith("_completed"), result.data.get(
        "last_action_outputs"
    )
    assert result.data.get("file_link_gate") == "optional"
    assert "file_link_scope_mode" not in result.data
    paper_concept_id = result.data.get("paper_concept_id")
    assert isinstance(paper_concept_id, str) and paper_concept_id.startswith("#V#")
    assert result.data.get("verification_passed") is True
    assert result.data.get("response_text")
    observations = result.data.get("observations")
    assert isinstance(observations, list)
    assert observations[0]["label"] == "paper_metadata_readback"

    paper_doc = concept_service.get_concept_by_concept_id(paper_concept_id)
    assert paper_doc is not None
    relationships = paper_doc.get("relationships") or {}
    assert "#V#scholarly_article" in (relationships.get("is_an_instance_of") or [])
    author_concept_ids = relationships.get("#V#authored_by") or []
    assert author_concept_ids
    from src.backend.services import ontology_publication_authority_service

    assert (
        ontology_publication_authority_service.concept_publication_context(
            paper_concept_id
        ).kind
        == ontology_publication_authority_service.PublicationContextKind.GLOBAL
    )
    assert all(
        ontology_publication_authority_service.concept_publication_context(
            author_concept_id
        ).kind
        == ontology_publication_authority_service.PublicationContextKind.GLOBAL
        for author_concept_id in author_concept_ids
    )
    assert relationships.get("#V#about") is None

    names = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasName",
            limit=20,
        )
    ]
    assert "Composable Identity Control for Multi-Character Illustration" in names
    doi_values = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="#V#has_doi",
            limit=5,
        )
    ]
    assert "https://doi.org/10.1145/3743093.3770985" in doi_values
    source_uri_values = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="#V#has_source_uri",
            limit=5,
        )
    ]
    assert "https://dl.acm.org/doi/full/10.1145/3743093.3770985" in (source_uri_values)
    topic_label_values = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="#V#has_topic_labels",
            limit=5,
        )
    ]
    assert "computer vision, story illustration" in topic_label_values
    text_relation_summary = result.data.get("article_text_relations_summary")
    assert isinstance(text_relation_summary, dict)
    summary_predicates = {
        group.get("predicate")
        for group in (text_relation_summary.get("groups") or [])
        if isinstance(group, dict)
    }
    assert "#V#has_doi" in summary_predicates
    assert "#V#has_source_uri" in summary_predicates
    assert "#V#has_publication_date" in summary_predicates
    assert "#V#has_topic_labels" in summary_predicates

    descriptions = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasDescription",
            limit=5,
        )
    ]
    assert "A paper about composable identity control." in descriptions


def test_metadata_workflow_reuses_global_papers_across_authorised_actors_but_not_name_only_people(
    _canonical_seeded_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_publication_authority_service

    registry_factory._resolve_subworkflow_definition.cache_clear()
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    second_actor_id = "#V#second_public_paper_actor"
    authorised_actor_ids = {
        _LIVE_ARXIV_ACCEPTANCE_USER_ID,
        second_actor_id,
    }

    def _roles(actor_id: str):
        if actor_id not in authorised_actor_ids:
            return ()
        return (
            ontology_publication_authority_service.AuthorityRoleEvidence(
                role=(
                    ontology_publication_authority_service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
                ),
                actor_concept_id=actor_id,
                organisation_concept_id=None,
                relation_id=f"test:global-paper-role:{actor_id}",
                revision="test-revision-cross-actor-1",
            ),
        )

    monkeypatch.setattr(
        ontology_publication_authority_service,
        "resolve_live_semantic_roles",
        _roles,
    )
    executor = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    )
    first_environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )
    second_environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=(
            f"{second_actor_id}@{_LIVE_ARXIV_ACCEPTANCE_ORG_ID.removeprefix('#V#')}"
        ),
        user_concept_id=second_actor_id,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )

    first = executor.run(
        definition,
        environment=first_environment,
        data={
            "paper_metadata": {
                "title": "Cross-actor Public Paper",
                "doi": "10.5555/cross-actor-public-paper",
                "authors": ["Alex Kim"],
            },
            "require_representation_evidence_summary": False,
        },
    )
    same_paper = executor.run(
        definition,
        environment=second_environment,
        data={
            "paper_metadata": {
                "title": "Cross-actor Public Paper, Corrected Title",
                "doi": "https://doi.org/10.5555/cross-actor-public-paper",
                "authors": ["Alex Kim"],
            },
            "require_representation_evidence_summary": False,
        },
    )
    second_paper = executor.run(
        definition,
        environment=second_environment,
        data={
            "paper_metadata": {
                "title": "A Distinct Paper with a Namesake Author",
                "doi": "10.5555/namesake-author-paper",
                "authors": ["Alex Kim"],
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert first.completed is True, first.error
    assert same_paper.completed is True, same_paper.error
    assert second_paper.completed is True, second_paper.error
    first_paper_id = first.data.get("paper_concept_id")
    assert same_paper.data.get("paper_concept_id") == first_paper_id
    assert second_paper.data.get("paper_concept_id") != first_paper_id

    first_author_ids = set(
        (concept_service.get_concept_by_concept_id(first_paper_id) or {})
        .get("relationships", {})
        .get("#V#authored_by", [])
    )
    second_author_ids = set(
        (
            concept_service.get_concept_by_concept_id(
                second_paper.data.get("paper_concept_id")
            )
            or {}
        )
        .get("relationships", {})
        .get("#V#authored_by", [])
    )
    assert len(first_author_ids) == 1
    assert len(second_author_ids) == 1
    assert first_author_ids.isdisjoint(second_author_ids)


def test_metadata_workflow_rejects_global_representation_without_live_role(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace="#V#unauthorised_paper_actor",
            user_concept_id="#V#unauthorised_paper_actor",
            org_concept_id=None,
        ),
        data={
            "paper_metadata": {
                "title": "Unauthorised Global Paper Attempt",
                "doi": "10.5555/unauthorised-global-paper",
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert result.completed is False
    assert result.data.get("last_action_error") == (
        "global_ontology_admin_authority_required"
    )
    predicted_paper_id = result.data.get("paper_external_identity_concept_id")
    assert isinstance(predicted_paper_id, str)
    with pytest.raises(concept_service.ConceptNotFoundError):
        concept_service.get_concept_by_concept_id(predicted_paper_id)


def test_metadata_workflow_keeps_local_file_copy_link_user_scoped(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()
    file_copy_concept_id = "#V#private_local_copy_of_public_paper"
    concept_service.create_concept(
        name="Local copy of public paper",
        concept_id=file_copy_concept_id,
        parent_concept_ids=["#V#computer_file"],
        create_as_instance=True,
        created_by_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        organisation_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        visibility_scope_mode="user_only_default",
        maintain_relationship_inverses=False,
    )
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "file_copy_concept_id": file_copy_concept_id,
            "paper_metadata": {
                "title": "Public Paper with a Private Local Copy",
                "doi": "10.5555/public-paper-private-copy",
                "authors": ["Ada Lovelace"],
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert result.completed is True, json.dumps(
        {
            key: value
            for key, value in result.data.items()
            if key.startswith("last_") or "scope" in key or "file" in key
        },
        sort_keys=True,
        default=str,
    )
    paper_concept_id = result.data.get("paper_concept_id")
    assert isinstance(paper_concept_id, str)
    paper_doc = concept_service.get_concept_by_concept_id(paper_concept_id) or {}
    assert not (
        (paper_doc.get("relationships") or {}).get(
            "#V#propositional_information_thing_has_computer_file"
        )
        or []
    )

    from src.backend.db.mongo_client import (
        get_scoped_knowledge_assertions_collection,
    )

    assertion = get_scoped_knowledge_assertions_collection().find_one(
        {
            "subject_concept_id": paper_concept_id,
            "predicate": "#V#local_file_copy",
            "object_concept_id": file_copy_concept_id,
        }
    )
    assert assertion is not None
    assert assertion.get("canonical_publication") is False
    assert (assertion.get("scope") or {}).get("mode") == "user"
    assert (assertion.get("scope") or {}).get("user_concept_id") == (
        _LIVE_ARXIV_ACCEPTANCE_USER_ID
    )


def test_metadata_workflow_reuses_existing_article_on_identical_retry(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    executor = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    )
    environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )
    workflow_data = {
        "paper_metadata": {
            "title": "Retry-safe Scholarly Article Representation",
            "abstract": "The same authorised request should reuse its article.",
            "authors": ["Ada Lovelace", "Grace Hopper"],
            "publication_date": "2026-08-18",
        },
        "require_representation_evidence_summary": False,
    }

    first = executor.run(
        definition,
        environment=environment,
        data=dict(workflow_data),
    )
    second = executor.run(
        definition,
        environment=environment,
        data=dict(workflow_data),
    )

    assert first.completed is True, first.error
    assert first.data.get("paper_external_identity_scheme") == "bibliographic"
    assert "article_resolution_status" not in first.data
    first_paper_concept_id = first.data.get("paper_concept_id")
    assert isinstance(first_paper_concept_id, str)

    assert second.completed is True, second.error
    assert second.data.get("paper_external_identity_scheme") == "bibliographic"
    assert second.data.get("paper_external_identity_resolution_status") == "resolved"
    assert "article_resolution_status" not in second.data
    assert second.data.get("paper_concept_id") == first_paper_concept_id
    assert "create_article_result" not in second.data
    assert concept_service.get_concept_by_concept_id(first_paper_concept_id) is not None


def test_metadata_workflow_reuses_same_doi_when_title_varies(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None
    executor = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    )
    environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )

    first = executor.run(
        definition,
        environment=environment,
        data={
            "paper_metadata": {
                "title": "Initial Display Title for a DOI Paper",
                "doi": "https://doi.org/10.5555/Identity-First",
            },
            "require_representation_evidence_summary": False,
        },
    )
    second = executor.run(
        definition,
        environment=environment,
        data={
            "paper_metadata": {
                "title": "Corrected Display Title for the Same DOI Paper",
                "doi": "10.5555/identity-first",
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert first.completed is True, first.error
    assert second.completed is True, second.error
    assert first.data.get("paper_external_identity_scheme") == "doi"
    assert second.data.get("paper_external_identity_scheme") == "doi"
    assert second.data.get("paper_external_identity_resolution_status") == "resolved"
    assert second.data.get("paper_concept_id") == first.data.get("paper_concept_id")
    assert "article_resolution_status" not in second.data
    assert "create_article_result" not in second.data


def test_metadata_workflow_creates_distinct_same_title_articles_for_different_dois(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None
    executor = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    )
    environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )
    title = "A Legitimate Scholarly Article Homonym"

    first = executor.run(
        definition,
        environment=environment,
        data={
            "paper_metadata": {"title": title, "doi": "10.5555/homonym-a"},
            "require_representation_evidence_summary": False,
        },
    )
    assert first.completed is True, first.error
    first_paper_concept_id = first.data.get("paper_concept_id")
    assert isinstance(first_paper_concept_id, str)
    first_text_snapshot = _snapshot_concept_text_relations(first_paper_concept_id)

    second = executor.run(
        definition,
        environment=environment,
        data={
            "paper_metadata": {"title": title, "doi": "10.5555/homonym-b"},
            "require_representation_evidence_summary": False,
        },
    )

    assert second.completed is True, second.error
    second_paper_concept_id = second.data.get("paper_concept_id")
    assert isinstance(second_paper_concept_id, str)
    assert second_paper_concept_id != first_paper_concept_id
    assert second.data.get("paper_external_identity_scheme") == "doi"
    assert "article_resolution_status" not in second.data
    assert _snapshot_concept_text_relations(first_paper_concept_id) == (
        first_text_snapshot
    )
    assert {
        row.get("text")
        for row in get_texts_for_concept(
            second_paper_concept_id,
            predicate="#V#has_doi",
            limit=5,
        )
    } == {"10.5555/homonym-b"}


def test_metadata_workflow_creates_distinct_same_title_articles_for_different_source_uris(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None
    executor = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    )
    environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )
    title = "A Source-identified Scholarly Article Homonym"
    first_source_uri = "https://publisher.example/papers/homonym-a"
    second_source_uri = "https://publisher.example/papers/homonym-b"

    first = executor.run(
        definition,
        environment=environment,
        data={
            "paper_metadata": {"title": title},
            "source_uri": first_source_uri,
            "require_representation_evidence_summary": False,
        },
    )
    assert first.completed is True, first.error
    first_paper_concept_id = first.data.get("paper_concept_id")
    assert isinstance(first_paper_concept_id, str)
    first_text_snapshot = _snapshot_concept_text_relations(first_paper_concept_id)

    second = executor.run(
        definition,
        environment=environment,
        data={
            "paper_metadata": {"title": title},
            "source_uri": second_source_uri,
            "require_representation_evidence_summary": False,
        },
    )

    assert second.completed is True, second.error
    second_paper_concept_id = second.data.get("paper_concept_id")
    assert isinstance(second_paper_concept_id, str)
    assert second_paper_concept_id != first_paper_concept_id
    assert second.data.get("paper_external_identity_scheme") == "url"
    assert "article_resolution_status" not in second.data
    assert _snapshot_concept_text_relations(first_paper_concept_id) == (
        first_text_snapshot
    )
    assert {
        row.get("text")
        for row in get_texts_for_concept(
            second_paper_concept_id,
            predicate="#V#has_source_uri",
            limit=5,
        )
    } == {second_source_uri}


def test_metadata_workflow_rejects_title_only_reuse_before_mutation(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None
    executor = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    )
    environment = WorkflowEnvironment(
        llm_client=None,
        user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
        user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
    )
    workflow_data = {
        "paper_metadata": {
            "title": "Identity-less Title Collision",
            "abstract": "A title alone does not prove paper identity.",
        },
        "require_representation_evidence_summary": False,
    }

    first = executor.run(
        definition,
        environment=environment,
        data=dict(workflow_data),
    )
    assert first.completed is True, first.error
    paper_concept_id = first.data.get("paper_concept_id")
    assert isinstance(paper_concept_id, str)
    before_retry = _snapshot_concept_text_relations(paper_concept_id)

    second = executor.run(
        definition,
        environment=environment,
        data=dict(workflow_data),
    )

    assert second.completed is False
    assert second.data.get("article_resolution_status") == "resolved"
    assert second.data.get("paper_concept_id") == paper_concept_id
    assert second.data.get("last_action_error") == (
        "scholarly_paper_title_only_reuse_unverified"
    )
    assert _snapshot_concept_text_relations(paper_concept_id) == before_retry


@pytest.mark.parametrize(
    "scope_mode",
    ["organisation_general", "global_general"],
)
def test_metadata_workflow_does_not_mutate_shared_article_by_title_alone(
    _canonical_seeded_mock_db: Any,
    scope_mode: str,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    title = "Shared Scope Scholarly Article"
    shared_concept_id = "#V#shared_scope_scholarly_article"
    concept_service.create_concept(
        name=title,
        concept_id=shared_concept_id,
        parent_concept_ids=["#V#scholarly_article"],
        create_as_instance=True,
        description="The shared article description must remain unchanged.",
        created_by_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        organisation_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        visibility_scope_mode=scope_mode,
        maintain_relationship_inverses=False,
    )
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_metadata": {
                "title": title,
                "abstract": "This private request must not modify the shared article.",
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert result.completed is False
    assert result.data.get("article_resolution_status") == "resolved"
    assert result.data.get("paper_concept_id") == shared_concept_id
    assert result.data.get("last_action_error") == (
        "scholarly_paper_title_only_reuse_unverified"
    )
    descriptions = [
        row.get("text")
        for row in get_texts_for_concept(
            shared_concept_id,
            predicate="hasDescription",
            limit=10,
        )
    ]
    assert descriptions == ["The shared article description must remain unchanged."]


@pytest.mark.parametrize(
    "scope_mode",
    ["organisation_general", "global_general"],
)
def test_metadata_workflow_validates_supplied_article_scope_before_text_mutation(
    _canonical_seeded_mock_db: Any,
    scope_mode: str,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    paper_concept_id = "#V#supplied_shared_scholarly_article"
    concept_service.create_concept(
        name="Supplied Shared Scholarly Article",
        concept_id=paper_concept_id,
        parent_concept_ids=["#V#scholarly_article"],
        create_as_instance=True,
        description="Original shared description.",
        created_by_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        organisation_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        visibility_scope_mode=scope_mode,
        maintain_relationship_inverses=False,
    )
    before_text = _snapshot_concept_text_relations(paper_concept_id)
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_concept_id": paper_concept_id,
            "paper_metadata": {
                "title": "Attempted Shared Article Rewrite",
                "abstract": "This must not be attached to the shared target.",
            },
            "require_representation_evidence_summary": False,
        },
    )

    if scope_mode == "organisation_general":
        assert result.completed is False
        assert result.data.get("last_action_error") == (
            "scholarly_paper_global_target_required"
        )
        assert _snapshot_concept_text_relations(paper_concept_id) == before_text
    else:
        assert result.completed is True, result.error
        assert result.data.get("paper_external_identity_resolution_status") == (
            "supplied"
        )
        assert "Attempted Shared Article Rewrite" in {
            row.get("text")
            for row in get_texts_for_concept(
                paper_concept_id,
                predicate="hasName",
                limit=10,
            )
        }


def test_metadata_workflow_rejects_supplied_private_article_without_trusted_actor(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    paper_concept_id = "#V#other_users_private_scholarly_article"
    concept_service.create_concept(
        name="Other User's Private Scholarly Article",
        concept_id=paper_concept_id,
        parent_concept_ids=["#V#scholarly_article"],
        create_as_instance=True,
        description="Other user's original description.",
        created_by_concept_id="#V#other_user",
        visibility_scope_mode="user_only_default",
        maintain_relationship_inverses=False,
    )
    before_text = _snapshot_concept_text_relations(paper_concept_id)
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=None,
            user_concept_id=None,
            org_concept_id=None,
        ),
        data={
            "paper_concept_id": paper_concept_id,
            "paper_metadata": {
                "title": "Attempted Sessionless Rewrite",
                "abstract": "This must not be attached without a trusted actor.",
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert result.completed is False
    assert result.data.get("last_action_error") == (
        "scholarly_paper_global_target_required"
    )
    assert _snapshot_concept_text_relations(paper_concept_id) == before_text


def test_metadata_workflow_rejects_supplied_actor_private_article(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    paper_concept_id = "#V#supplied_actor_private_scholarly_article"
    concept_service.create_concept(
        name="Supplied Actor-private Scholarly Article",
        concept_id=paper_concept_id,
        parent_concept_ids=["#V#scholarly_article"],
        create_as_instance=True,
        created_by_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
        organisation_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        visibility_scope_mode="user_only_default",
        maintain_relationship_inverses=False,
    )
    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_concept_id": paper_concept_id,
            "paper_metadata": {
                "title": "Supplied Actor-private Scholarly Article",
                "abstract": "The trusted actor may extend their private article.",
            },
            "require_representation_evidence_summary": False,
        },
    )

    assert result.completed is False
    assert result.data.get("last_action_error") == (
        "scholarly_paper_global_target_required"
    )
    descriptions = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasDescription",
            limit=10,
        )
    ]
    assert "The trusted actor may extend their private article." not in descriptions


def test_metadata_workflow_represents_sparse_source_uri_without_title(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    source_uri = "https://dl.acm.org/doi/full/10.1145/3743093.3770985"
    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_EvidenceSummaryLLM(),
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "source_uri": source_uri,
            "summary": None,
            "publication_date": None,
        },
    )

    assert result.completed is True
    paper_concept_id = result.data.get("paper_concept_id")
    assert isinstance(paper_concept_id, str) and paper_concept_id.startswith("#V#")

    paper_doc = concept_service.get_concept_by_concept_id(paper_concept_id)
    assert paper_doc is not None
    assert paper_doc.get("name")
    relationships = paper_doc.get("relationships") or {}
    assert "#V#scholarly_article" in (relationships.get("is_an_instance_of") or [])
    names = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasName",
            limit=20,
        )
    ]
    assert source_uri in names
    source_uri_values = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="#V#has_source_uri",
            limit=5,
        )
    ]
    assert source_uri in source_uri_values


def test_metadata_workflow_treats_null_paper_concept_id_as_absent(
    _canonical_seeded_mock_db: Any,
) -> None:
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_EvidenceSummaryLLM(),
            user_namespace=_LIVE_ARXIV_ACCEPTANCE_NAMESPACE,
            user_concept_id=_LIVE_ARXIV_ACCEPTANCE_USER_ID,
            org_concept_id=_LIVE_ARXIV_ACCEPTANCE_ORG_ID,
        ),
        data={
            "paper_concept_id": None,
            "arxiv_id": "2604.22937",
            "paper_metadata": {
                "title": "Null Paper Concept Should Create an Article",
                "abstract": "The workflow should treat a null identifier as absent.",
                "authors": ["Ada Lovelace"],
                "keywords": ["workflow composition"],
                "publication_date": "2026-04-29",
            },
            "source_uri": "https://arxiv.org/abs/2604.22937",
        },
    )

    assert result.completed is True, result.error
    assert result.final_state.endswith("_completed"), result.data.get(
        "last_action_outputs"
    )
    assert result.data.get("paper_external_identity_scheme") == "arxiv"
    assert (result.data.get("create_article_result") or {}).get("successful") == 1
    paper_concept_id = result.data.get("paper_concept_id")
    assert isinstance(paper_concept_id, str) and paper_concept_id.startswith("#V#")

    paper_doc = concept_service.get_concept_by_concept_id(paper_concept_id)
    assert paper_doc is not None
    relationships = paper_doc.get("relationships") or {}
    assert "#V#scholarly_article" in (relationships.get("is_an_instance_of") or [])
    names = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasName",
            limit=20,
        )
    ]
    assert "2604.22937" in names
    source_uri_values = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="#V#has_source_uri",
            limit=5,
        )
    ]
    assert "https://arxiv.org/abs/2604.22937" in source_uri_values


def test_bootstrap_preserves_authoritative_state_when_repo_seed_snapshot_is_stale(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    mapping_concept_id = (
        "#V#workflow_mapping_arxiv_paper_representation_workflow_"
        "decide_acquisition_mode_file_copy_concept_id_to_file_copy_concept_id_parameter"
    )
    mapping_doc = concept_service.get_concept_by_concept_id(mapping_concept_id)
    assert mapping_doc is not None
    mapping_spec = dict(
        ((mapping_doc.get("concept_data") or {}).get("workflow_mapping_spec") or {})
    )
    mapping_spec["required"] = True
    concept_service.update_concept(
        mapping_concept_id,
        {"concept_data.workflow_mapping_spec": mapping_spec},
    )

    drifted_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert drifted_definition is not None
    drifted_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="decide_acquisition_mode",
    )
    drifted_action = drifted_definition.states[drifted_state_id].actions[0]
    assert drifted_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": mapping_concept_id,
        "$required": True,
    }

    launch_contract, _launch_source = resolve_workflow_launch_input_contract(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(launch_contract, dict)
    updated_launch_contract = json.loads(json.dumps(launch_contract))
    updated_launch_contract.setdefault("input_mappings", []).append(
        {
            "target_context_key": "paper_concept_id",
            "source_expression": "inputs.paper_concept_id",
            "extractor": "identity",
            "required": False,
            "description": (
                "Allow callers to pin an existing paper concept when the "
                "authoritative Vontology workflow is updated."
            ),
        }
    )
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        payload=updated_launch_contract,
    )
    updated_routing_profile = {
        "schema_version": "workflow_routing_profile.v1",
        "role": "authoring",
        "authoring_intent_required": True,
        "prefer_existing_capability": True,
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        payload=updated_routing_profile,
    )
    updated_discovery_exemplars = {
        "schema_version": "workflow_discovery_exemplars.v1",
        "keywords": ["arxiv", "paper", "authority-only-export-sentinel"],
        "examples": ["Represent this arXiv paper from its identifier."],
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowDiscoveryExemplarsJson",
        payload=updated_discovery_exemplars,
    )

    repair_report = bootstrap_canonical_paper_representation_workflows()
    repair_preflight = repair_report.get("materialisation_preflight") or {}
    repair_publication = repair_report.get("publication") or {}
    repair_counts = repair_publication.get("counts") or {}

    assert repair_preflight.get("already_current") is True
    assert repair_preflight.get("drift_detected") is False
    assert repair_preflight.get("drift_workflow_ids") == []
    assert repair_preflight.get("issue_codes") == []
    assert repair_preflight.get("bundle_snapshot_drift_detected") is False
    assert repair_preflight.get("bundle_snapshot_drift_workflow_ids") == []
    assert repair_preflight.get("bundle_snapshot_issue_codes") == []
    assert repair_publication.get("skipped") is True
    assert repair_publication.get("materialisation_status") == "current"
    assert repair_publication.get("drift_detected") is False
    assert repair_publication.get("issue_codes") == []
    assert repair_publication.get("bundle_snapshot_drift_detected") is False
    assert repair_publication.get("bundle_snapshot_drift_workflow_ids") == []
    assert repair_publication.get("bundle_snapshot_issue_codes") == []
    assert repair_counts.get("workflows_published") == 0
    assert repair_counts.get("errors") == 1
    authority_blockers = (repair_report.get("repo_seed_version_gate") or {}).get(
        "authority_blockers_by_workflow"
    ) or {}
    assert (
        authority_blockers[ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID]["error_code"]
        == "live_authority_requires_explicit_migration"
    )

    repaired_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert repaired_definition is not None
    repaired_action = repaired_definition.states[drifted_state_id].actions[0]
    assert repaired_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": mapping_concept_id,
        "$required": True,
    }
    repaired_launch_contract, repaired_launch_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert repaired_launch_source.startswith("text_relation:")
    assert repaired_launch_contract == updated_launch_contract
    repaired_routing_profile, repaired_routing_source = (
        resolve_workflow_routing_profile(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert repaired_routing_source.startswith("text_relation:")
    assert repaired_routing_profile == {
        **updated_routing_profile,
        "explicit_workflow_context_required": False,
    }
    repaired_discovery_exemplars, repaired_discovery_source = (
        resolve_workflow_discovery_exemplars(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert repaired_discovery_source.startswith("text_relation:")
    assert repaired_discovery_exemplars == updated_discovery_exemplars


def test_bootstrap_repairs_explicit_unpublished_lifecycle(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    unpublished_payload = {
        "schema_version": "workflow_publication_lifecycle.v1",
        "phase": "draft",
        "published": False,
        "validation_passed": False,
        "postconditions_verified": False,
    }
    concept_service.update_concept(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        {"concept_data.workflow_publication_lifecycle": unpublished_payload},
    )
    upsert_singleton_text_relation(
        subject_concept_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLifecycleJson",
        text=json.dumps(unpublished_payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"source": "test_bootstrap_repairs_explicit_unpublished_lifecycle"},
        garbage_collect=True,
    )
    invalidate_workflow_discovery_executability_caches()

    repair_report = bootstrap_canonical_paper_representation_workflows()
    preflight = repair_report.get("materialisation_preflight") or {}
    publication = repair_report.get("publication") or {}

    assert preflight.get("drift_detected") is True
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        preflight.get("drift_workflow_ids") or []
    )
    assert "workflow_not_published" in (preflight.get("issue_codes") or [])
    assert publication.get("materialisation_status") == "current"
    assert (publication.get("counts") or {}).get("errors") == 1
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID not in (
        publication.get("published_workflow_ids") or []
    )

    explicit_repair_report = bootstrap_canonical_paper_representation_workflows(
        force_republish=True
    )
    explicit_repair_publication = explicit_repair_report.get("publication") or {}
    assert explicit_repair_publication.get("materialisation_status") == (
        "repaired_from_repo_seed"
    )
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        explicit_repair_publication.get("published_workflow_ids") or []
    )

    repaired_lifecycle, repaired_source = resolve_workflow_publication_lifecycle(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert repaired_lifecycle == {
        "schema_version": "workflow_publication_lifecycle.v1",
        "phase": "published",
        "published": True,
    }
    assert repaired_source in {
        "concept_data",
        "text_relation:#V#hasWorkflowLifecycleJson",
    }


def test_bootstrap_repairs_missing_required_launch_metadata_surfaces(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    _delete_workflow_text_relations(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
    )
    _delete_workflow_text_relations(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#has_launch_contract",
    )
    invalidate_workflow_discovery_executability_caches()

    broken_launch_input_contract, broken_launch_input_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    broken_explicit_launch_contract, broken_explicit_launch_source = (
        resolve_workflow_launch_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert broken_launch_input_contract is None
    assert broken_launch_input_source == "none"
    assert broken_explicit_launch_contract is None
    assert broken_explicit_launch_source == "none"

    repair_report = bootstrap_canonical_paper_representation_workflows()
    preflight = repair_report.get("materialisation_preflight") or {}
    publication = repair_report.get("publication") or {}

    assert preflight.get("drift_detected") is True
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        preflight.get("drift_workflow_ids") or []
    )
    assert "required_authority_surface_missing" in (preflight.get("issue_codes") or [])
    workflow_status = (preflight.get("workflow_status_by_id") or {}).get(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    ) or {}
    assert workflow_status.get("status") == "required_authority_surface_missing"
    assert sorted(workflow_status.get("missing_authority_surfaces") or []) == [
        "launch_contract",
        "launch_input_contract",
    ]
    assert publication.get("materialisation_status") == "current"
    assert (publication.get("counts") or {}).get("errors") == 1
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID not in (
        publication.get("published_workflow_ids") or []
    )

    explicit_repair_report = bootstrap_canonical_paper_representation_workflows(
        force_republish=True
    )
    explicit_repair_publication = explicit_repair_report.get("publication") or {}
    assert explicit_repair_publication.get("materialisation_status") == (
        "repaired_from_repo_seed"
    )
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        explicit_repair_publication.get("published_workflow_ids") or []
    )

    repaired_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert repaired_definition is not None
    assert repaired_definition.metadata.get("launch_contract") == {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {
                "type": "context_key_present",
                "key": "prompt",
                "required": True,
            }
        ],
    }
    assert repaired_definition.metadata.get("launch_input_contract", {}).get(
        "required_inputs"
    ) == ["prompt"]
    assert repaired_definition.metadata.get("launch_contract_source") == (
        "text_relation:#V#has_launch_contract"
    )
    assert repaired_definition.metadata.get("launch_input_contract_source") == (
        "text_relation:#V#hasWorkflowLaunchInputContractJson"
    )


def test_export_refreshes_paper_repo_seed_bundle_from_authority(
    _reset_mock_db: Any,
    tmp_path: Path,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    updated_launch_contract = {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": ["prompt"],
        "input_mappings": [
            {
                "target_context_key": "prompt",
                "source_expression": "inputs.prompt",
                "extractor": "identity",
                "required": True,
                "description": "Pass through the prompt.",
            },
            {
                "target_context_key": "paper_concept_id",
                "source_expression": "inputs.paper_concept_id",
                "extractor": "identity",
                "required": False,
                "description": "Allow an explicit paper concept override.",
            },
        ],
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        payload=updated_launch_contract,
    )
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        payload={
            "schema_version": "workflow_routing_profile.v1",
            "role": "authoring",
            "authoring_intent_required": True,
            "prefer_existing_capability": True,
        },
    )
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowDiscoveryExemplarsJson",
        payload={
            "schema_version": "workflow_discovery_exemplars.v1",
            "keywords": ["arxiv", "paper", "authority-export"],
            "examples": [
                "Refresh the repo seed from authoritative paper workflow state."
            ],
        },
    )

    tmp_asset_path = tmp_path / _PAPER_REPO_SEED_ASSET_PATH.name
    tmp_asset_path.write_text(
        _PAPER_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    before_diff = diff_canonical_paper_representation_workflow_repo_seed_bundle(
        asset_path=tmp_asset_path
    )
    assert before_diff.get("has_differences") is True

    export_report = export_canonical_paper_representation_workflow_repo_seed_bundle(
        asset_path=tmp_asset_path
    )
    assert export_report.get("asset_path") == str(tmp_asset_path.resolve())
    assert export_report.get("workflow_ids") == [
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
        SCHOLARLY_PUBLIC_AUTHOR_REPRESENTATION_WORKFLOW_ID,
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID,
        PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID,
        SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID,
        PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
    ]

    payload = json.loads(tmp_asset_path.read_text(encoding="utf-8"))
    workflows = payload.get("workflows") or []
    arxiv_entry = next(
        item
        for item in workflows
        if item.get("workflow_id") == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert arxiv_entry.get("launch_input_contract") == updated_launch_contract
    text_relations = arxiv_entry.get("text_relations") or []
    assert any(
        item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
        for item in text_relations
    )
    assert any(
        item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
        for item in text_relations
    )
    assert any(
        item.get("predicate") == "#V#hasWorkflowTerminalSuccessContractJson"
        for item in text_relations
    )

    after_diff = diff_canonical_paper_representation_workflow_repo_seed_bundle(
        asset_path=tmp_asset_path
    )
    assert after_diff.get("has_differences") is False


def test_live_arxiv_paper_representation_workflow_acceptance(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _live_acceptance_enabled():
        _skip_live_acceptance()

    definition = _prepare_live_arxiv_acceptance_environment(monkeypatch)
    report = _execute_live_arxiv_acceptance_case(
        definition=definition,
        paper_ref=_resolve_live_arxiv_acceptance_primary_paper(),
    )

    assert report.get("success") is True, report


def test_live_arxiv_paper_representation_workflow_acceptance_batch(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _live_acceptance_enabled(batch=True):
        _skip_live_acceptance(batch=True)

    sample = _resolve_live_arxiv_acceptance_sample()
    assert len(sample) >= 10, {
        "error": "live_arxiv_acceptance_sample_too_small",
        "count": len(sample),
        "sample": sample,
    }

    definition = _prepare_live_arxiv_acceptance_environment(monkeypatch)
    reports = [
        _execute_live_arxiv_acceptance_case(definition=definition, paper_ref=paper_ref)
        for paper_ref in sample
    ]
    success_count = sum(1 for report in reports if report.get("success") is True)
    success_rate_pct = round((float(success_count) * 100.0) / float(len(reports)), 2)
    failures = [report for report in reports if report.get("success") is not True]

    assert success_rate_pct >= 90.0, {
        "success_rate_pct": success_rate_pct,
        "success_count": success_count,
        "case_count": len(reports),
        "failures": failures,
    }
