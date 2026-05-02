from __future__ import annotations

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
    SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID,
    SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
    diff_canonical_paper_representation_workflow_repo_seed_bundle,
    export_canonical_paper_representation_workflow_repo_seed_bundle,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.services.text_value_service import (
    delete_text_relation,
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.durable import registry_factory
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_contract,
    resolve_workflow_launch_input_contract,
    resolve_workflow_publication_lifecycle,
    resolve_workflow_routing_profile,
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
_LIVE_ARXIV_ACCEPTANCE_NAMESPACE = (
    f"{_LIVE_ARXIV_ACCEPTANCE_USER_ID}@{_LIVE_ARXIV_ACCEPTANCE_ORG_ID.removeprefix('#V#')}"
)


class _EvidenceSummaryLLM:
    def generate(self, prompt: str, context=None, model=None) -> str:
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
    pytest.skip(
        f"Set {_LIVE_ARXIV_ACCEPTANCE_RUN_ENV}=1 to run live arXiv acceptance."
    )


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
                llm_client=None,
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
                "final_status": "completed" if result.completed else "failed",
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


def test_bootstrap_materialises_paper_representation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_paper_representation_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 3
    assert counts.get("errors") == 0
    support_concepts = report.get("support_concepts") or {}
    assert support_concepts.get("errors") == []
    assert "#V#scholarly_article" in (
        support_concepts.get("created_concept_ids") or []
    )
    assert "#V#has_doi" in (support_concepts.get("created_concept_ids") or [])
    assert "#V#has_source_uri" in (
        support_concepts.get("created_concept_ids") or []
    )
    source_uri_predicate = concept_service.get_concept_by_concept_id(
        "#V#has_source_uri"
    )
    assert "#V#predicate" in (
        (source_uri_predicate.get("relationships") or {}).get("is_an_instance_of")
        or []
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

    metadata_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID
    )
    assert metadata_definition is not None

    scholarly_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_definition is not None

    arxiv_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert arxiv_definition is not None
    metadata_terminal_contract = metadata_definition.metadata.get(
        "terminal_success_contract"
    )
    assert isinstance(metadata_terminal_contract, dict)
    assert metadata_terminal_contract.get("success_statuses") == ["completed"]
    metadata_launch_contract = metadata_definition.metadata.get(
        "launch_input_contract"
    )
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
    assert decide_metadata_transitions[
        "prompt_text_available"
    ].to_state == extract_metadata_state_id
    extract_metadata_action = metadata_definition.states[
        extract_metadata_state_id
    ].actions[0]
    assert extract_metadata_action.action_id == "llm.action"
    assert extract_metadata_action.execution_mode == "llm"
    assert extract_metadata_action.prompt_contract["requested_prompt_concept_ids"] == [
        "#V#prompt_scholarly_article_metadata_extraction"
    ]
    extract_mappings = (
        metadata_definition.states[
            extract_metadata_state_id
        ].metadata.get("tool_output_context_mappings")
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
    normalise_metadata_transitions = {
        transition.reason: transition
        for transition in metadata_definition.states[
            normalise_metadata_state_id
        ].transitions
    }
    assert normalise_metadata_transitions[
        "existing_article_concept_supplied"
    ].condition_spec == {
        "kind": "all",
        "conditions": [
            {
                "kind": "context_exists",
                "key": "paper_concept_id",
                "expected": True,
            },
            {
                "kind": "context_is_null",
                "key": "paper_concept_id",
                "expected": False,
            },
        ],
    }
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
    assert decide_arxiv_transitions[
        "arxiv_id_present"
    ].to_state == attach_arxiv_identity_state_id
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
    ]
    assert any(
        transition.to_state == summary_state_id
        for transition in metadata_definition.states[
            read_back_text_relations_state_id
        ].transitions
    )
    summary_action = metadata_definition.states[summary_state_id].actions[0]
    assert summary_action.action_id == "llm.action"
    assert summary_action.execution_mode == "llm"
    assert summary_action.prompt_contract["requested_prompt_concept_ids"] == [
        "#V#prompt_scholarly_article_representation_evidence_summary"
    ]
    summary_mappings = (
        metadata_definition.states[
            summary_state_id
        ].metadata.get("tool_output_context_mappings")
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
    arxiv_terminal_contract = arxiv_definition.metadata.get(
        "terminal_success_contract"
    )
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
    assert isinstance(
        routing_profile.get("explicit_workflow_context_required"), bool
    )

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
    normalise_action = arxiv_definition.states[normalise_state_id].actions[0]
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
        "$required": True,
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
    assert decide_transitions["existing_file_copy_available"].to_state == delegate_state_id
    assert decide_transitions["existing_file_copy_available"].condition_spec == {
        "key": "acquisition_mode",
        "kind": "context_value_equals",
        "value": "existing_file_copy",
    }
    assert decide_transitions["finalise_cached_pdf"].to_state == finalise_cached_pdf_state_id
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
    scholarly_verify_action = scholarly_definition.states[scholarly_verify_state_id].actions[0]
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
    step_types = (step_concept.get("relationships") or {}).get("is_an_instance_of") or []
    assert "#V#workflow_step" in step_types


def test_bootstrap_skips_republication_when_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_paper_representation_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 3

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
    upsert_singleton_text_relation(
        subject_concept_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        text=json.dumps(
            {
                "schema_version": "workflow_repo_seed_version.v1",
                "seed_version": "8",
                "family_id": "paper_representation_workflow_seed_bundle",
                "source_tag": "JVNAUTOSCI-2192",
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
    status = (
        (preflight.get("repo_seed_version_gate") or {})
        .get("status_by_workflow_id", {})
        .get(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
        or {}
    )
    assert status.get("reason") == "repo_seed_version_newer"

    refreshed_launch_contract, refreshed_launch_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert refreshed_launch_source == "text_relation:#V#hasWorkflowLaunchInputContractJson"
    assert isinstance(refreshed_launch_contract, dict)
    arxiv_sources = {
        mapping.get("source_expression")
        for mapping in refreshed_launch_contract.get("input_mappings") or []
        if mapping.get("target_context_key") == "arxiv_id"
    }
    assert {
        "inputs.arxiv_id",
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
        "inputs.prompt",
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
    assert any(payload.get("seed_version") == "11" for payload in marker_payloads)

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
    assert required_effect["targets_extractor"] == "arxiv_id_list"
    assert "workflow_discovery_result.discovery_query_input" in (
        required_effect["targets_source_expressions"]
    )


def test_metadata_workflow_executes_direct_scholarly_article_representation(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()
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

    assert result.completed is True
    assert result.final_state.endswith("_completed")
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
    assert relationships.get("#V#authored_by")
    assert relationships.get("#V#about")

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
    assert "https://dl.acm.org/doi/full/10.1145/3743093.3770985" in (
        source_uri_values
    )
    text_relation_summary = result.data.get("article_text_relations_summary")
    assert isinstance(text_relation_summary, dict)
    summary_predicates = {
        group.get("predicate")
        for group in (text_relation_summary.get("groups") or [])
        if isinstance(group, dict)
    }
    assert "#V#has_doi" in summary_predicates
    assert "#V#has_source_uri" in summary_predicates

    descriptions = [
        row.get("text")
        for row in get_texts_for_concept(
            paper_concept_id,
            predicate="hasDescription",
            limit=5,
        )
    ]
    assert "A paper about composable identity control." in descriptions


def test_metadata_workflow_represents_sparse_source_uri_without_title(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()
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
        data={"source_uri": source_uri},
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
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()
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
    assert result.final_state.endswith("_completed")
    assert isinstance(result.data.get("create_article_result"), dict)
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
    assert repair_counts.get("errors") == 0

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
    repaired_routing_profile, repaired_routing_source = resolve_workflow_routing_profile(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
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
    assert publication.get("materialisation_status") == "repaired_from_repo_seed"
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        publication.get("published_workflow_ids") or []
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
    assert "required_authority_surface_missing" in (
        preflight.get("issue_codes") or []
    )
    workflow_status = (
        (preflight.get("workflow_status_by_id") or {}).get(
            ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
        )
        or {}
    )
    assert workflow_status.get("status") == "required_authority_surface_missing"
    assert sorted(workflow_status.get("missing_authority_surfaces") or []) == [
        "launch_contract",
        "launch_input_contract",
    ]
    assert publication.get("materialisation_status") == "repaired_from_repo_seed"
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        publication.get("published_workflow_ids") or []
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
            "examples": ["Refresh the repo seed from authoritative paper workflow state."],
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
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
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
