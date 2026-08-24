from __future__ import annotations

import pytest

from src.backend.services.ontology_publication_authority_service import (
    PublicationContext,
)
from src.backend.services.paper_representation_workflow_vontology_service import (
    _REPO_SEED_ASSET_PATH,
    PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID,
    PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.paper_representation_workflow import (
    ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE,
    ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY,
    ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF,
    ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE,
    ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
    ARXIV_NORMALISE_SOURCE_ACTION_ID,
    PAPER_REFERENCE_NORMALISE_SET_ACTION_ID,
    PAPER_REFERENCE_PREPARE_PUBLIC_TITLE_SEARCH_ACTION_ID,
    PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
    PAPER_REFERENCE_SELECT_ARXIV_TITLE_MATCH_ACTION_ID,
    PAPER_REFERENCE_VERIFY_UNDER_PREPARATION_ACTION_ID,
    SCHOLARLY_PAPER_ENRICH_ACTION_ID,
    SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID,
    SCHOLARLY_PAPER_LINK_FILE_COPY_ACTION_ID,
    SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
    SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
    SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
    SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    register_paper_representation_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)

_ACTOR_CONCEPT_ID = "#V#paper_workflow_actor"


def _paper_registry() -> ActionRegistry:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)
    return registry


def test_register_paper_representation_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    assert {
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        ARXIV_NORMALISE_SOURCE_ACTION_ID,
        PAPER_REFERENCE_NORMALISE_SET_ACTION_ID,
        PAPER_REFERENCE_PREPARE_PUBLIC_TITLE_SEARCH_ACTION_ID,
        PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
        PAPER_REFERENCE_SELECT_ARXIV_TITLE_MATCH_ACTION_ID,
        PAPER_REFERENCE_VERIFY_UNDER_PREPARATION_ACTION_ID,
        SCHOLARLY_PAPER_ENRICH_ACTION_ID,
        SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    }.issubset(set(registry.all_action_ids()))
    verify_spec = registry.get(SCHOLARLY_PAPER_VERIFY_ACTION_ID)
    assert verify_spec is not None
    assert verify_spec.required_tool_operation_class == "verification_read"


def test_normalise_reference_set_preserves_explicit_under_preparation_evidence() -> (
    None
):
    result = _paper_registry().execute(
        PAPER_REFERENCE_NORMALISE_SET_ACTION_ID,
        inputs={
            "paper_references": [
                {
                    "reference_kind": "metadata",
                    "title": "A Private Conference Submission",
                    "paper_type_concept_id": "#V#paper_under_preparation",
                    "paper_lifecycle_state": "under_review",
                    "submission_identifier": "17045",
                    "submission_venue": "NeurIPS 2026",
                }
            ],
            "source_context": {"source_kind": "private_email"},
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    item = result.outputs["paper_reference_items"][0]
    assert item["paper_type_concept_id"] == "#V#paper_under_preparation"
    assert item["paper_lifecycle_state"] == "under_review"
    assert item["submission_identifier"] == "17045"
    assert item["submission_venue"] == "NeurIPS 2026"
    assert item["paper_metadata"]["lifecycle_state"] == "under_review"


def test_prepare_under_preparation_identity_is_org_scoped_and_idempotent() -> None:
    registry = _paper_registry()
    inputs = {
        "paper_type_concept_id": "#V#paper_under_preparation",
        "title": "A Private Conference Submission",
        "paper_lifecycle_state": "under_review",
        "submission_identifier": "17045",
        "submission_venue": "NeurIPS 2026",
    }
    environment = WorkflowEnvironment(
        llm_client=None,
        org_concept_id="#V#test_research_org",
    )

    first = registry.execute(
        PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
        inputs=inputs,
        context={},
        env=environment,
    )
    second = registry.execute(
        PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
        inputs=inputs,
        context={"message_id": "second_email_with_same_paper"},
        env=environment,
    )

    assert first.status == "success"
    assert second.status == "success"
    assert first.outputs["paper_concept_id"] == second.outputs["paper_concept_id"]
    assert first.outputs["paper_concept_id"].startswith("#V#paper_under_preparation_")
    assert first.outputs["organisation_concept_id"] == "#V#test_research_org"
    assert first.outputs["under_preparation_identity_basis"] == (
        "organisation_submission_identifier"
    )

    title_only_inputs = {
        "paper_type_concept_id": "#V#paper_under_preparation",
        "title": "A Private Conference Submission",
        "paper_lifecycle_state": "under_preparation",
    }
    first_title_mention = registry.execute(
        PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
        inputs=title_only_inputs,
        context={"message_id": "first_email"},
        env=environment,
    )
    second_title_mention = registry.execute(
        PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
        inputs=title_only_inputs,
        context={"message_id": "another_email"},
        env=environment,
    )
    assert first_title_mention.outputs["paper_concept_id"] == (
        second_title_mention.outputs["paper_concept_id"]
    )
    assert first_title_mention.outputs["under_preparation_identity_basis"] == (
        "organisation_title"
    )

    missing_org = registry.execute(
        PAPER_REFERENCE_PREPARE_UNDER_PREPARATION_ACTION_ID,
        inputs=inputs,
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )
    assert missing_org.status == "failed"
    assert missing_org.error == "paper_under_preparation_organisation_missing"


def test_verify_under_preparation_requires_exact_org_type_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    concept_id = "#V#paper_under_preparation_test"
    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda _concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#paper_under_preparation"]},
        },
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda _concept_id: PublicationContext.organisation("#V#test_research_org"),
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [{"text": "A Private Conference Submission"}],
    )

    result = _paper_registry().execute(
        PAPER_REFERENCE_VERIFY_UNDER_PREPARATION_ACTION_ID,
        inputs={
            "paper_concept_id": concept_id,
            "title": "A Private Conference Submission",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            org_concept_id="#V#test_research_org",
        ),
    )

    assert result.status == "success"
    assert result.outputs["paper_under_preparation_verified"] is True
    assert result.outputs["paper_instance_scope_mode"] == "organisation_general"
    assert result.outputs["paper_publication_context"]["kind"] == "organisation"

    wrong_org = _paper_registry().execute(
        PAPER_REFERENCE_VERIFY_UNDER_PREPARATION_ACTION_ID,
        inputs={
            "paper_concept_id": concept_id,
            "title": "A Private Conference Submission",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            org_concept_id="#V#another_research_org",
        ),
    )
    assert wrong_org.status == "failed"
    assert wrong_org.error == "paper_under_preparation_organisation_scope_missing"


