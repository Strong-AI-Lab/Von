from __future__ import annotations

import pytest

from src.backend.services.ontology_publication_authority_service import (
    PublicationContext,
)
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.paper_representation_workflow import (
    ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
    ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE,
    ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY,
    ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF,
    ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE,
    ARXIV_NORMALISE_SOURCE_ACTION_ID,
    SCHOLARLY_PAPER_ENRICH_ACTION_ID,
    SCHOLARLY_PAPER_ENSURE_PAPER_CONCEPT_ACTION_ID,
    SCHOLARLY_PAPER_LINK_FILE_COPY_ACTION_ID,
    SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
    SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
    SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
    SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    register_paper_representation_actions,
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
        SCHOLARLY_PAPER_ENRICH_ACTION_ID,
        SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    }.issubset(set(registry.all_action_ids()))
    verify_spec = registry.get(SCHOLARLY_PAPER_VERIFY_ACTION_ID)
    assert verify_spec is not None
    assert verify_spec.required_tool_operation_class == "verification_read"


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
        lambda concept_id: paper_context
        if concept_id == predicted_paper_id
        else PublicationContext.global_context(),
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


def test_link_file_copy_rejects_shared_file_before_mutating_either_record(
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
    assert result.error == "scholarly_paper_actor_private_target_required"
    assert result.outputs["mutation_target_concept_id"] == file_copy_concept_id
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
    assert result.error == "scholarly_paper_actor_private_target_required"
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
    assert result.outputs["missing_schema_concept_ids"] == [
        "#V#scholarly_article"
    ]
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

    monkeypatch.setattr(mod, "_concept_exists", lambda concept_id: concept_id in schema_ids)
    monkeypatch.setattr(
        mod,
        "validate_predicate_concept",
        lambda _concept_id: (True, None, None),
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda concept_id: PublicationContext.user(_ACTOR_CONCEPT_ID)
        if concept_id == file_copy_concept_id
        else PublicationContext.global_context(),
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


def test_actor_bound_schema_preflight_uses_hard_predicate_authority_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security import access_control
    from src.backend.workflows.durable import paper_representation_workflow as mod

    paper_concept_id = "#V#actor_private_schema_preflight_paper"
    author_concept_id = "#V#actor_private_schema_preflight_author"
    relationship_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        mod,
        "_concept_exists",
        lambda concept_id: concept_id in {"#V#person", "#V#authored_by"},
    )
    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {},
        },
    )
    monkeypatch.setattr(
        mod,
        "concept_publication_context",
        lambda _concept_id: PublicationContext.user(_ACTOR_CONCEPT_ID),
    )
    monkeypatch.setattr(
        mod,
        "predict_scholarly_author_concept_id",
        lambda **_kwargs: author_concept_id,
    )
    monkeypatch.setattr(
        mod,
        "resolve_or_create_scholarly_author_concept_id",
        lambda **_kwargs: author_concept_id,
    )
    monkeypatch.setattr(
        mod,
        "add_relationship",
        lambda **kwargs: relationship_calls.append(
            (kwargs["source_id"], kwargs["predicate"], kwargs["target"])
        ),
    )

    def _find_predicate(filter_doc: dict[str, object], projection=None):
        del projection
        assert access_control._BYPASS.get() is True
        return {
            "concept_id": filter_doc["concept_id"],
            "relationships": {"is_an_instance_of": ["#V#predicate"]},
        }

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_predicate,
    )

    result = _paper_registry().execute(
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        inputs={
            "paper_concept_id": paper_concept_id,
            "author_names": ["Private Author"],
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id=_ACTOR_CONCEPT_ID,
        ),
    )

    assert result.status == "success"
    assert relationship_calls == [
        (paper_concept_id, "#V#authored_by", author_concept_id)
    ]


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

    monkeypatch.setattr(mod, "_resolve_user_concept_id", lambda _request: "#V#user_test")
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
    assert result.outputs["acquisition_mode"] == ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY


def test_arxiv_decide_acquisition_mode_prefers_finalise_for_cached_pdf(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(mod, "_resolve_user_concept_id", lambda _request: "#V#user_test")
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
    assert result.outputs["acquisition_mode"] == ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF
    assert result.outputs["cache_state"] == "cached_pdf_available"
    assert result.outputs["cached_pdf_path"] == "C:/tmp/arxiv_cache/2603.14482.pdf"
    assert result.outputs["partial_cache_without_pdf"] is False


def test_arxiv_decide_acquisition_mode_marks_partial_cache_for_reacquisition(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(mod, "_resolve_user_concept_id", lambda _request: "#V#user_test")
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

    monkeypatch.setattr(mod, "_resolve_user_concept_id", lambda _request: "#V#user_test")
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
    assert result.outputs["acquisition_mode"] == ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE
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
