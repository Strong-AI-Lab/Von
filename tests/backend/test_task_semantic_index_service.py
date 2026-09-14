"""Isolated end-to-end task retrieval; no model requests or live databases."""

import json
import sqlite3
from pathlib import Path

import mongomock
import pytest
from flask import Flask

from src.backend.security import access_control as access
from src.backend.services import task_management_service as tasks
from src.backend.services import task_semantic_index_service as semantic


class FixtureEmbedder:
    """Known vector geometry tests retrieval plumbing, not neural model quality."""

    def __init__(self):
        self.texts = []
        self.fail = False
        self.on_embed = None

    def get_query_embedding(self, text):
        if self.fail:
            raise RuntimeError("private provider input must not be logged")
        return [1.0, 0.0]

    def get_text_embedding_batch(self, texts):
        self.texts.extend(texts)
        if self.on_embed:
            self.on_embed()
        return [[1.0, 0.0] if "airfare" in text else [0.0, 1.0] for text in texts]


@pytest.fixture
def environment(monkeypatch, tmp_path):
    collection = mongomock.MongoClient().db.concepts
    monkeypatch.setattr(tasks.ConceptsRepository, "collection", lambda: collection)
    monkeypatch.setattr(access, "get_concepts_collection", lambda: collection)
    rows = {}
    monkeypatch.setattr(
        tasks,
        "get_texts_for_concepts",
        lambda ids, **kw: {key: rows.get(key, []) for key in ids},
    )
    model = FixtureEmbedder()
    signature = {"provider": "fixture", "model": "fixture-v1"}

    class Runtime:
        def _capture_embedding_runtime(self, namespace):
            return {"embedding_signature": dict(signature)}, model, {}

    monkeypatch.setattr(semantic, "get_rag_service", Runtime)
    monkeypatch.setenv("VON_TASK_SEMANTIC_INDEX_PATH", str(tmp_path / "index.sqlite3"))

    def add(
        key, title, description="", actor=None, org=None, status="pending", project=None
    ):
        identity = "#V#" + key
        relationships = {"is_an_instance_of": [tasks.TASK_SPECIFICATION_TYPE_ID]}
        if actor:
            relationships["#V#specific_to_user"] = [actor]
        if org:
            relationships["#V#specific_to_organisation"] = [org]
        collection.insert_one(
            {
                "concept_id": identity,
                "relationships": relationships,
                "metadata": {"project_concept_id": project},
            }
        )
        rows[identity] = [
            {"predicate": "#V#hasName", "text": title},
            {"predicate": "#V#hasDescription", "text": description},
            {"predicate": "#V#hasTaskStatus", "text": status},
        ]
        return identity

    return collection, rows, model, signature, add


def search(**kwargs):
    return tasks.search_tasks(
        query="organise transport", search_mode="semantic", **kwargs
    )


def test_semantic_paraphrase_filters_pagination_and_context(environment):
    _, _, model, _, add = environment
    add("minutes", "Summarise lab meeting", project="#V#project")
    winner = add(
        "travel", "Arrange conference flights", "Book airfare", project="#V#project"
    )
    add(
        "done",
        "Arrange old travel",
        "airfare",
        status="completed",
        project="#V#project",
    )
    add("other", "Other trip", "airfare", project="#V#other")
    with access.override_current_actor("#V#alice"):
        result = search(project_concept_id="#V#project", statuses=["pending"], limit=1)
        assert result["tasks"][0]["task_concept_id"] == winner
        assert result["total"] == 2
        assert result["semantic_retrieval"]["embedded"] == 2
        assert len(model.texts) == 2
        assert result["rag_context"][0]["citation"] == winner
        assert result["rag_context"][0]["content_role"] == "retrieved_data"
        page = search(
            project_concept_id="#V#project", statuses=["pending"], limit=1, offset=1
        )
        assert page["tasks"][0]["task_concept_id"] == "#V#minutes"
        assert page["semantic_retrieval"]["cache_hits"] == 2
        assert tasks.search_tasks(query="organise transport")["count"] == 0