def test_under_preparation_workflow_uses_profiled_org_scope_and_canonical_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID],
    )[PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID]
    concept_id_holder: dict[str, str] = {}
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#paper_under_preparation"]},
        },
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda _concept_id: PublicationContext.organisation("#V#test_research_org"),
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [{"text": "A Private Conference Submission"}],
    )

    registry = _paper_registry()
    register_control_flow_actions(registry)

    def resolve_scope(request):
        calls.append(("resolve_scope", dict(request.inputs)))
        return WorkflowActionResult(
            status="success",
            outputs={
                "selected_scope_mode": "organisation_general",
                "decision_evidence": {"source": "represented_type_profile"},
            },
        )

    def create_concepts(request):
        calls.append(("create_concepts", dict(request.inputs)))
        concept_id = str(request.inputs["concepts"][0]["concept_id"])
        concept_id_holder["concept_id"] = concept_id
        return WorkflowActionResult(
            status="success",
            outputs={"result": {"results": [{"concept_id": concept_id}]}},
        )

    registry.register(
        ActionSpec(
            action_id="resolve_publication_scope_profile",
            handler=resolve_scope,
        )
    )
    registry.register(ActionSpec(action_id="create_concepts", handler=create_concepts))
    registry.register(
        ActionSpec(
            action_id="upsert_text_relation",
            handler=lambda request: (
                calls.append(("upsert_text_relation", dict(request.inputs)))
                or WorkflowActionResult(status="success", outputs={"updated": True})
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=20).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            org_concept_id="#V#test_research_org",
        ),
        data={
            "paper_type_concept_id": "#V#paper_under_preparation",
            "title": "A Private Conference Submission",
            "paper_lifecycle_state": "under_review",
            "submission_identifier": "17045",
            "submission_venue": "NeurIPS 2026",
        },
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert result.data["paper_concept_id"] == concept_id_holder["concept_id"]
    assert result.data["paper_under_preparation_verified"] is True
    assert result.data["paper_instance_scope_mode"] == "organisation_general"
    assert [label for label, _inputs in calls] == [
        "resolve_scope",
        "create_concepts",
        "upsert_text_relation",
    ]
    assert calls[0][1]["type_concept_ids"] == ["#V#paper_under_preparation"]
    assert calls[1][1]["parent_id"] == "#V#paper_under_preparation"
    assert calls[1][1]["scope_mode"] == "organisation_general"


def test_under_preparation_workflow_does_not_mutate_when_profile_is_not_org() -> None:
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID],
    )[PAPER_UNDER_PREPARATION_REPRESENTATION_WORKFLOW_ID]
    registry = _paper_registry()
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(
            action_id="resolve_publication_scope_profile",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={
                    "selected_scope_mode": "global_general",
                    "decision_evidence": {"source": "bad_test_profile"},
                },
            ),
        )
    )
    registry.register(
        ActionSpec(
            action_id="create_concepts",
            handler=lambda _request: pytest.fail(
                "an unsupported profile must fail before mutation"
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            org_concept_id="#V#test_research_org",
        ),
        data={
            "paper_type_concept_id": "#V#paper_under_preparation",
            "title": "A Private Conference Submission",
        },
    )

    assert result.completed is False
    assert result.error == "paper_under_preparation_organisation_scope_required"


def test_public_title_resolution_selects_one_exact_arxiv_match() -> None:
    registry = _paper_registry()
    environment = WorkflowEnvironment(llm_client=None)

    prepared = registry.execute(
        PAPER_REFERENCE_PREPARE_PUBLIC_TITLE_SEARCH_ACTION_ID,
        inputs={
            "paper_metadata": {
                "title": "Attention Is All You Need",
                "authors": ["Ashish Vaswani"],
            }
        },
        context={},
        env=environment,
    )
    assert prepared.status == "success"
    assert prepared.outputs["public_paper_title_query"] == (
        "Attention Is All You Need"
    )

    selected = registry.execute(
        PAPER_REFERENCE_SELECT_ARXIV_TITLE_MATCH_ACTION_ID,
        inputs={
            "title": "Attention Is All You Need",
            "author_names": ["Ashish Vaswani"],
            "search_payload": {
                "results": [
                    {
                        "id": "http://arxiv.org/abs/1706.03762v7",
                        "title": "Attention Is All You Need",
                        "authors": ["Ashish Vaswani", "Noam Shazeer"],
                        "summary": "A sequence transduction architecture.",
                        "published": "2017-06-12T00:00:00Z",
                        "categories": ["cs.CL", "cs.LG"],
                    },
                    {
                        "id": "http://arxiv.org/abs/9999.00001",
                        "title": "Attention is almost all you need",
                    },
                ]
            },
        },
        context={},
        env=environment,
    )

    assert selected.status == "success"
    assert selected.outputs["arxiv_id"] == "1706.03762"
    assert selected.outputs["source_uri"] == (
        "https://arxiv.org/abs/1706.03762"
    )
    assert selected.outputs["paper_metadata"]["authors"] == [
        "Ashish Vaswani",
        "Noam Shazeer",
    ]
    assert selected.outputs["public_paper_resolution_status"] == (
        "exact_public_title_match"
    )


@pytest.mark.parametrize(
    ("paper_metadata", "expected_sufficient", "expected_basis"),
    (
        (
            {"title": "A Bare Paper Title"},
            False,
            "insufficient",
        ),
        (
            {
                "title": "A Bibliographically Identified Paper",
                "authors": ["Ada Lovelace"],
                "publication_date": "1843",
            },
            True,
            "title_publication_date_authors",
        ),
        (
            {
                "title": "A DOI Identified Paper",
                "doi": "10.1000/example",
            },
            True,
            "doi",
        ),
        (
            {
                "title": "A Source Identified Paper",
                "source_url": "https://example.org/papers/identified",
            },
            True,
            "source_uri",
        ),
    ),
)
def test_prepare_public_title_search_reports_bibliographic_fallback_evidence(
    paper_metadata: dict[str, object],
    expected_sufficient: bool,
    expected_basis: str,
) -> None:
    prepared = _paper_registry().execute(
        PAPER_REFERENCE_PREPARE_PUBLIC_TITLE_SEARCH_ACTION_ID,
        inputs={"paper_metadata": paper_metadata},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert prepared.status == "success"
    assert prepared.outputs["bibliographic_fallback_sufficient"] is expected_sufficient
    assert prepared.outputs["bibliographic_fallback_basis"] == expected_basis
    assert prepared.outputs["public_paper_title_search_required"] is (
        expected_basis not in {"doi", "source_uri"}
    )


def test_public_title_resolution_fails_closed_on_ambiguous_exact_matches() -> None:
    selected = _paper_registry().execute(
        PAPER_REFERENCE_SELECT_ARXIV_TITLE_MATCH_ACTION_ID,
        inputs={
            "title": "A Shared Title",
            "search_payload": {
                "results": [
                    {"id": "2601.00001", "title": "A Shared Title"},
                    {"id": "2602.00002", "title": "A Shared Title"},
                ]
            },
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert selected.status == "failed"
    assert selected.error == "public_paper_title_match_ambiguous"
    assert selected.outputs["public_paper_exact_match_count"] == 2


def test_public_title_resolution_workflow_searches_then_represents_exact_match() -> (
    None
):
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID],
    )[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID]
    calls: list[str] = []

    def invoke_tool(request):
        calls.append(str(request.inputs["tool_name"]))
        assert request.inputs["tool_arguments"]["query"] == (
            "Attention Is All You Need"
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "results": [
                        {
                            "id": "http://arxiv.org/abs/1706.03762",
                            "title": "Attention Is All You Need",
                            "authors": ["Ashish Vaswani"],
                            "published": "2017-06-12",
                        }
                    ]
                }
            },
        )

    def invoke_subworkflow(request):
        calls.append(str(request.workflow_state_id))
        assert request.inputs["arxiv_id"] == "1706.03762"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_concept_id": "#V#public_arxiv_1706_03762",
                    "file_copy_concept_id": "#V#local_arxiv_1706_03762",
                    "article_readback": {
                        "concept_id": "#V#public_arxiv_1706_03762",
                    },
                }
            },
        )

    registry = _paper_registry()
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=invoke_tool)
    )
    registry.register(
        ActionSpec(action_id="workflow_invoke_subworkflow", handler=invoke_subworkflow)
    )
    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"paper_metadata": {"title": "Attention Is All You Need"}},
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert calls == ["search_arxiv", "ingest_resolved_arxiv"]
    assert result.data["paper_concept_id"] == "#V#public_arxiv_1706_03762"
    assert result.data["public_paper_resolution_status"] == (
        "exact_public_title_match"
    )


