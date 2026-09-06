"""Current-product selection must remain explicit and actor-visible."""

import hashlib
from unittest.mock import Mock

import pytest

from src.backend.services import task_work_product_service as service
from src.backend.services import task_management_service as tasks


def task_doc(targets):
    return {
        "concept_id": "#V#task_a",
        "relationships": {
            service.PREDICATE_HAS_CURRENT_WORK_PRODUCT: targets,
            "is_an_instance_of": [tasks.TASK_SPECIFICATION_TYPE_ID],
        },
    }


@pytest.fixture
def reads(monkeypatch):
    access = Mock(return_value=True)
    find = Mock(return_value={"concept_id": "#V#brief_a"})
    texts = Mock(return_value=[{"text": "Current A", "lang": "en-NZ"}])
    monkeypatch.setattr(service, "can_access_concept", access)
    monkeypatch.setattr(service.ConceptsRepository, "find_one", find)
    monkeypatch.setattr(service, "get_texts_for_concept", texts)
    return access, find, texts


def test_selected_product_is_read_fresh_without_neighbour_substitution(reads):
    _, _, texts = reads
    doc = task_doc(["#V#brief_a"])
    ref = service.resolve_task_work_product(doc)
    assert ref["concept_id"] == "#V#brief_a"
    assert "content" not in ref
    texts.return_value = [{"text": "Revised A", "lang": "en-NZ"}]
    product = service.resolve_task_work_product(doc, include_content=True)
    assert product["content"] == "Revised A"
    assert product["content_sha256"] == hashlib.sha256(b"Revised A").hexdigest()
    texts.assert_called_with(
        "#V#brief_a", predicate="#V#hasContent", limit=2, context_view="actor_effective"
    )


@pytest.mark.parametrize(
    "targets, status",
    [([], "missing"), (["#V#brief_a", "#V#brief_b"], "ambiguous_link")],
)
def test_missing_or_ambiguous_reference_never_guesses(reads, targets, status):
    access, find, texts = reads
    assert service.resolve_task_work_product(
        task_doc(targets), include_content=True
    ) == {"status": status}
    access.assert_not_called()
    find.assert_not_called()
    texts.assert_not_called()


def test_inaccessible_reference_discloses_neither_identity_nor_body(reads):
    access, find, texts = reads
    access.return_value = False
    assert service.resolve_task_work_product(
        task_doc(["#V#private_b"]), include_content=True
    ) == {"status": "unavailable"}
    find.assert_not_called()
    texts.assert_not_called()


@pytest.mark.parametrize(
    "rows, status",
    [
        ([], "missing_content"),
        ([{"text": "Old"}, {"text": "New"}], "ambiguous_content"),
    ],
)
def test_content_ambiguity_does_not_claim_a_current_revision(reads, rows, status):
    reads[2].return_value = rows
    product = service.resolve_task_work_product(
        task_doc(["#V#brief_a"]), include_content=True
    )
    assert product == {"concept_id": "#V#brief_a", "status": status}


def test_task_get_exposes_canonical_reference_to_existing_consumers(monkeypatch, reads):
    monkeypatch.setattr(
        tasks, "_get_task_doc", lambda _: ("#V#task_a", task_doc(["#V#brief_a"]))
    )
    monkeypatch.setattr(
        tasks, "_build_task_response", lambda _: {"task_concept_id": "#V#task_a"}
    )
    assert (
        tasks.get_task("#V#task_a")["current_work_product"]["concept_id"]
        == "#V#brief_a"
    )


def test_invalid_product_link_cannot_mutate_other_task_fields(monkeypatch):
    monkeypatch.setattr(tasks, "_get_task_doc", lambda _: ("#V#task_a", task_doc([])))
    monkeypatch.setattr(tasks, "_build_task_response", lambda _: {})
    monkeypatch.setattr(tasks, "can_access_concept", lambda _: False)
    mutation = Mock()
    monkeypatch.setattr(tasks, "_replace_single_relationship_target", mutation)
    monkeypatch.setattr(tasks, "update_task_status", mutation)
    with pytest.raises(tasks.InvalidTaskDataError, match="not accessible"):
        tasks.update_task_fields(
            "#V#task_a",
            fields={
                "current_work_product_concept_id": "#V#private_b",
                "status": "completed",
            },
        )
    mutation.assert_not_called()


def test_product_route_does_not_read_product_for_unavailable_task(monkeypatch):
    from flask import Flask
    from src.backend.server.routes.task_routes import task_bp

    def unavailable(_):
        raise tasks.TaskNotFoundError("Not accessible")

    monkeypatch.setattr(tasks, "_get_task_doc", unavailable)
    resolver = Mock()
    monkeypatch.setattr(service, "resolve_task_work_product", resolver)
    app = Flask(__name__)
    app.register_blueprint(task_bp, url_prefix="/api/tasks")
    response = app.test_client().get("/api/tasks/%23V%23private_task/work-product")
    assert response.status_code == 404
    assert response.json == {"status": "unavailable"}
    resolver.assert_not_called()
