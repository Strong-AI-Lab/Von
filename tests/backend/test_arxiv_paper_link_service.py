from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _mock_db_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")


def test_link_file_copy_to_arxiv_paper_creates_stable_paper_instance_and_links(
    monkeypatch,
):
    from src.backend.security import access_control

    monkeypatch.setattr(
        access_control,
        "get_effective_user_concept_id",
        lambda: "#V#user_test",
    )

    from src.backend.services.computer_file_copy_service import (
        create_computer_file_copy_instance,
    )
    from src.backend.services.arxiv_paper_link_service import (
        link_file_copy_to_arxiv_paper,
    )
    from src.backend.db.repositories.concepts_repository import ConceptsRepository

    record = create_computer_file_copy_instance(
        type_concept_id="#V#arxiv_pdf_file",
        user_concept_id="#V#user_test",
        name="2505.12477.pdf",
        sha256="deadbeef",
        size_bytes=123,
        content_type="application/pdf",
        blob_backend="local",
        blob_key="arxiv/papers/2505.12477.pdf",
        blob_uri="local://arxiv/papers/2505.12477.pdf",
        metadata={"source": "arxiv"},
    )

    link = link_file_copy_to_arxiv_paper(
        user_concept_id="#V#user_test",
        arxiv_id="2505.12477",
        file_copy_concept_id=record.concept_id,
    )

    assert link["linked"] is True
    assert isinstance(link.get("paper_concept_id"), str)
    paper_id = link["paper_concept_id"]

    file_doc = ConceptsRepository.find_one({"concept_id": record.concept_id})
    paper_doc = ConceptsRepository.find_one({"concept_id": paper_id})

    assert file_doc is not None
    assert paper_doc is not None

    assert paper_doc.get("relationships", {}).get("is_an_instance_of") == [
        "#V#paper_on_arxiv"
    ]

    assert paper_id in (file_doc.get("relationships", {}) or {}).get("related_to", [])
    assert record.concept_id in (paper_doc.get("relationships", {}) or {}).get(
        "related_to", []
    )

    assert paper_id in (file_doc.get("relationships", {}) or {}).get(
        "#V#computer_file_for_propositional_information_thing", []
    )
    assert record.concept_id in (paper_doc.get("relationships", {}) or {}).get(
        "#V#propositional_information_thing_has_computer_file", []
    )


def test_materialise_scholarly_representation_adds_metadata_authors_and_topics():
    from src.backend.services.computer_file_copy_service import (
        create_computer_file_copy_instance,
    )
    from src.backend.services.arxiv_paper_link_service import (
        materialise_scholarly_representation_for_arxiv_file_copy,
    )
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.text_value_service import get_texts_for_concept

    record = create_computer_file_copy_instance(
        type_concept_id="#V#arxiv_pdf_file",
        user_concept_id="#V#user_test",
        name="2502.14996.pdf",
        sha256="cafebabe",
        size_bytes=456,
        content_type="application/pdf",
        blob_backend="local",
        blob_key="uploads/user/2502.14996.pdf",
        blob_uri="local://uploads/user/2502.14996.pdf",
        metadata={"source": "upload"},
    )

    metadata = {
        "title": "Reasoning with Structured Retrieval for Scientific Documents",
        "authors": ["Jane Example", "Alan Example"],
        "summary": "A deterministic pipeline for scholarly paper representation.",
        "categories": ["cs.AI", "cs.CL"],
    }
    report = materialise_scholarly_representation_for_arxiv_file_copy(
        user_concept_id="#V#user_test",
        arxiv_id="2502.14996",
        file_copy_concept_id=record.concept_id,
        metadata=metadata,
    )

    assert report["success"] is True
    assert report["verified"] is True
    assert report["author_links_written"] >= 2
    assert report["topic_labels"] == ["cs.AI", "cs.CL"]
    assert report["type_asserted"] is True
    assert report["file_link_verified"] is True

    paper_id = report["paper_concept_id"]
    paper_doc = ConceptsRepository.find_one({"concept_id": paper_id})
    assert paper_doc is not None
    rel = paper_doc.get("relationships") or {}
    assert "#V#scholarly_article" in (rel.get("is_an_instance_of") or [])
    assert record.concept_id in (
        rel.get("#V#propositional_information_thing_has_computer_file") or []
    )
    assert len(rel.get("#V#authored_by") or []) >= 2
    assert len(rel.get("#V#about") or []) >= 1

    name_texts = get_texts_for_concept(
        subject_concept_id=paper_id,
        predicate="hasName",
        limit=200,
    )
    assert any(
        isinstance(item, dict)
        and item.get("text") == metadata["title"]
        for item in name_texts
    )
    assert any(
        isinstance(item, dict) and item.get("text") == "2502.14996"
        for item in name_texts
    )

    summary_texts = get_texts_for_concept(
        subject_concept_id=paper_id,
        predicate="hasDescription",
        limit=50,
    )
    assert any(
        isinstance(item, dict)
        and item.get("text") == metadata["summary"]
        for item in summary_texts
    )


def test_materialise_generic_scholarly_representation_for_file_copy():
    from src.backend.services.computer_file_copy_service import (
        create_computer_file_copy_instance,
    )
    from src.backend.services.arxiv_paper_link_service import (
        materialise_scholarly_representation_for_file_copy,
    )
    from src.backend.db.repositories.concepts_repository import ConceptsRepository

    record = create_computer_file_copy_instance(
        type_concept_id="#V#computer_file_copy",
        user_concept_id="#V#user_test",
        name="Evaluating_the_Inductive.pdf",
        sha256="fadedcab",
        size_bytes=512,
        content_type="application/pdf",
        blob_backend="local",
        blob_key="uploads/user/evaluating_the_inductive.pdf",
        blob_uri="local://uploads/user/evaluating_the_inductive.pdf",
        metadata={"source": "upload"},
    )

    report = materialise_scholarly_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id=record.concept_id,
        metadata={
            "title": "Evaluating the Inductive",
            "summary": "A structured account of inductive reasoning evaluation.",
        },
    )

    assert report["success"] is True
    assert report["verified"] is True
    assert report["representation_mode"] == "generic_file_copy"
    assert report["file_copy_concept_id"] == record.concept_id
    paper_id = report["paper_concept_id"]
    assert isinstance(paper_id, str) and paper_id.startswith(
        "#V#scholarly_paper_for_file_copy_"
    )

    paper_doc = ConceptsRepository.find_one({"concept_id": paper_id})
    assert paper_doc is not None
    relationships = paper_doc.get("relationships") or {}
    assert "#V#scholarly_article" in list(relationships.get("is_an_instance_of") or [])
    assert record.concept_id in list(
        relationships.get("#V#propositional_information_thing_has_computer_file") or []
    )
