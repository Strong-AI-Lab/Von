from __future__ import annotations

from typing import Any

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
    """The request service must route through ``launch_event_workflow``.

    Routing decisions for ``identity_resolution.requested`` belong to the
    persistent ``EventWorkflowBinding`` registry (see JVNAUTOSCI-2150 phase 1),
    so the request service is allowed to know only the *event type* — not
    which workflow handles it.  This test pins that contract: it stubs
    ``launch_event_workflow`` to capture its arguments and asserts the
    request service hands the launcher the event-shaped contract and never
    selects a workflow ID itself.
    """

    from src.backend.services import identity_resolution_workflow_request_service as mod

    captured: dict[str, object] = {}

    def _fake_launch(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "success": True,
            "triggered": True,
            "outcome": "triggered",
            "reason": "created_new_instance",
            "hint": "Created a new durable workflow instance for this event.",
            "workflow_id": "#V#some_workflow_resolved_from_binding",
            "selected_workflow_id": "#V#some_workflow_resolved_from_binding",
            "instance_id": "wf_identity_1",
            "event_type": kwargs.get("event_type"),
            "event_id": kwargs.get("event_id"),
            "idempotency_key": kwargs.get("event_id"),
            "idempotent_reused": False,
            "verification": {"launch_input_resolved": True},
            "submission_status": "pending",
            "binding_id": "binding_test_1",
            "binding_source": "persistent",
            "launch_strategy": "resolved_persistent_bindings",
            "launches": [],
            "launch_count": 1,
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.launch_event_workflow",
        _fake_launch,
    )

    report = mod.request_identity_resolution_for_materialised_scholarly_authors(
        author_concept_ids=["#V#person_michael_witbrock_0880532f"],
        paper_concept_id="#V#paper_one",
        author_names=["Michael Witbrock"],
        trigger_source="test_ingest",
        user_id="#V#michael_witbrock",
    )

    assert report["success"] is True
    assert report["triggered"] is True
    assert report["status"] == "pending"
    # The workflow_id in the report comes from the launch result (i.e. from
    # the persistent binding), not from a Python constant.
    assert report["workflow_id"] == "#V#some_workflow_resolved_from_binding"
    assert report["selected_workflow_id"] == "#V#some_workflow_resolved_from_binding"
    assert report["binding_id"] == "binding_test_1"
    assert report["launch_strategy"] == "resolved_persistent_bindings"

    # The request service must have called launch_event_workflow with the
    # event contract and *without* pre-selecting a workflow ID (workflow_id
    # stays None so binding resolution is the authoritative router).
    assert captured["event_type"] == mod.IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE
    assert captured["event_id"] == (
        "test_ingest:#V#paper_one:#V#person_michael_witbrock_0880532f"
    )
    assert captured["workflow_id"] is None
    assert captured["user_id"] == "#V#michael_witbrock"
    inputs = captured["inputs"]
    assert isinstance(inputs, dict)
    assert inputs["paper_concept_id"] == "#V#paper_one"
    assert inputs["candidate_concept_ids"] == [
        "#V#person_michael_witbrock_0880532f",
    ]
    assert inputs["author_concept_ids"] == [
        "#V#person_michael_witbrock_0880532f",
    ]
    assert inputs["author_names"] == ["Michael Witbrock"]
    assert inputs["candidate_names"] == ["Michael Witbrock"]
    assert inputs["trigger_source"] == "test_ingest"
    event_payload = captured["event_payload"]
    assert isinstance(event_payload, dict)
    assert event_payload["paper_concept_id"] == "#V#paper_one"
    assert event_payload["candidate_concept_ids"] == [
        "#V#person_michael_witbrock_0880532f",
    ]