def test_revision_change_refreshes_without_timestamp_change(environment):
    _, rows, model, _, add = environment
    identity = add("travel", "Travel", "Book airfare")
    with access.override_current_actor("#V#alice"):
        initial = search()
        assert search()["semantic_retrieval"]["cache_hits"] == 1
        rows[identity][1]["text"] = "Cancelled; retain meeting notes instead"
        updated = search()
        assert updated["semantic_retrieval"]["embedded"] == 1
        assert (
            initial["tasks"][0]["indexed_revision"]
            != updated["tasks"][0]["indexed_revision"]
        )
        assert "Cancelled" in updated["rag_context"][0]["text"]
        assert len(model.texts) == 2


def test_model_version_and_corrupt_vector_recover(environment):
    _, _, _, signature, add = environment
    add("travel", "Travel", "airfare")
    with access.override_current_actor("#V#alice"):
        search()
        signature["model"] = "fixture-v2"
        assert search()["semantic_retrieval"]["embedded"] == 1
        with sqlite3.connect(semantic._index_path()) as connection:
            connection.execute("UPDATE task_vectors SET vector = 'invalid json'")
        assert search()["semantic_retrieval"]["embedded"] == 1
        receipt = semantic.reindex_semantic_tasks()
        assert receipt["embedded"] == 1
        assert receipt["cache_hits"] == 0


def test_actor_organisation_and_anonymous_boundaries(environment):
    _, _, model, _, add = environment
    add("private", "Private travel", "airfare", actor="#V#alice")
    add("org", "Organisation travel", "airfare", org="#V#org")
    add("public", "Public minutes")
    with access.override_current_actor("#V#alice", "#V#org"):
        assert search()["count"] == 3
    with access.override_current_actor("#V#bob", "#V#other"):
        result = search(assignee_concept_id="#V#alice")
        assert result["count"] == 0  # Payload filter never supplies authority.
        result = search()
        assert [task["task_concept_id"] for task in result["tasks"]] == ["#V#public"]
        assert result["semantic_retrieval"]["cache_hits"] == 0
    # An anonymous caller cannot acquire organisation visibility from an org selection.
    with access.override_current_actor(None, "#V#org"):
        assert search()["count"] == 1
    assert sum("Private travel" in text for text in model.texts) == 1


@pytest.mark.parametrize("delete", [False, True])
def test_permission_revocation_and_deletion_remove_warm_results(environment, delete):
    collection, _, _, _, add = environment
    identity = add("travel", "Private travel", "airfare", actor="#V#alice")
    with access.override_current_actor("#V#alice"):
        assert search()["count"] == 1
        if delete:
            collection.delete_one({"concept_id": identity})
        else:
            collection.update_one(
                {"concept_id": identity},
                {"$set": {"relationships.#V#specific_to_user": ["#V#bob"]}},
            )
        result = search()
        assert result["count"] == 0
        assert result["rag_context"] == []
        with sqlite3.connect(semantic._index_path()) as connection:
            assert (
                connection.execute("SELECT count(*) FROM task_vectors").fetchone()[0]
                == 0
            )


def test_revoked_during_embedding_is_excluded(environment):
    collection, _, model, _, add = environment
    identity = add("travel", "Private travel", "airfare", actor="#V#alice")
    model.on_embed = lambda: collection.delete_one({"concept_id": identity})
    with access.override_current_actor("#V#alice"):
        result = search()
    assert result["count"] == 0
    assert result["semantic_retrieval"]["access_recheck_excluded"] == 1


def test_provider_failure_returns_explicit_lexical_fallback_without_private_error(
    environment, caplog
):
    _, _, model, _, add = environment
    add("travel", "organise transport", "airfare")
    model.fail = True
    with access.override_current_actor("#V#alice"):
        result = search()
    assert result["semantic_retrieval"]["status"] == "degraded"
    assert result["count"] == 1
    assert "private provider input" not in caplog.text
    assert "rag_context" not in result


@pytest.mark.parametrize("embedding", [[0, 0], [float("nan"), 1], [1], []])
def test_invalid_embedding_does_not_produce_semantic_success(environment, embedding):
    _, _, model, _, add = environment
    add("travel", "Travel", "airfare")
    model.get_text_embedding_batch = lambda texts: [embedding for _ in texts]
    with access.override_current_actor("#V#alice"):
        assert search()["semantic_retrieval"]["status"] == "degraded"


