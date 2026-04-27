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


def test_materialise_scholarly_representation_adds_metadata_authors_and_topics(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.services.computer_file_copy_service import (
        create_computer_file_copy_instance,
    )
    from src.backend.services import arxiv_paper_link_service as mod
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.text_value_service import get_texts_for_concept

    identity_requests: list[dict[str, object]] = []
    monkeypatch.setattr(
        mod,
        "request_identity_resolution_for_materialised_scholarly_authors",
        lambda **kwargs: identity_requests.append(kwargs)
        or {"success": True, "triggered": True, "instance_id": "wf_identity_1"},
    )

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
        "publication_date": "2025-02-21",
    }
    report = mod.materialise_scholarly_representation_for_arxiv_file_copy(
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
    assert report["identity_resolution_refresh"] == {
        "success": True,
        "triggered": True,
        "instance_id": "wf_identity_1",
    }

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

    author_ids = list(rel.get("#V#authored_by") or [])
    assert author_ids
    assert identity_requests
    assert identity_requests[0]["paper_concept_id"] == paper_id
    assert identity_requests[0]["author_concept_ids"] == author_ids
    assert identity_requests[0]["author_names"] == metadata["authors"]
    assert identity_requests[0]["trigger_source"] == (
        "materialise_scholarly_representation_for_arxiv_file_copy"
    )
    assert identity_requests[0]["user_id"] == "#V#user_test"
    for author_name, author_id in zip(metadata["authors"], author_ids, strict=False):
        author_name_texts = get_texts_for_concept(
            subject_concept_id=author_id,
            predicate="hasName",
            limit=20,
        )
        assert any(
            isinstance(item, dict) and item.get("text") == author_name
            for item in author_name_texts
        )

    topic_ids = list(rel.get("#V#about") or [])
    assert topic_ids
    for topic_label, topic_id in zip(metadata["categories"], topic_ids, strict=False):
        topic_name_texts = get_texts_for_concept(
            subject_concept_id=topic_id,
            predicate="hasName",
            limit=20,
        )
        assert any(
            isinstance(item, dict) and item.get("text") == topic_label
            for item in topic_name_texts
        )

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
    publication_date_texts = get_texts_for_concept(
        subject_concept_id=paper_id,
        predicate="#V#has_publication_date",
        limit=10,
    )
    assert any(
        isinstance(item, dict)
        and item.get("text") == metadata["publication_date"]
        for item in publication_date_texts
    )
    assert report["publication_date"] == metadata["publication_date"]
    assert report["publication_date_asserted"] is True


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
            "publication_date": "2024-09-12",
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
    assert report["publication_date"] == "2024-09-12"
    assert report["publication_date_present"] is True


def test_predict_helpers_return_stable_expected_concept_ids() -> None:
    from src.backend.services.arxiv_paper_link_service import (
        predict_arxiv_paper_concept_id,
        predict_scholarly_author_concept_id,
        predict_scholarly_topic_concept_id,
    )

    paper_id = predict_arxiv_paper_concept_id(arxiv_id="2603.21702")
    author_id = predict_scholarly_author_concept_id(
        user_concept_id="#V#user_test",
        author_name="Amit Kanujia",
    )
    topic_id = predict_scholarly_topic_concept_id(
        user_concept_id="#V#user_test",
        topic_label="cs.AI",
    )

    assert paper_id.startswith("#V#paper_on_arxiv_2603_21702_")
    assert author_id.startswith("#V#person_amit_kanujia_")
    assert topic_id.startswith("#V#research_topic_cs_ai_")


def test_identity_resolution_request_emits_event_without_python_workflow_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import identity_resolution_workflow_request_service as mod

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "launch_event_workflow",
        lambda **kwargs: captured.update(kwargs)
        or {"success": True, "triggered": True},
    )

    report = mod.request_identity_resolution_for_materialised_scholarly_authors(
        author_concept_ids=["#V#person_michael_witbrock_0880532f"],
        paper_concept_id="#V#paper_one",
        author_names=["Michael Witbrock"],
        trigger_source="test_ingest",
        user_id="#V#michael_witbrock",
    )

    assert report == {"success": True, "triggered": True}
    assert captured["event_type"] == mod.IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE
    assert captured["event_id"] == (
        "test_ingest:#V#paper_one:#V#person_michael_witbrock_0880532f"
    )
    assert "workflow_id" not in captured
    assert captured["inputs"] == {
        "trigger_source": "test_ingest",
        "paper_concept_id": "#V#paper_one",
        "author_concept_ids": ["#V#person_michael_witbrock_0880532f"],
        "candidate_concept_ids": ["#V#person_michael_witbrock_0880532f"],
        "author_names": ["Michael Witbrock"],
    }


def test_existing_author_concept_still_reasserts_name_metadata(monkeypatch) -> None:
    from src.backend.services import arxiv_paper_link_service as mod

    ensured: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "predict_scholarly_author_concept_id",
        lambda **_kwargs: "#V#person_jane_example",
    )
    monkeypatch.setattr(mod, "_concept_exists", lambda concept_id: bool(concept_id))
    monkeypatch.setattr(
        mod,
        "_ensure_name_text_relation",
        lambda **kwargs: ensured.update(kwargs),
    )

    concept_id = mod.resolve_or_create_scholarly_author_concept_id(
        user_concept_id="#V#user_test",
        author_name="Jane Example",
    )

    assert concept_id == "#V#person_jane_example"
    assert ensured == {
        "concept_id": "#V#person_jane_example",
        "text": "Jane Example",
        "source": "arxiv_author_metadata",
        "logger": None,
    }


def test_existing_topic_concept_still_reasserts_name_metadata(monkeypatch) -> None:
    from src.backend.services import arxiv_paper_link_service as mod

    ensured: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "predict_scholarly_topic_concept_id",
        lambda **_kwargs: "#V#research_topic_cs_ai",
    )
    monkeypatch.setattr(mod, "_concept_exists", lambda concept_id: bool(concept_id))
    monkeypatch.setattr(
        mod,
        "_ensure_name_text_relation",
        lambda **kwargs: ensured.update(kwargs),
    )

    concept_id = mod._resolve_or_create_topic_concept_id(
        user_concept_id="#V#user_test",
        topic_label="cs.AI",
    )

    assert concept_id == "#V#research_topic_cs_ai"
    assert ensured == {
        "concept_id": "#V#research_topic_cs_ai",
        "text": "cs.AI",
        "source": "arxiv_topic_metadata",
        "logger": None,
    }


def test_exact_existence_checks_do_not_use_recursive_concept_resolution(
    monkeypatch,
) -> None:
    from src.backend.services import arxiv_paper_link_service as mod
    from src.backend.services import concept_service

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("arXiv link helpers should use exact concept lookup")
        ),
    )

    def _fake_exact_lookup(concept_id: str):
        if concept_id == "#V#paper_on_arxiv":
            return {"concept_id": concept_id}
        raise concept_service.ConceptNotFoundError("missing")

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        _fake_exact_lookup,
    )

    assert mod._concept_exists("#V#paper_on_arxiv") is True
    assert mod._concept_exists("#V#paper_on_arxiv_missing") is False