def test_identity_resolution_request_passes_through_explicit_workflow_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``workflow_id`` overrides flow through to ``launch_event_workflow``.

    The override path mirrors ``launch_event_workflow``'s own contract: when
    callers pass an explicit workflow id, it is used as a one-off binding
    rather than the persisted registry.  This is allowed as an explicit, not
    silent, escape hatch.
    """

    from src.backend.services import identity_resolution_workflow_request_service as mod

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.launch_event_workflow",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "triggered": True,
            "outcome": "triggered",
            "reason": "created_new_instance",
            "workflow_id": kwargs.get("workflow_id"),
            "selected_workflow_id": kwargs.get("workflow_id"),
            "instance_id": "wf_identity_2",
            "event_type": kwargs.get("event_type"),
            "event_id": kwargs.get("event_id"),
            "idempotency_key": kwargs.get("event_id"),
            "idempotent_reused": False,
            "verification": {},
            "submission_status": "pending",
            "binding_source": "explicit",
            "launches": [],
            "launch_count": 1,
        },
    )

    report = mod.request_identity_resolution_for_candidate_concepts(
        candidate_concept_ids=["#V#person_a"],
        paper_concept_id="#V#paper_two",
        trigger_source="test_override",
        workflow_id="#V#some_other_workflow",
    )

    assert report["success"] is True
    assert report["workflow_id"] == "#V#some_other_workflow"
    assert captured["workflow_id"] == "#V#some_other_workflow"
    assert captured["event_type"] == mod.IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE


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


# ---------------------------------------------------------------------------
# JVNAUTOSCI-2152 — unit tests for extracted support-surface helpers
# ---------------------------------------------------------------------------


def test_ensure_relationship_edge_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_ensure_relationship_edge`` returns True when ``add_relationship`` succeeds.

    No fallback to ``mutate_relationship_edge`` should occur when the edge is
    already verified after the canonical write.
    """
    from src.backend.services import arxiv_paper_link_service as mod

    add_calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        mod,
        "add_relationship",
        lambda **kwargs: add_calls.append(kwargs) or {"success": True},
    )

    contains_calls: list[tuple[str, str, str]] = []

    def _fake_contains(concept_id: str, predicate: str, target: str) -> bool:
        contains_calls.append((concept_id, predicate, target))
        return True

    monkeypatch.setattr(mod, "_relation_contains_target", _fake_contains)

    # Sentinel: ConceptsRepository.mutate_relationship_edge must NOT be invoked.
    from src.backend.db.repositories import concepts_repository as repo_mod

    fallback_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        repo_mod.ConceptsRepository,
        "mutate_relationship_edge",
        classmethod(
            lambda cls, *args, **kwargs: fallback_calls.append((args, kwargs))
            or True
        ),
    )

    verified = mod._ensure_relationship_edge(
        source_id="#V#paper_x",
        predicate="#V#authored_by",
        target_id="#V#person_y",
    )

    assert verified is True
    assert len(add_calls) == 1
    assert add_calls[0]["source_id"] == "#V#paper_x"
    assert add_calls[0]["predicate"] == "#V#authored_by"
    assert add_calls[0]["target"] == "#V#person_y"
    assert fallback_calls == []
    # Two verification reads: the precondition check and the final verify call.
    assert contains_calls == [
        ("#V#paper_x", "#V#authored_by", "#V#person_y"),
        ("#V#paper_x", "#V#authored_by", "#V#person_y"),
    ]


def test_ensure_relationship_edge_fallback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the canonical ``add_relationship`` write doesn't show up, fall back to repo."""
    from src.backend.services import arxiv_paper_link_service as mod

    monkeypatch.setattr(mod, "add_relationship", lambda **kwargs: {"success": False})

    from src.backend.db.repositories import concepts_repository as repo_mod

    fallback_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_mutate(cls, *args: Any, **kwargs: Any) -> bool:
        fallback_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(
        repo_mod.ConceptsRepository,
        "mutate_relationship_edge",
        classmethod(_fake_mutate),
    )

    # First contains-call after add_relationship returns False → triggers fallback.
    # Second contains-call after the fallback returns True → edge is verified.
    contains_results = iter([False, True])
    monkeypatch.setattr(
        mod,
        "_relation_contains_target",
        lambda *args, **kwargs: next(contains_results),
    )

    verified = mod._ensure_relationship_edge(
        source_id="#V#paper_x",
        predicate="#V#about",
        target_id="#V#topic_y",
        maintain_inverse=True,
    )

    assert verified is True
    assert len(fallback_calls) == 1
    args, kwargs = fallback_calls[0]
    assert args == ("#V#paper_x", "#V#about", "#V#topic_y")
    assert kwargs == {"action": "add", "maintain_inverse": True}


