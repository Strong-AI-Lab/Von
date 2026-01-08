import hashlib
import io
import urllib.parse
from typing import Any

import pytest
from flask import Flask


class _FakeStore:
    def __init__(self, blob_ref_cls):
        self._blob_ref_cls = blob_ref_cls
        self.last_put = None
        self._bytes_by_key = {}

    def put_bytes(self, key, data, content_type=None, metadata=None):
        self.last_put = {
            "key": key,
            "data": data,
            "content_type": content_type,
            "metadata": metadata,
        }
        self._bytes_by_key[key] = bytes(data)
        return self._blob_ref_cls(
            backend="local",
            key=key,
            uri=f"local://{key}",
            content_type=content_type,
            size_bytes=len(data),
            metadata=metadata or {},
        )

    def get_bytes(self, key):
        if key not in self._bytes_by_key:
            raise FileNotFoundError(key)
        return self._bytes_by_key[key]


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp
    from src.backend.services.blob_store import BlobRef

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    fake_store = _FakeStore(BlobRef)
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: fake_store,
    )

    # Avoid touching the database: keep an in-memory concept table.
    concept_docs: dict[str, dict[str, Any]] = {
        "#V#computer_file_copy": {"concept_id": "#V#computer_file_copy"},
        "#V#store_of_information": {"concept_id": "#V#store_of_information"},
    }

    def fake_find_one(query, projection=None):
        concept_id = (query or {}).get("concept_id")
        if not concept_id:
            return None
        return concept_docs.get(concept_id)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        fake_find_one,
    )

    updates = []

    def fake_update_one(filter_doc: dict[str, Any], update_doc: dict[str, Any]):
        updates.append((filter_doc, update_doc))

        concept_id = (filter_doc or {}).get("concept_id")
        if concept_id and concept_id in concept_docs and isinstance(update_doc, dict):
            set_doc: dict[str, Any] = {}
            raw_set = update_doc.get("$set")
            if isinstance(raw_set, dict):
                set_doc = dict(raw_set)
            for key, value in set_doc.items():
                if key == "relationships.specific_to_user":
                    doc: dict[str, Any] = concept_docs.get(concept_id) or {
                        "concept_id": concept_id
                    }
                    rel: dict[str, Any] = {}
                    raw_rel = doc.get("relationships")
                    if isinstance(raw_rel, dict):
                        rel = dict(raw_rel)
                    rel["specific_to_user"] = value
                    doc["relationships"] = rel
                    concept_docs[concept_id] = doc
                else:
                    doc: dict[str, Any] = concept_docs.get(concept_id) or {
                        "concept_id": concept_id
                    }
                    doc[key] = value
                    concept_docs[concept_id] = doc

        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        fake_update_one,
    )

    created = []

    def fake_create_concept(**kwargs):
        created.append(kwargs)
        concept_id = kwargs.get("concept_id")
        if concept_id:
            concept_docs[concept_id] = {
                "concept_id": concept_id,
                "name": kwargs.get("name") or kwargs.get("concept_id"),
                "relationships": {},
            }
        return {"success": True, "concept": {"concept_id": kwargs.get("concept_id")}}

    monkeypatch.setattr(
        "src.backend.services.concept_service.create_concept",
        fake_create_concept,
    )

    upserts = []
    text_values = {}

    def fake_upsert_text_for_concept(
        subject_concept_id,
        predicate,
        text,
        lang="en",
        provenance=None,
        context=None,
    ):
        upserts.append(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
                "text": text,
                "lang": lang,
            }
        )
        text_values[(subject_concept_id, predicate)] = [str(text)]
        return {"relation_id": "test", "text": text, "lang": lang}

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        fake_upsert_text_for_concept,
    )

    def fake_get_texts_for_concept(
        subject_concept_id, predicate=None, lang=None, limit=50
    ):
        if not predicate:
            return []
        values = text_values.get((subject_concept_id, predicate), [])
        results = []
        for v in values[: max(0, int(limit or 0) or 0)]:
            results.append({"text": v, "predicate": predicate, "lang": lang or "en-NZ"})
        return results

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        fake_get_texts_for_concept,
    )

    history_calls = []

    def fake_add_message_to_history(user_id, session_id, message, llm_debug_data=None):
        history_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "message": message,
                "llm_debug_data": llm_debug_data,
            }
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        fake_add_message_to_history,
    )

    flask_app.config["TEST_FAKE_STORE"] = fake_store
    flask_app.config["TEST_CREATED"] = created
    flask_app.config["TEST_UPSERTS"] = upserts
    flask_app.config["TEST_UPDATES"] = updates
    flask_app.config["TEST_HISTORY_CALLS"] = history_calls
    flask_app.config["TEST_CONCEPT_DOCS"] = concept_docs
    return flask_app