def test_public_title_resolution_workflow_rejects_title_only_after_public_miss() -> (
    None
):
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID],
    )[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID]
    registry = _paper_registry()
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(
            action_id="workflow_mcp.invoke_tool",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={"result": {"results": []}},
            ),
        )
    )
    registry.register(
        ActionSpec(
            action_id="workflow_invoke_subworkflow",
            handler=lambda _request: pytest.fail(
                "title-only evidence must not reach a mutating fallback"
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"paper_metadata": {"title": "A Bare Paper Title"}},
    )

    assert result.completed is False
    assert result.error == "public_paper_stable_identity_not_resolved"
    assert result.data["last_action_outputs"]["paper_reference_item_status"] == (
        "failed"
    )
    assert result.data["last_action_outputs"]["paper_reference_error_code"] == (
        "public_paper_stable_identity_not_resolved"
    )


def test_public_title_resolution_workflow_uses_sufficient_bibliographic_fallback() -> (
    None
):
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID],
    )[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID]
    calls: list[str] = []

    def invoke_tool(_request):
        calls.append("search_arxiv")
        return WorkflowActionResult(
            status="success",
            outputs={"result": {"results": []}},
        )

    def invoke_metadata(request):
        calls.append(str(request.workflow_state_id))
        assert request.inputs["paper_metadata"]["authors"] == ["Ada Lovelace"]
        assert request.inputs["paper_metadata"]["publication_date"] == "1843"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_concept_id": "#V#public_bibliographic_example",
                    "article_readback": {
                        "concept_id": "#V#public_bibliographic_example"
                    },
                }
            },
        )

    registry = _paper_registry()
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=invoke_tool)
    )
    registry.register(
        ActionSpec(action_id="workflow_invoke_subworkflow", handler=invoke_metadata)
    )
    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "paper_metadata": {
                "title": "A Bibliographically Identified Paper",
                "authors": ["Ada Lovelace"],
                "publication_date": "1843",
            }
        },
    )

    assert result.completed is True
    assert calls == ["search_arxiv", "ingest_metadata_fallback"]
    assert result.data["bibliographic_fallback_basis"] == (
        "title_publication_date_authors"
    )
    assert result.data["paper_concept_id"] == "#V#public_bibliographic_example"


