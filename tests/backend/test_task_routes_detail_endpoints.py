"""Route coverage for task detail endpoints used by task UI parity features."""

from __future__ import annotations

from flask import Flask

from src.backend.server.routes.task_routes import task_bp


def _build_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(task_bp, url_prefix="/api/tasks")
    return app.test_client()


def test_list_task_comments_route(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_list_task_comments(task_concept_id: str, *, limit: int, offset: int):
        captured["task_concept_id"] = task_concept_id
        captured["limit"] = limit
        captured["offset"] = offset
        return {
            "task_concept_id": task_concept_id,
            "comments": [],
            "total": 0,
            "count": 0,
            "offset": offset,
            "limit": limit,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.list_task_comments",
        _fake_list_task_comments,
    )

    response = client.get("/api/tasks/%23V%23task_1/comments?limit=20&offset=5")
    assert response.status_code == 200
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["limit"] == 20
    assert captured["offset"] == 5


def test_add_task_comment_route_uses_session_actor(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_add_task_comment(task_concept_id: str, *, body: str, author_concept_id=None):
        captured["task_concept_id"] = task_concept_id
        captured["body"] = body
        captured["author_concept_id"] = author_concept_id
        return {
            "comment_id": "comment_1",
            "body": body,
            "author_concept_id": author_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.add_task_comment",
        _fake_add_task_comment,
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_alice"

    response = client.post(
        "/api/tasks/%23V%23task_1/comments",
        json={"body": "Looks good"},
    )
    assert response.status_code == 201
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["body"] == "Looks good"
    assert captured["author_concept_id"] == "#V#user_alice"


def test_list_task_attachments_route(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_list_task_attachments(task_concept_id: str, *, limit: int, offset: int):
        captured["task_concept_id"] = task_concept_id
        captured["limit"] = limit
        captured["offset"] = offset
        return {
            "task_concept_id": task_concept_id,
            "attachments": [],
            "total": 0,
            "count": 0,
            "offset": offset,
            "limit": limit,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.list_task_attachments",
        _fake_list_task_attachments,
    )

    response = client.get("/api/tasks/%23V%23task_1/attachments?limit=10&offset=2")
    assert response.status_code == 200
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["limit"] == 10
    assert captured["offset"] == 2


def test_add_task_attachment_route_parses_size_bytes(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_add_task_attachment(
        task_concept_id: str,
        *,
        filename: str,
        uri: str,
        media_type=None,
        size_bytes=None,
        added_by_concept_id=None,
        note=None,
    ):
        captured["task_concept_id"] = task_concept_id
        captured["filename"] = filename
        captured["uri"] = uri
        captured["media_type"] = media_type
        captured["size_bytes"] = size_bytes
        captured["added_by_concept_id"] = added_by_concept_id
        captured["note"] = note
        return {
            "attachment_id": "attachment_1",
            "filename": filename,
            "uri": uri,
            "size_bytes": size_bytes,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.add_task_attachment",
        _fake_add_task_attachment,
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_alice"

    response = client.post(
        "/api/tasks/%23V%23task_1/attachments",
        json={
            "filename": "spec.pdf",
            "uri": "https://example.org/spec.pdf",
            "media_type": "application/pdf",
            "size_bytes": "2048",
            "note": "Draft attachment",
        },
    )
    assert response.status_code == 201
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["filename"] == "spec.pdf"
    assert captured["size_bytes"] == 2048
    assert captured["added_by_concept_id"] == "#V#user_alice"


def test_get_task_history_route(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_get_task_history(task_concept_id: str, *, limit: int, offset: int):
        captured["task_concept_id"] = task_concept_id
        captured["limit"] = limit
        captured["offset"] = offset
        return {
            "task_concept_id": task_concept_id,
            "history": [],
            "total": 0,
            "count": 0,
            "offset": offset,
            "limit": limit,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.get_task_history",
        _fake_get_task_history,
    )

    response = client.get("/api/tasks/%23V%23task_1/history?limit=30&offset=1")
    assert response.status_code == 200
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["limit"] == 30
    assert captured["offset"] == 1


def test_add_and_remove_task_link_routes(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_link_tasks(
        source_task_concept_id: str,
        target_task_concept_id: str,
        *,
        link_type: str,
        actor_concept_id=None,
    ):
        captured["add_source"] = source_task_concept_id
        captured["add_target"] = target_task_concept_id
        captured["add_link_type"] = link_type
        captured["add_actor"] = actor_concept_id
        return {"linked": True}

    def _fake_unlink_tasks(
        source_task_concept_id: str,
        target_task_concept_id: str,
        *,
        link_type: str,
        actor_concept_id=None,
    ):
        captured["remove_source"] = source_task_concept_id
        captured["remove_target"] = target_task_concept_id
        captured["remove_link_type"] = link_type
        captured["remove_actor"] = actor_concept_id
        return {"unlinked": True}

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.link_tasks",
        _fake_link_tasks,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.unlink_tasks",
        _fake_unlink_tasks,
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_alice"

    add_response = client.post(
        "/api/tasks/%23V%23task_1/links",
        json={
            "target_task_concept_id": "#V#task_2",
            "link_type": "blocks",
        },
    )
    assert add_response.status_code == 201
    assert captured["add_source"] == "#V#task_1"
    assert captured["add_target"] == "#V#task_2"
    assert captured["add_link_type"] == "blocks"
    assert captured["add_actor"] == "#V#user_alice"

    remove_response = client.post(
        "/api/tasks/%23V%23task_1/links/remove",
        json={
            "target_task_concept_id": "#V#task_2",
            "link_type": "blocks",
        },
    )
    assert remove_response.status_code == 200
    assert captured["remove_source"] == "#V#task_1"
    assert captured["remove_target"] == "#V#task_2"
    assert captured["remove_link_type"] == "blocks"
    assert captured["remove_actor"] == "#V#user_alice"