def test_upload_requires_user_session(app):
    client = app.test_client()

    resp = client.post(
        "/von/api/files/upload",
        data={"file": (io.BytesIO(b"hello"), "hello.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "missing_user_context"


def test_upload_stores_bytes_and_registers_concept(app):
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user"
        sess["session_id"] = "test-session"

    payload = b"hello world"
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    resp = client.post(
        "/von/api/files/upload",
        data={"file": (io.BytesIO(payload), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["success"] is True
    assert body["chat_history_recorded"] is True
    assert body["uploaded"]["type_concept_id"] == "#V#computer_file_copy"
    assert body["uploaded"]["sha256"] == expected_sha256
    assert body["uploaded"]["original_filename"] == "notes.txt"

    fake_store = app.config["TEST_FAKE_STORE"]
    assert fake_store.last_put is not None
    assert fake_store.last_put["metadata"]["sha256"] == expected_sha256
    assert fake_store.last_put["key"].startswith(f"uploads/user/{expected_sha256}/")
    assert fake_store.last_put["key"].endswith("notes.txt")

    created = app.config["TEST_CREATED"]
    assert len(created) == 1
    assert created[0]["create_as_instance"] is True
    assert created[0]["parent_concept_ids"] == ["#V#computer_file_copy"]

    upserts = app.config["TEST_UPSERTS"]
    predicates = {u["predicate"] for u in upserts}
    assert "#V#has_blob_uri" in predicates
    assert "#V#has_blob_key" in predicates
    assert "#V#has_blob_backend" in predicates
    assert "#V#has_original_filename" in predicates
    assert "#V#has_sha256" in predicates
    assert "#V#has_size_bytes" in predicates
    assert "#V#has_upload_timestamp" in predicates

    history_calls = app.config["TEST_HISTORY_CALLS"]
    assert len(history_calls) == 2
    assert history_calls[0]["user_id"] == "#V#user"
    assert history_calls[0]["session_id"] == "test-session"
    assert history_calls[0]["message"]["role"] == "user"
    assert "[UPLOAD]" in history_calls[0]["message"]["content"]

    assert history_calls[1]["message"]["role"] == "assistant"
    assistant_text = history_calls[1]["message"]["content"]
    assert "File copy concept:" in assistant_text
    assert "Blob URI:" in assistant_text
    assert "local://" in assistant_text
    assert expected_sha256 in assistant_text


def test_download_round_trip_and_access_control(app):
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user"
        sess["session_id"] = "test-session"

    payload = b"hello download"

    upload_resp = client.post(
        "/von/api/files/upload",
        data={"file": (io.BytesIO(payload), "download.txt")},
        content_type="multipart/form-data",
    )
    assert upload_resp.status_code == 200
    uploaded = upload_resp.get_json()["uploaded"]
    concept_id = uploaded["concept_id"]

    encoded = urllib.parse.quote(concept_id, safe="")
    download_resp = client.get(f"/von/api/files/{encoded}/download")
    assert download_resp.status_code == 200
    assert download_resp.data == payload
    assert "attachment" in (download_resp.headers.get("Content-Disposition") or "")

    # Different user should not be able to fetch (return 404 to avoid leakage).
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#other_user"
        sess["session_id"] = "test-session"

    denied_resp = client.get(f"/von/api/files/{encoded}/download")
    assert denied_resp.status_code == 404


def test_upload_returns_error_when_blob_store_fails(app, monkeypatch):
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user"
        sess["session_id"] = "test-session"

    fake_store = app.config["TEST_FAKE_STORE"]

    def boom(*args, **kwargs):
        raise RuntimeError("swift is down")

    monkeypatch.setattr(fake_store, "put_bytes", boom)

    resp = client.post(
        "/von/api/files/upload",
        data={"file": (io.BytesIO(b"hello"), "hello.txt")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 502
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "blob_store_upload_failed"

    created = app.config["TEST_CREATED"]
    assert created == []
