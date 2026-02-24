from __future__ import annotations


_MISSING = object()


def _get_nested_value(doc: dict, path: str):
    value = doc
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _matches_operator(value, operator: str, expected) -> bool:
    if operator == "$exists":
        return bool(expected) == (value is not _MISSING)
    if operator == "$in":
        if value is _MISSING:
            return False
        if isinstance(value, list):
            return any(item in expected for item in value)
        return value in expected
    if operator == "$nin":
        if value is _MISSING:
            return True
        if isinstance(value, list):
            return all(item not in expected for item in value)
        return value not in expected
    return False


def _matches_query(doc: dict, query: dict) -> bool:
    for key, condition in query.items():
        if key == "$and":
            if not all(_matches_query(doc, clause) for clause in condition):
                return False
            continue
        if key == "$or":
            if not any(_matches_query(doc, clause) for clause in condition):
                return False
            continue

        value = _get_nested_value(doc, key)
        if isinstance(condition, dict):
            if not all(
                _matches_operator(value, operator, expected)
                for operator, expected in condition.items()
            ):
                return False
            continue
        if value is _MISSING or value != condition:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def skip(self, n):
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


class _ConceptCollection:
    def __init__(self, docs):
        self._docs = list(docs)
        self.last_find_query = None
        self.last_find_one_query = None

    def find(self, query, _projection=None):
        self.last_find_query = dict(query)
        return _Cursor([doc for doc in self._docs if _matches_query(doc, query)])

    def find_one(self, query, _projection=None):
        self.last_find_one_query = dict(query)
        for doc in self._docs:
            if _matches_query(doc, query):
                return doc
        return None

    def count_documents(self, query):
        return sum(1 for doc in self._docs if _matches_query(doc, query))


class _DB:
    def __init__(self, collections):
        self._collections = dict(collections)

    def __getitem__(self, key):
        return self._collections[key]


def test_rag_list_indexed_supports_file_copy_concepts(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "concept_id": "#V#uploaded_file_copy_visible",
            "name": "notes.txt",
            "attributes": {
                "blob_key": "uploads/user/hash/notes.txt",
                "blob_backend": "swift",
                "blob_uri": "swift://bucket/uploads/user/hash/notes.txt",
                "content_type": "text/plain",
                "size_bytes": 42,
            },
            "relationships": {"specific_to_user": ["#V#user"]},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T01:00:00Z",
        },
        {
            "concept_id": "#V#uploaded_file_copy_hidden",
            "name": "secret.txt",
            "attributes": {
                "blob_key": "uploads/other/hash/secret.txt",
                "blob_backend": "swift",
            },
            "relationships": {"specific_to_user": ["#V#other_user"]},
        },
        {
            "concept_id": "#V#uploaded_file_copy_missing_blob",
            "name": "broken.txt",
            "attributes": {},
            "relationships": {"specific_to_user": ["#V#user"]},
        },
    ]
    concepts = _ConceptCollection(docs)

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"concepts": concepts}),
    )

    result = cat._rag_list_indexed(
        namespace="#V#user@org",
        collection="blob_store_files",
        limit=10,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "file_copy_concepts"
    assert result["requested_collection"] == "blob_store_files"
    assert result["effective_collection"] == "file_copy_concepts"
    assert result["provenance"]["item_kind"] == "rag_file_copy_list"
    assert result["provenance"]["source_system"] == "mongo.concepts"
    assert result["total"] == 1
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["concept_id"] == "#V#uploaded_file_copy_visible"
    assert item["item_kind"] == "file_copy_concept"
    assert item["blob"]["key"] == "uploads/user/hash/notes.txt"


def test_rag_get_item_supports_file_copy_concepts(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "concept_id": "#V#uploaded_file_copy_visible",
            "name": "paper.pdf",
            "attributes": {
                "blob_key": "uploads/user/hash/paper.pdf",
                "blob_backend": "swift",
                "blob_uri": "swift://bucket/uploads/user/hash/paper.pdf",
                "content_type": "application/pdf",
                "size_bytes": 5120,
            },
            "relationships": {"specific_to_user": ["#V#user"]},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T02:00:00Z",
        }
    ]
    concepts = _ConceptCollection(docs)

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"concepts": concepts}),
    )

    result = cat._rag_get_item(
        namespace="#V#user@org",
        collection="file_copy_concepts",
        session_id="#V#uploaded_file_copy_visible",
    )

    assert result["success"] is True
    assert result["collection"] == "file_copy_concepts"
    assert result["provenance"]["item_kind"] == "rag_file_copy_item"
    assert result["concept_id"] == "#V#uploaded_file_copy_visible"
    assert result["blob"]["backend"] == "swift"
    assert "paper.pdf" in result["preview"]


def test_rag_get_item_file_copy_respects_visibility_scope(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "concept_id": "#V#uploaded_file_copy_hidden",
            "name": "private.pdf",
            "attributes": {
                "blob_key": "uploads/other/hash/private.pdf",
                "content_type": "application/pdf",
            },
            "relationships": {"specific_to_user": ["#V#other_user"]},
        }
    ]
    concepts = _ConceptCollection(docs)

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"concepts": concepts}),
    )

    result = cat._rag_get_item(
        namespace="#V#user@org",
        collection="file_copy_concepts",
        session_id="#V#uploaded_file_copy_hidden",
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
