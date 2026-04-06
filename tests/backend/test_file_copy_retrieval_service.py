from __future__ import annotations


def test_fetch_file_copy_bytes_returns_data(monkeypatch):
    from src.backend.services.computer_file_copy_service import fetch_file_copy_bytes

    concept_id = "#V#uploaded_file_copy_test"

    def fake_find_one(filter_doc, projection=None):
        if (filter_doc or {}).get("concept_id") == concept_id:
            return {"concept_id": concept_id, "name": "notes.txt"}
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        fake_find_one,
    )

    text_values = {
        "#V#has_blob_key": "uploads/user/abc/notes.txt",
        "#V#has_blob_backend": "local",
        "#V#has_blob_uri": "local://uploads/user/abc/notes.txt",
        "#V#has_mime_type": "text/plain",
        "#V#has_original_filename": "notes.txt",
        "#V#has_size_bytes": "11",
    }

    def fake_get_texts_for_concept(subject_concept_id, predicate=None, **kwargs):
        if subject_concept_id != concept_id or predicate not in text_values:
            return []
        return [{"text": text_values[predicate]}]

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        fake_get_texts_for_concept,
    )

    class _FakeStore:
        def get_bytes(self, key):
            assert key == text_values["#V#has_blob_key"]
            return b"hello world"

    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FakeStore(),
    )

    result = fetch_file_copy_bytes(file_copy_concept_id=concept_id)

    assert result["success"] is True
    assert result["data"] == b"hello world"
    info = result["info"]
    assert info.blob_key == text_values["#V#has_blob_key"]
    assert info.content_type == text_values["#V#has_mime_type"]
    assert info.original_filename == text_values["#V#has_original_filename"]
    assert info.size_bytes == 11


def test_fetch_file_copy_bytes_not_found(monkeypatch):
    from src.backend.services.computer_file_copy_service import fetch_file_copy_bytes

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *args, **kwargs: None,
    )

    result = fetch_file_copy_bytes(file_copy_concept_id="#V#missing")
    assert result["success"] is False
    assert result["error"] == "not_found"


def test_fetch_file_copy_bytes_denies_out_of_scope_actor(monkeypatch):
    from src.backend.services.computer_file_copy_service import fetch_file_copy_bytes

    concept_id = "#V#private_file_copy"

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": concept_id,
                "name": "secret.txt",
                "relationships": {"specific_to_user": ["#V#owner_user"]},
            }
            if (filter_doc or {}).get("concept_id") == concept_id
            else None
        ),
    )

    result = fetch_file_copy_bytes(
        file_copy_concept_id=concept_id,
        user_concept_id="#V#other_user",
    )

    assert result["success"] is False
    assert result["error"] == "not_found"


def test_fetch_file_copy_bytes_allows_org_scoped_actor_from_namespace(monkeypatch):
    from src.backend.services.computer_file_copy_service import fetch_file_copy_bytes

    concept_id = "#V#org_shared_file_copy"

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": concept_id,
                "name": "shared-notes.txt",
                "relationships": {"specific_to_org": ["#V#team_org"]},
            }
            if (filter_doc or {}).get("concept_id") == concept_id
            else None
        ),
    )

    text_values = {
        "#V#has_blob_key": "uploads/team/shared-notes.txt",
        "#V#has_blob_backend": "local",
        "#V#has_blob_uri": "local://uploads/team/shared-notes.txt",
        "#V#has_mime_type": "text/plain",
        "#V#has_original_filename": "shared-notes.txt",
        "#V#has_size_bytes": "12",
    }

    def fake_get_texts_for_concept(subject_concept_id, predicate=None, **kwargs):
        if subject_concept_id != concept_id or predicate not in text_values:
            return []
        return [{"text": text_values[predicate]}]

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        fake_get_texts_for_concept,
    )

    class _FakeStore:
        def get_bytes(self, key):
            assert key == text_values["#V#has_blob_key"]
            return b"team hello!!"

    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FakeStore(),
    )

    result = fetch_file_copy_bytes(
        file_copy_concept_id=concept_id,
        namespace="#V#reader_user@team_org",
    )

    assert result["success"] is True
    assert result["data"] == b"team hello!!"
