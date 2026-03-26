from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from src.backend.services import arxiv_ingestion_testing_service as mod


def test_prepare_arxiv_fixture_can_reclaim_existing_artifacts(
    monkeypatch,
) -> None:
    paper_concept_id = "#V#paper_on_arxiv_2603_21702"
    file_copy_ids = ["#V#file_copy_pdf_2603_21702", "#V#file_copy_md_2603_21702"]
    author_ids = ["#V#author_one", "#V#author_two"]
    topic_ids = ["#V#topic_cs_ai"]
    existing_ids = {paper_concept_id, *file_copy_ids, *author_ids, *topic_ids}
    cleanup_calls: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "fetch_arxiv_metadata",
        lambda *_args, **_kwargs: {"id": "2603.21702v1"},
    )
    monkeypatch.setattr(mod, "extract_scholarly_metadata_title", lambda *_args: "Obscure title")
    monkeypatch.setattr(mod, "extract_scholarly_metadata_summary", lambda *_args: "Obscure summary")
    monkeypatch.setattr(
        mod,
        "extract_scholarly_metadata_publication_date",
        lambda *_args: "2026-03-25",
    )
    monkeypatch.setattr(
        mod,
        "extract_scholarly_author_names",
        lambda *_args: ["Author One", "Author Two"],
    )
    monkeypatch.setattr(mod, "extract_scholarly_topic_labels", lambda *_args: ["cs.AI"])
    monkeypatch.setattr(mod, "predict_arxiv_paper_concept_id", lambda **_kwargs: paper_concept_id)
    monkeypatch.setattr(
        mod,
        "predict_scholarly_author_concept_id",
        lambda **kwargs: author_ids[0]
        if kwargs.get("author_name") == "Author One"
        else author_ids[1],
    )
    monkeypatch.setattr(
        mod,
        "predict_scholarly_topic_concept_id",
        lambda **_kwargs: topic_ids[0],
    )
    monkeypatch.setattr(mod, "_concept_exists", lambda concept_id: concept_id in existing_ids)
    monkeypatch.setattr(
        mod,
        "_get_concept_or_none",
        lambda concept_id: (
            {
                "relationships": {
                    "#V#authored_by": list(author_ids),
                    "#V#about": list(topic_ids),
                    "#V#propositional_information_thing_has_computer_file": list(
                        file_copy_ids
                    ),
                }
            }
            if concept_id == paper_concept_id
            else None
        ),
    )

    def _fake_cleanup(**kwargs: Any) -> dict[str, Any]:
        cleanup_calls.update(kwargs)
        for concept_id in [kwargs.get("paper_concept_id"), *(kwargs.get("file_copy_concept_ids") or ())]:
            if isinstance(concept_id, str):
                existing_ids.discard(concept_id)
        return {
            "success": True,
            "cleanup_passed": True,
            "cleanup_summary": {
                "cleanup_passed": True,
                "deleted_concept_ids": [paper_concept_id, *file_copy_ids],
                "failed_deletions": [],
            },
        }

    monkeypatch.setattr(mod, "cleanup_arxiv_paper_ingestion_test_artifacts", _fake_cleanup)

    result = mod.prepare_arxiv_paper_ingestion_test_fixture(
        prompt_text="Run the ingestion test on https://arxiv.org/abs/2603.21702",
        user_concept_id="#V#user",
        repair_existing_artifacts=True,
    )

    assert result["success"] is True
    assert cleanup_calls["paper_concept_id"] == paper_concept_id
    assert cleanup_calls["file_copy_concept_ids"] == file_copy_ids
    assert cleanup_calls["preexisting_author_concept_ids"] == author_ids
    assert cleanup_calls["preexisting_topic_concept_ids"] == topic_ids
    assert result["stale_artifact_reclamation"]["reclamation_passed"] is True
    assert result["preexisting_author_concept_ids"] == author_ids
    assert result["preexisting_topic_concept_ids"] == topic_ids