def test_link_authors_to_paper_counts_verified_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_link_authors_to_paper`` returns one concept per name and counts only verified edges."""
    from src.backend.services import arxiv_paper_link_service as mod

    monkeypatch.setattr(
        mod,
        "_resolve_or_create_person_concept_id",
        lambda *, user_concept_id, person_name, logger=None: f"#V#person_{person_name.lower().replace(' ', '_')}",
    )

    # Edge for "Bob Smith" fails to verify — should not count toward author_links_written.
    edge_results = {
        "#V#person_alice_jones": True,
        "#V#person_bob_smith": False,
        "#V#person_carol_lee": True,
    }
    monkeypatch.setattr(
        mod,
        "_ensure_relationship_edge",
        lambda *, source_id, predicate, target_id: edge_results[target_id],
    )

    out = mod._link_authors_to_paper(
        user_concept_id="#V#user_test",
        paper_concept_id="#V#paper_x",
        author_names=["Alice Jones", "Bob Smith", "Carol Lee"],
    )

    assert out["author_concept_ids"] == [
        "#V#person_alice_jones",
        "#V#person_bob_smith",
        "#V#person_carol_lee",
    ]
    assert out["author_links_written"] == 2


def test_link_topics_to_paper_honours_max_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_link_topics_to_paper`` truncates to ``max_relations`` and counts verified edges."""
    from src.backend.services import arxiv_paper_link_service as mod

    monkeypatch.setattr(
        mod,
        "_resolve_or_create_topic_concept_id",
        lambda *, user_concept_id, topic_label, logger=None: f"#V#topic_{topic_label.lower().replace('.', '_')}",
    )

    captured: list[str] = []

    def _fake_edge(*, source_id: str, predicate: str, target_id: str) -> bool:
        captured.append(target_id)
        return True

    monkeypatch.setattr(mod, "_ensure_relationship_edge", _fake_edge)

    out = mod._link_topics_to_paper(
        user_concept_id="#V#user_test",
        paper_concept_id="#V#paper_x",
        topic_labels=["cs.AI", "cs.CL", "cs.LG", "stat.ML", "cs.IR"],
        max_relations=3,
    )

    # Only the first 3 topics should be processed.
    assert out["topic_concept_ids"] == [
        "#V#topic_cs_ai",
        "#V#topic_cs_cl",
        "#V#topic_cs_lg",
    ]
    assert out["topic_links_written"] == 3
    assert captured == [
        "#V#topic_cs_ai",
        "#V#topic_cs_cl",
        "#V#topic_cs_lg",
    ]


def test_gather_arxiv_paper_verification_state_emits_each_failure_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each missing precondition produces its specific ``verification_failures`` token."""
    from src.backend.services import arxiv_paper_link_service as mod

    # Empty text-row reads — no hasName, no hasDescription, no publication_date.
    monkeypatch.setattr(
        mod, "get_texts_for_concept", lambda **kwargs: []
    )
    # All relations missing.
    monkeypatch.setattr(
        mod, "_relation_contains_target", lambda *args, **kwargs: False
    )

    state = mod._gather_arxiv_paper_verification_state(
        paper_concept_id="#V#paper_x",
        file_copy_concept_id="#V#file_y",
        normalised_arxiv_id="2502.14996",
        type_relation_success=False,
        title_asserted=False,
        summary_asserted=False,
        publication_date_asserted=False,
        author_concept_ids=[],
        author_links_written=0,
        topic_labels=[],
        topic_links_written=0,
    )

    failures = state["verification_failures"]
    assert "arxiv_identifier_missing" in failures
    assert "title_missing" in failures
    assert "summary_missing" in failures
    assert "publication_date_missing" in failures
    assert "authors_missing" in failures
    assert "type_missing" in failures
    assert "topic_missing" in failures
    assert "file_link_missing" in failures
    assert state["type_asserted"] is False
    assert state["file_link_verified"] is False
    assert state["summary_present"] is False


def test_gather_arxiv_paper_verification_state_all_satisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every precondition holds, ``verification_failures`` is empty."""
    from src.backend.services import arxiv_paper_link_service as mod

    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda *, subject_concept_id, predicate, limit: (
            [{"text": "2502.14996"}, {"text": "Some Title"}]
            if predicate == "hasName"
            else [{"text": "abstract"}]
        ),
    )
    monkeypatch.setattr(
        mod, "_relation_contains_target", lambda *args, **kwargs: True
    )

    state = mod._gather_arxiv_paper_verification_state(
        paper_concept_id="#V#paper_x",
        file_copy_concept_id="#V#file_y",
        normalised_arxiv_id="2502.14996",
        type_relation_success=True,
        title_asserted=True,
        summary_asserted=True,
        publication_date_asserted=True,
        author_concept_ids=["#V#person_a", "#V#person_b"],
        author_links_written=2,
        topic_labels=["cs.AI"],
        topic_links_written=1,
    )

    assert state["verification_failures"] == []
    assert state["type_asserted"] is True
    assert state["file_link_verified"] is True
    assert state["summary_present"] is True