def test_public_title_resolution_workflow_preserves_stronger_doi_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    monkeypatch.setattr(mod, "_get_concept", lambda _concept_id: None)
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID],
    )[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID]
    registry = _paper_registry()
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(
            action_id="resolve_publication_scope_profile",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={
                    "selected_scope_mode": "global_general",
                    "decision_evidence": {"source": "represented_type_profile"},
                },
            ),
        )
    )

    def resolve_doi(request):
        assert request.inputs["tool_name"] == "get_doi_metadata"
        assert request.inputs["tool_arguments"]["doi"] == "10.1000/example"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "doi": "10.1000/example",
                    "source_uri": "https://doi.org/10.1000/example",
                    "title": "A Paper With A DOI",
                    "author_names": ["Ada Lovelace"],
                    "publication_date": "1843",
                    "paper_metadata": {
                        "title": "A Paper With A DOI",
                        "doi": "10.1000/example",
                        "source_uri": "https://doi.org/10.1000/example",
                        "authors": ["Ada Lovelace"],
                        "publication_date": "1843",
                    },
                }
            },
        )

    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=resolve_doi)
    )

    def invoke_metadata(request):
        assert request.workflow_state_id == "ingest_metadata_fallback"
        assert request.inputs["paper_metadata"]["doi"] == "10.1000/example"
        assert request.inputs["doi"] == "10.1000/example"
        assert request.inputs["source_uri"] == (
            "https://doi.org/10.1000/example"
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_concept_id": "#V#public_doi_10_1000_example",
                    "article_readback": {
                        "concept_id": "#V#public_doi_10_1000_example"
                    },
                }
            },
        )

    registry.register(
        ActionSpec(
            action_id="workflow_invoke_subworkflow",
            handler=invoke_metadata,
        )
    )
    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "doi": "10.1000/example",
            "paper_metadata": {
                "title": "A Paper With A DOI",
                "doi": "10.1000/example",
            },
        },
    )

    assert result.completed is True
    assert result.data["paper_concept_id"] == "#V#public_doi_10_1000_example"


def test_public_title_resolution_workflow_reuses_complete_exact_doi_before_crossref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    existing_paper_id = "#V#external_identity_doi_existing_complete"
    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda _concept_id: {
            "name": "An Existing Complete DOI Paper",
            "relationships": {
                "is_an_instance_of": ["#V#scholarly_article"],
                "#V#authored_by": ["#V#public_author"],
            },
        },
    )
    monkeypatch.setattr(mod, "_require_global_targets", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda _concept_id: mod.PublicationContext.global_context(),
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda **kwargs: (
            [{"text": "An Existing Complete DOI Paper"}]
            if kwargs.get("predicate") == "hasName"
            else []
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service.canonical_concept_id_for_external_identifiers",
        lambda *_args, **_kwargs: existing_paper_id,
    )

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID],
    )[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID]
    registry = _paper_registry()
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(
            action_id="resolve_publication_scope_profile",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={
                    "selected_scope_mode": "global_general",
                    "decision_evidence": {"source": "represented_type_profile"},
                },
            ),
        )
    )
    registry.register(
        ActionSpec(
            action_id="workflow_mcp.invoke_tool",
            handler=lambda _request: pytest.fail(
                "a complete existing exact DOI must be reused before Crossref"
            ),
        )
    )
    registry.register(
        ActionSpec(
            action_id="workflow_invoke_subworkflow",
            handler=lambda _request: pytest.fail(
                "exact DOI reuse must not rematerialise the existing paper"
            ),
        )
    )
    registry.register(
        ActionSpec(
            action_id="fetch_concept_content",
            handler=lambda request: WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "concept_id": request.inputs["concept_id"],
                        "name": "An Existing Complete DOI Paper",
                    }
                },
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"doi": "10.5555/existing-complete"},
    )

    assert result.completed is True, result.error
    assert result.data["paper_external_identity_resolution_status"] == "resolved"
    assert result.data["scholarly_representation_verified"] is True
    assert result.data["paper_concept_id"] == existing_paper_id
    assert result.data["article_readback"]["concept_id"] == existing_paper_id