def test_verify_arxiv_ingestion_result_exposes_all_cleanup_file_copy_targets(
    monkeypatch,
) -> None:
    paper_concept_id = "#V#paper_on_arxiv_2603_21702"
    file_copy_ids = ["#V#file_copy_pdf_2603_21702", "#V#file_copy_md_2603_21702"]
    author_id = "#V#author_one"
    topic_id = "#V#topic_cs_ai"

    monkeypatch.setattr(
        mod,
        "_get_concept_or_none",
        lambda concept_id: (
            {
                "relationships": {
                    "#V#authored_by": [author_id],
                    "#V#about": [topic_id],
                    "#V#propositional_information_thing_has_computer_file": list(
                        file_copy_ids
                    ),
                }
            }
            if concept_id == paper_concept_id
            else None
        ),
    )
    monkeypatch.setattr(
        mod,
        "_get_text_values",
        lambda concept_id, *, predicate, limit=50: {
            (paper_concept_id, "hasName"): ["Obscure title", "2603.21702", "https://arxiv.org/abs/2603.21702"],
            (paper_concept_id, "hasDescription"): ["Obscure summary"],
            (paper_concept_id, "#V#has_publication_date"): ["2026-03-25"],
            (author_id, "hasName"): ["Author One"],
        }.get((concept_id, predicate), []),
    )
    monkeypatch.setattr(
        mod,
        "_concept_exists",
        lambda concept_id: concept_id in {paper_concept_id, *file_copy_ids, author_id, topic_id},
    )

    result = mod.verify_arxiv_paper_ingestion_test_result(
        workflow_execution={
            "final_status": "completed",
            "outputs": {
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_ids[0],
            },
        },
        arxiv_id="2603.21702",
        source_uri="https://arxiv.org/abs/2603.21702",
        expected_title="Obscure title",
        expected_summary="Obscure summary",
        expected_publication_date="2026-03-25",
        expected_author_names=["Author One"],
        expected_author_concept_ids=[author_id],
        expected_topic_labels=["cs.AI"],
        expected_topic_concept_ids=[topic_id],
    )

    assert result["success"] is True
    assert result["verification_passed"] is True
    assert result["file_copy_concept_ids"] == file_copy_ids
    assert result["cleanup_targets"]["file_copy_concept_ids"] == file_copy_ids


def test_cleanup_arxiv_ingestion_artifacts_deletes_all_linked_file_copies(
    monkeypatch,
) -> None:
    paper_concept_id = "#V#paper_on_arxiv_2603_21702"
    file_copy_ids = ["#V#file_copy_pdf_2603_21702", "#V#file_copy_md_2603_21702"]
    author_id = "#V#author_one"
    topic_id = "#V#topic_cs_ai"
    existing_ids = {paper_concept_id, *file_copy_ids, author_id, topic_id}
    deleted_concepts: list[str] = []
    deleted_file_copies: list[str] = []

    monkeypatch.setattr(mod, "_concept_exists", lambda concept_id: concept_id in existing_ids)
    monkeypatch.setattr(
        mod.concept_service,
        "delete_concept",
        lambda concept_id: deleted_concepts.append(concept_id)
        or existing_ids.discard(concept_id)
        or True,
    )

    def _fake_delete_file_copy_blob_and_concept(*, file_copy_concept_id: str) -> dict[str, Any]:
        deleted_file_copies.append(file_copy_concept_id)
        existing_ids.discard(file_copy_concept_id)
        return {
            "success": True,
            "concept_id": file_copy_concept_id,
            "concept_deleted": True,
            "blob_deleted": True,
            "partial": False,
        }

    monkeypatch.setattr(
        mod,
        "delete_file_copy_blob_and_concept",
        _fake_delete_file_copy_blob_and_concept,
    )

    result = mod.cleanup_arxiv_paper_ingestion_test_artifacts(
        paper_concept_id=paper_concept_id,
        file_copy_concept_id=file_copy_ids[0],
        file_copy_concept_ids=file_copy_ids,
        author_concept_ids=[author_id],
        topic_concept_ids=[topic_id],
        preexisting_author_concept_ids=[author_id],
        preexisting_topic_concept_ids=[],
    )

    assert result["success"] is True
    assert result["cleanup_passed"] is True
    assert deleted_file_copies == file_copy_ids
    assert topic_id in deleted_concepts
    assert author_id not in deleted_concepts
    assert result["cleanup_summary"]["file_copy_concept_ids"] == file_copy_ids