def test_rest_path_uses_server_session_and_current_canonical_tasks(environment):
    from src.backend.server.routes.task_routes import task_bp

    _, _, _, _, add = environment
    add("alice", "Private flights", "airfare", actor="#V#alice")
    add("bob", "Other flights", "airfare", actor="#V#bob")
    app = Flask(__name__)
    app.secret_key = "fixture-only"
    app.register_blueprint(task_bp, url_prefix="/api/tasks")
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#alice"
    response = client.get(
        "/api/tasks/search",
        query_string={"query": "organise transport", "search_mode": "semantic"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["semantic_retrieval"]["status"] == "ready"
    assert [task["task_concept_id"] for task in payload["tasks"]] == ["#V#alice"]


def test_context_budget_and_document_provenance():
    task = {
        "task_concept_id": "#V#travel",
        "description": "x" * 20000,
        "external_references": {"jira": {"external_id": "ABC-1"}},
    }
    document = semantic.build_task_document(task)
    assert "ABC-1" in document["text"]
    context = semantic.assemble_task_context(
        [{**task, "retrieval_text": document["text"], "semantic_score": 1}]
    )
    assert len(context[0]["text"]) == 12000
    assert context[0]["truncated"] is True


def test_rejects_invalid_modes_internal_parameters_and_bypass(environment):
    with pytest.raises(tasks.InvalidTaskDataError):
        tasks.search_tasks(search_mode="invented")
    with pytest.raises(tasks.InvalidTaskDataError):
        tasks.search_tasks(_semantic_query="private")
    with pytest.raises(tasks.InvalidTaskDataError):
        tasks.search_tasks(search_mode="semantic", query=" ")
    with access.bypass_access_control(), pytest.raises(tasks.InvalidTaskDataError):
        search()


def test_evaluation_fixture_has_explicit_relevance_judgements():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/task_retrieval/cases.json").read_text()
    )
    ids = {task["id"] for task in fixture["tasks"]}
    assert all(set(case["relevant"]) <= ids for case in fixture["queries"])


def test_evaluation_measures_semantic_and_lexical_baselines():
    from scripts.evaluate_task_semantic_retrieval import evaluate

    cases = {
        "tasks": [
            {"id": "travel", "title": "Arrange flights", "description": "airfare"},
            {"id": "minutes", "title": "Record discussion", "description": "meeting"},
        ],
        "queries": [{"query": "organise transport", "relevant": ["travel"]}],
    }
    receipt = evaluate(cases, FixtureEmbedder(), k=1)
    assert receipt["aggregate"]["semantic"] == {
        "recall_at_k": 1,
        "reciprocal_rank": 1,
        "ndcg_at_k": 1,
    }
    assert receipt["aggregate"]["lexical"]["recall_at_k"] == 0
    assert receipt["index_build_ms"] >= 0


def test_mcp_task_search_returns_citation_context(environment):
    from src.backend.integrations.internal_mcp.catalogue import _task_search

    _, _, _, _, add = environment
    identity = add("travel", "Arrange flights", "airfare", actor="#V#alice")
    with access.override_current_actor("#V#alice"):
        result = _task_search(query="organise transport", search_mode="semantic")
    assert result["success"] is True
    assert result["rag_context"][0]["citation"] == identity


def test_partial_batch_failure_preserves_reusable_batches(environment):
    _, _, model, _, add = environment
    for index in range(40):
        add(f"task_{index}", "Arrange flights", "airfare")
    original = model.get_text_embedding_batch
    calls = 0

    def fail_second_batch(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary embedding outage")
        return original(texts)

    model.get_text_embedding_batch = fail_second_batch
    with access.override_current_actor("#V#alice"):
        assert search()["semantic_retrieval"]["status"] == "degraded"
        model.get_text_embedding_batch = original
        receipt = search()["semantic_retrieval"]
        assert receipt["cache_hits"] == 32
        assert receipt["embedded"] == 8


def test_index_file_contains_no_task_text_and_uses_private_mode(environment):
    _, _, _, _, add = environment
    add("travel", "Private conference title", "Private airfare details")
    with access.override_current_actor("#V#alice"):
        search()
    path = semantic._index_path()
    assert b"Private conference title" not in path.read_bytes()
    assert b"Private airfare details" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_storage_is_degraded_not_authoritative_empty(environment, monkeypatch):
    monkeypatch.setattr(tasks.ConceptsRepository, "collection", lambda: None)
    with access.override_current_actor("#V#alice"):
        result = search()
    assert result["semantic_retrieval"]["status"] == "degraded"
    assert not semantic._index_path().exists()