def test_public_title_resolution_workflow_extracts_exact_public_source() -> None:
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID],
    )[PUBLIC_PAPER_TITLE_RESOLUTION_WORKFLOW_ID]
    registry = _paper_registry()
    register_control_flow_actions(registry)

    def extract_source(request):
        assert request.inputs["tool_name"] == "extract_url"
        assert request.inputs["tool_arguments"]["url"] == (
            "https://example.org/public-paper.pdf"
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "content": "Public Paper Title\nAda Lovelace\nA public abstract.",
                    "title": "public-paper.pdf",
                    "url": "https://example.org/public-paper.pdf",
                }
            },
        )

    def invoke_metadata(request):
        assert request.workflow_state_id == "ingest_metadata_fallback"
        assert request.inputs["source_uri"] == (
            "https://example.org/public-paper.pdf"
        )
        assert "Public Paper Title" in request.inputs["prompt"]
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_concept_id": "#V#public_paper_from_source",
                    "article_readback": {
                        "concept_id": "#V#public_paper_from_source"
                    },
                }
            },
        )

    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=extract_source)
    )
    registry.register(
        ActionSpec(
            action_id="workflow_invoke_subworkflow",
            handler=invoke_metadata,
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"source_uri": "https://example.org/public-paper.pdf"},
    )

    assert result.completed is True
    assert result.data["paper_concept_id"] == "#V#public_paper_from_source"


@pytest.mark.parametrize(
    "paper_context",
    (
        PublicationContext.global_context(),
        PublicationContext.organisation("#V#paper_workflow_organisation"),
    ),
)
def test_ensure_arxiv_paper_rejects_existing_shared_target_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    paper_context: PublicationContext,
) -> None:
    from src.backend.services import arxiv_paper_link_service
    from src.backend.workflows.durable import paper_representation_workflow as mod

    predicted_paper_id = "#V#paper_on_arxiv_existing_shared"
    mutation_calls: list[str] = []
    monkeypatch.setattr(mod, "_concept_exists", lambda _concept_id: True)
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda concept_id: (
            paper_context
            if concept_id == predicted_paper_id
            else PublicationContext.global_context()
        ),
    )
    monkeypatch.setattr(
        arxiv_paper_link_service,
        "resolve_actor_private_arxiv_paper_concept_id",
        lambda *, user_concept_id, arxiv_id: predicted_paper_id,
    )
    monkeypatch.setattr(
        arxiv_paper_link_service,
        "ensure_arxiv_paper_instance",
        lambda **_kwargs: mutation_calls.append("ensure") or predicted_paper_id,
    )
    monkeypatch.setattr(
        mod,
        "add_structural_relationship",
        lambda **_kwargs: mutation_calls.append("type") or {"success": True},
    )

    result = _paper_registry().execute(
        SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID,
        inputs={"arxiv_id": "2608.00001"},
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id=_ACTOR_CONCEPT_ID,
        ),
    )

    assert result.status == "failed"
    assert result.error == "scholarly_paper_actor_private_target_required"
    assert result.outputs["mutation_target_concept_id"] == predicted_paper_id
    assert mutation_calls == []


def test_link_file_copy_rejects_non_global_paper_before_writing_scoped_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import arxiv_paper_link_service
    from src.backend.workflows.durable import paper_representation_workflow as mod

    paper_concept_id = "#V#actor_private_paper"
    file_copy_concept_id = "#V#shared_file_copy"
    link_calls: list[tuple[str, str]] = []

    def _publication_context(concept_id: str) -> PublicationContext:
        if concept_id == paper_concept_id:
            return PublicationContext.user(_ACTOR_CONCEPT_ID)
        return PublicationContext.organisation("#V#paper_workflow_organisation")

    monkeypatch.setattr(mod, "concept_publication_context", _publication_context)
    monkeypatch.setattr(
        arxiv_paper_link_service,
        "_link_file_copy_to_paper_concept",
        lambda **kwargs: link_calls.append(
            (kwargs["paper_concept_id"], kwargs["file_copy_concept_id"])
        ),
    )

    result = _paper_registry().execute(
        SCHOLARLY_PAPER_LINK_FILE_COPY_ACTION_ID,
        inputs={
            "paper_concept_id": paper_concept_id,
            "file_copy_concept_id": file_copy_concept_id,
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id=_ACTOR_CONCEPT_ID,
        ),
    )

    assert result.status == "failed"
    assert result.error == "scholarly_paper_global_target_required"
    assert result.outputs["mutation_target_concept_id"] == paper_concept_id
    assert link_calls == []