def test_verify_arxiv_ingestion_result_matches_author_names_independent_of_id_order(
    monkeypatch,
) -> None:
    paper_concept_id = "#V#paper_on_arxiv_2603_21702"
    file_copy_id = "#V#file_copy_pdf_2603_21702"
    author_ids = ["#V#author_one", "#V#author_two"]

    monkeypatch.setattr(
        mod,
        "_get_concept_or_none",
        lambda concept_id: (
            {
                "relationships": {
                    "#V#authored_by": list(author_ids),
                    "#V#propositional_information_thing_has_computer_file": [file_copy_id],
                }
            }
            if concept_id == paper_concept_id
            else None
        ),
    )
    monkeypatch.setattr(
        mod,
        "_get_text_values",
        lambda concept_id, *, predicate, limit=50: {
            (paper_concept_id, "hasName"): ["Obscure title", "2603.21702", "https://arxiv.org/abs/2603.21702"],
            (paper_concept_id, "hasDescription"): ["Obscure summary"],
            (paper_concept_id, "#V#has_publication_date"): ["2026-03-25"],
            (author_ids[0], "hasName"): ["Author One"],
            (author_ids[1], "hasName"): ["Author Two"],
        }.get((concept_id, predicate), []),
    )
    monkeypatch.setattr(
        mod,
        "_concept_exists",
        lambda concept_id: concept_id in {paper_concept_id, file_copy_id, *author_ids},
    )

    result = mod.verify_arxiv_paper_ingestion_test_result(
        workflow_execution={
            "final_status": "completed",
            "outputs": {
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_id,
            },
        },
        arxiv_id="2603.21702",
        source_uri="https://arxiv.org/abs/2603.21702",
        expected_title="Obscure title",
        expected_summary="Obscure summary",
        expected_publication_date="2026-03-25",
        expected_author_names=["Author Two", "Author One"],
        expected_author_concept_ids=list(author_ids),
        expected_topic_labels=[],
        expected_topic_concept_ids=[],
    )

    assert result["success"] is True
    assert result["verification_passed"] is True
    assert result["metadata_verification"]["author_names_matched"] is True
    assert result["metadata_verification"]["author_name_matches"] == {
        "Author Two": True,
        "Author One": True,
    }


def test_verify_arxiv_ingestion_result_reads_canonical_texts_under_access_bypass(
    monkeypatch,
) -> None:
    paper_concept_id = "#V#paper_on_arxiv_2603_21702"
    file_copy_id = "#V#file_copy_pdf_2603_21702"
    author_id = "#V#author_one"
    bypass_active = False

    @contextmanager
    def _fake_bypass() -> Any:
        nonlocal bypass_active
        previous = bypass_active
        bypass_active = True
        try:
            yield
        finally:
            bypass_active = previous

    monkeypatch.setattr(mod, "bypass_access_control", _fake_bypass)
    monkeypatch.setattr(
        mod.concept_service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: (
            {
                "relationships": {
                    "#V#authored_by": [author_id],
                    "#V#propositional_information_thing_has_computer_file": [file_copy_id],
                }
            }
            if bypass_active and concept_id == paper_concept_id
            else None
        ),
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda subject_concept_id, predicate=None, lang=None, limit=50: (
            [
                {"text": "Obscure title"},
                {"text": "2603.21702"},
                {"text": "https://arxiv.org/abs/2603.21702"},
            ]
            if bypass_active
            and subject_concept_id == paper_concept_id
            and predicate == "hasName"
            else [{"text": "Obscure summary"}]
            if bypass_active
            and subject_concept_id == paper_concept_id
            and predicate == "hasDescription"
            else [{"text": "2026-03-25"}]
            if bypass_active
            and subject_concept_id == paper_concept_id
            and predicate == "#V#has_publication_date"
            else [{"text": "Author One"}]
            if bypass_active
            and subject_concept_id == author_id
            and predicate == "hasName"
            else []
        ),
    )
    monkeypatch.setattr(
        mod,
        "_concept_exists",
        lambda concept_id: concept_id in {paper_concept_id, file_copy_id, author_id},
    )

    result = mod.verify_arxiv_paper_ingestion_test_result(
        workflow_execution={
            "final_status": "completed",
            "outputs": {
                "paper_concept_id": paper_concept_id,
                "file_copy_concept_id": file_copy_id,
            },
        },
        arxiv_id="2603.21702",
        source_uri="https://arxiv.org/abs/2603.21702",
        expected_title="Obscure title",
        expected_summary="Obscure summary",
        expected_publication_date="2026-03-25",
        expected_author_names=["Author One"],
        expected_author_concept_ids=[author_id],
        expected_topic_labels=[],
        expected_topic_concept_ids=[],
    )

    assert result["success"] is True
    assert result["verification_passed"] is True
    assert result["metadata_verification"]["author_names_matched"] is True
