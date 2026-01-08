from __future__ import annotations


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