def test_sessionless_custom_mutation_ignores_payload_actor_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    text_writes: list[dict[str, object]] = []
    monkeypatch.setattr(
        mod,
        "upsert_text_for_concept",
        lambda **kwargs: text_writes.append(dict(kwargs)),
    )

    result = _paper_registry().execute(
        SCHOLARLY_PAPER_ENRICH_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#other_users_private_paper",
            "title": "Untrusted rewrite",
            "user_concept_id": _ACTOR_CONCEPT_ID,
        },
        context={"user_concept_id": _ACTOR_CONCEPT_ID},
        env=WorkflowEnvironment(llm_client=None, user_concept_id=None),
    )

    assert result.status == "failed"
    assert result.error == "scholarly_paper_mutation_target_missing"
    assert text_writes == []


def test_missing_schema_support_fails_without_on_demand_global_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import arxiv_paper_link_service
    from src.backend.workflows.durable import paper_representation_workflow as mod

    mutation_calls: list[str] = []
    predicted_paper_id = "#V#paper_on_arxiv_not_yet_created"

    def _concept_exists(concept_id: str) -> bool:
        return concept_id == "#V#paper_on_arxiv"

    monkeypatch.setattr(mod, "_concept_exists", _concept_exists)
    monkeypatch.setattr(
        arxiv_paper_link_service,
        "resolve_actor_private_arxiv_paper_concept_id",
        lambda *, user_concept_id, arxiv_id: predicted_paper_id,
    )
    monkeypatch.setattr(
        arxiv_paper_link_service,
        "ensure_arxiv_paper_instance",
        lambda **_kwargs: mutation_calls.append("ensure") or predicted_paper_id,
    )
    monkeypatch.setattr(
        mod.concept_service,
        "create_concept",
        lambda **_kwargs: mutation_calls.append("create"),
    )
    monkeypatch.setattr(
        mod,
        "add_structural_relationship",
        lambda **_kwargs: mutation_calls.append("type") or {"success": True},
    )

    result = _paper_registry().execute(
        SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID,
        inputs={"arxiv_id": "2608.00002"},
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id=_ACTOR_CONCEPT_ID,
        ),
    )

    assert result.status == "failed"
    assert result.error == "scholarly_paper_schema_support_missing"
    assert result.outputs["missing_schema_concept_ids"] == ["#V#scholarly_article"]
    assert mutation_calls == []


def test_guarded_materialisation_skips_inner_on_demand_schema_ensure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import arxiv_paper_link_service
    from src.backend.workflows.durable import paper_representation_workflow as mod

    file_copy_concept_id = "#V#actor_private_materialisation_file"
    paper_concept_id = "#V#actor_private_materialisation_paper"
    schema_ids = {
        "#V#paper_on_arxiv",
        "#V#scholarly_article",
        "#V#person",
        "#V#research_topic",
        "#V#authored_by",
        "#V#about",
    }
    materialisation_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod, "_concept_exists", lambda concept_id: concept_id in schema_ids
    )
    monkeypatch.setattr(
        mod,
        "validate_predicate_concept",
        lambda _concept_id: (True, None, None),
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda concept_id: (
            PublicationContext.user(_ACTOR_CONCEPT_ID)
            if concept_id == file_copy_concept_id
            else PublicationContext.global_context()
        ),
    )
    monkeypatch.setattr(
        arxiv_paper_link_service,
        "resolve_actor_private_arxiv_paper_concept_id",
        lambda **_kwargs: paper_concept_id,
    )

    def _materialise(**kwargs):
        materialisation_calls.append(dict(kwargs))
        return {
            "success": True,
            "paper_concept_id": paper_concept_id,
            "file_copy_concept_id": file_copy_concept_id,
        }

    monkeypatch.setattr(
        mod,
        "materialise_scholarly_representation_for_arxiv_file_copy",
        _materialise,
    )

    result = _paper_registry().execute(
        SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
        inputs={
            "arxiv_id": "2608.00003",
            "file_copy_concept_id": file_copy_concept_id,
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id=_ACTOR_CONCEPT_ID,
        ),
    )

    assert result.status == "success"
    assert materialisation_calls[0]["schema_support_preprovisioned"] is True


def test_resolve_authors_uses_scope_profiles_without_mutating_concepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    paper_concept_id = "#V#global_scope_profile_paper"

    monkeypatch.setattr(
        mod,
        "_require_preprovisioned_schema_support",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "_require_global_targets",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "resolve_publication_scope_profile",
        lambda **kwargs: {
            "success": True,
            "plane": kwargs["plane"],
            "selected_scope_mode": "global_general",
        },
    )
    result = _paper_registry().execute(
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        inputs={
            "paper_concept_id": paper_concept_id,
            "author_names": ["Public Author"],
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id=_ACTOR_CONCEPT_ID,
        ),
    )

    assert result.status == "success"
    assert result.outputs["author_instance_scope_mode"] == "global_general"
    assert result.outputs["authorship_assertion_scope_mode"] == "global_general"
    assert result.outputs["public_author_count"] == 1
    author_record = result.outputs["public_author_records"][0]
    assert author_record["name"] == "Public Author"
    assert author_record["identity_scheme"] == "scholarly-author-occurrence"
    assert author_record["concept_id"].startswith(
        "#V#external_identity_scholarly_author_occurrence_"
    )


