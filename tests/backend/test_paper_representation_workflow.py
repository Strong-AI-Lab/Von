from __future__ import annotations

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.paper_representation_workflow import (
    ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
    ARXIV_ACQUISITION_MODE_DOWNLOAD_FROM_SOURCE,
    ARXIV_ACQUISITION_MODE_EXISTING_FILE_COPY,
    ARXIV_ACQUISITION_MODE_FINALISE_CACHED_PDF,
    ARXIV_ACQUISITION_MODE_REACQUIRE_PARTIAL_CACHE,
    ARXIV_NORMALISE_SOURCE_ACTION_ID,
    SCHOLARLY_PAPER_ENRICH_ACTION_ID,
    SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
    SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
    SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
    SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    register_paper_representation_actions,
)


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