def test_resolve_authors_reuses_a_public_identifier_across_papers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    monkeypatch.setattr(
        mod,
        "_require_preprovisioned_schema_support",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "_require_global_targets",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "resolve_publication_scope_profile",
        lambda **kwargs: {
            "success": True,
            "plane": kwargs["plane"],
            "selected_scope_mode": "global_general",
        },
    )
    registry = _paper_registry()
    author = {
        "name": "Publicly Identified Author",
        "orcid": "https://orcid.org/0000-0002-1825-0097",
    }

    first = registry.execute(
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#public_paper_one",
            "author_records": [author],
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )
    second = registry.execute(
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#public_paper_two",
            "author_records": [author],
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert first.status == "success"
    assert second.status == "success"
    first_record = first.outputs["public_author_records"][0]
    second_record = second.outputs["public_author_records"][0]
    assert first_record["identity_kind"] == "public_identifier"
    assert first_record["identity_scheme"] == "orcid"
    assert first_record["concept_id"] == second_record["concept_id"]


def test_normalise_inputs_extracts_arxiv_id_from_prompt_context() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    result = registry.execute(
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        inputs={},
        context={
            "prompt": "Please represent https://arxiv.org/abs/2602.20478",
            "file_copy_concept_id": "#V#file_copy_2602_20478",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["arxiv_id"] == "2602.20478"
    assert result.outputs["verification_profile"] == "arxiv"
    assert result.outputs["file_copy_concept_id"] == "#V#file_copy_2602_20478"


def test_normalise_inputs_extracts_publication_date_from_metadata() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    result = registry.execute(
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        inputs={},
        context={
            "file_copy_concept_id": "#V#file_copy_2603_21702",
            "paper_metadata": {
                "title": "The Geometry of Next-Token Prediction",
                "published": "2026/03/23",
            },
            "source_uri": "https://arxiv.org/abs/2603.21702",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["publication_date"] == "2026-03-23"


def test_arxiv_decide_acquisition_mode_prefers_existing_file_copy(monkeypatch) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod, "_resolve_user_concept_id", lambda _request: "#V#user_test"
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.inspect_cached_arxiv_artifacts",
        lambda *, arxiv_id, storage_path=None: {
            "schema_version": "arxiv_cache_state.v1",
            "arxiv_id": arxiv_id,
            "cache_root": "C:/tmp/arxiv_cache",
            "cache_root_exists": True,
            "cache_state": "cache_miss",
            "has_cached_pdf": False,
            "cached_pdf_path": None,
            "has_cached_markdown": False,
            "cached_markdown_path": None,
            "partial_cache_without_pdf": False,
        },
    )
    result = registry.execute(
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        inputs={
            "arxiv_id": "2602.20478",
            "file_copy_concept_id": "#V#existing_file_copy",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["result"] is False
    assert result.outputs["acquisition_required"] is False
    assert (
        result.outputs["acquisition_mode"] == ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY
    )


def test_arxiv_decide_acquisition_mode_prefers_finalise_for_cached_pdf(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod, "_resolve_user_concept_id", lambda _request: "#V#user_test"
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.inspect_cached_arxiv_artifacts",
        lambda *, arxiv_id, storage_path=None: {
            "schema_version": "arxiv_cache_state.v1",
            "arxiv_id": arxiv_id,
            "cache_root": "C:/tmp/arxiv_cache",
            "cache_root_exists": True,
            "cache_state": "cached_pdf_available",
            "has_cached_pdf": True,
            "cached_pdf_path": "C:/tmp/arxiv_cache/2603.14482.pdf",
            "has_cached_markdown": True,
            "cached_markdown_path": "C:/tmp/arxiv_cache/2603.14482.md",
            "partial_cache_without_pdf": False,
        },
    )

    result = registry.execute(
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        inputs={"arxiv_id": "2603.14482"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["result"] is True
    assert result.outputs["acquisition_required"] is True
    assert (
        result.outputs["acquisition_mode"] == ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF
    )
    assert result.outputs["cache_state"] == "cached_pdf_available"
    assert result.outputs["cached_pdf_path"] == "C:/tmp/arxiv_cache/2603.14482.pdf"
    assert result.outputs["partial_cache_without_pdf"] is False


def test_arxiv_decide_acquisition_mode_marks_partial_cache_for_reacquisition(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod, "_resolve_user_concept_id", lambda _request: "#V#user_test"
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.inspect_cached_arxiv_artifacts",
        lambda *, arxiv_id, storage_path=None: {
            "schema_version": "arxiv_cache_state.v1",
            "arxiv_id": arxiv_id,
            "cache_root": "C:/tmp/arxiv_cache",
            "cache_root_exists": True,
            "cache_state": "markdown_only_partial_cache",
            "has_cached_pdf": False,
            "cached_pdf_path": None,
            "has_cached_markdown": True,
            "cached_markdown_path": "C:/tmp/arxiv_cache/2603.14482.md",
            "partial_cache_without_pdf": True,
        },
    )

    result = registry.execute(
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        inputs={"arxiv_id": "2603.14482"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["result"] is True
    assert result.outputs["acquisition_required"] is True
    assert (
        result.outputs["acquisition_mode"]
        == ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE
    )
    assert result.outputs["cache_state"] == "markdown_only_partial_cache"
    assert result.outputs["cached_markdown_path"] == "C:/tmp/arxiv_cache/2603.14482.md"
    assert result.outputs["partial_cache_without_pdf"] is True


def test_arxiv_decide_acquisition_mode_falls_back_to_download_without_cache(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod, "_resolve_user_concept_id", lambda _request: "#V#user_test"
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.inspect_cached_arxiv_artifacts",
        lambda *, arxiv_id, storage_path=None: {
            "schema_version": "arxiv_cache_state.v1",
            "arxiv_id": arxiv_id,
            "cache_root": "C:/tmp/arxiv_cache",
            "cache_root_exists": True,
            "cache_state": "cache_miss",
            "has_cached_pdf": False,
            "cached_pdf_path": None,
            "has_cached_markdown": False,
            "cached_markdown_path": None,
            "partial_cache_without_pdf": False,
        },
    )

    result = registry.execute(
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        inputs={"arxiv_id": "2603.14482"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert (
        result.outputs["acquisition_mode"]
        == ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE
    )
    assert result.outputs["partial_cache_without_pdf"] is False


def test_verify_representation_requires_publication_date_for_arxiv_profile(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda _concept_id: {
            "relationships": {
                "is_an_instance_of": ["#V#scholarly_article"],
                "#V#propositional_information_thing_has_computer_file": [
                    "#V#file_copy_2603_21702"
                ],
                "#V#authored_by": ["#V#author_1"],
                "#V#about": ["#V#topic_1"],
            },
            "attributes": {"arxiv_id": "2603.21702"},
        },
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda _concept_id: mod.PublicationContext.global_context(),
    )

    def _fake_get_texts_for_concept(*, predicate: str, **_kwargs):
        if predicate == "hasName":
            return [
                {"text": "The Geometry of Next-Token Prediction"},
                {"text": "2603.21702"},
                {"text": "https://arxiv.org/abs/2603.21702"},
            ]
        if predicate == "hasDescription":
            return [{"text": "Abstract text."}]
        if predicate == "#V#has_publication_date":
            return []
        if predicate == "#V#has_topic_labels":
            return [{"text": "cs.AI"}]
        return []

    monkeypatch.setattr(mod, "get_texts_for_concept", _fake_get_texts_for_concept)

    result = registry.execute(
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#paper_on_arxiv_2603_21702",
            "file_copy_concept_id": "#V#file_copy_2603_21702",
            "arxiv_id": "2603.21702",
            "publication_date": "2026-03-23",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["scholarly_representation_verified"] is False
    assert "publication_date_missing" in list(
        result.outputs["verification_failures"] or []
    )


def test_verify_representation_allows_missing_file_copy_when_not_required(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda _concept_id: {
            "relationships": {
                "is_an_instance_of": ["#V#scholarly_article"],
                "#V#authored_by": ["#V#author_1"],
                "#V#about": ["#V#topic_1"],
            },
            "attributes": {"arxiv_id": "2505.17801"},
            "name": "Integrating Counterfactual Simulations",
        },
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda _concept_id: mod.PublicationContext.global_context(),
    )

    def _fake_get_texts_for_concept(*, predicate: str, **_kwargs):
        if predicate == "hasName":
            return [
                {
                    "text": (
                        "Integrating Counterfactual Simulations with Language "
                        "Models for Explaining Multi-Agent Behaviour"
                    )
                },
                {"text": "2505.17801"},
                {"text": "https://arxiv.org/abs/2505.17801"},
            ]
        if predicate == "hasDescription":
            return [{"text": "Abstract text."}]
        if predicate == "#V#has_publication_date":
            return [{"text": "2025-05-23"}]
        if predicate == "#V#has_topic_labels":
            return [{"text": "cs.AI"}]
        return []

    monkeypatch.setattr(mod, "get_texts_for_concept", _fake_get_texts_for_concept)

    strict_result = registry.execute(
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#paper_on_arxiv_2505_17801",
            "arxiv_id": "2505.17801",
            "publication_date": "2025-05-23",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )
    optional_result = registry.execute(
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#paper_on_arxiv_2505_17801",
            "arxiv_id": "2505.17801",
            "publication_date": "2025-05-23",
            "require_file_copy": False,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert strict_result.status == "success"
    assert strict_result.outputs["scholarly_representation_verified"] is False
    assert "file_copy_concept_missing" in list(
        strict_result.outputs["verification_failures"] or []
    )
    assert optional_result.status == "success"
    assert optional_result.outputs["require_file_copy"] is False
    assert optional_result.outputs["scholarly_representation_verified"] is True
    assert optional_result.outputs["verification_failures"] == []
