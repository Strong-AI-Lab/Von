"""Gateway-level tests for file-copy RAG read tools."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


_MISSING = object()


def _get_nested_value(doc: dict, path: str):
    value = doc
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


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
            for operator, expected in condition.items():
                if operator == "$exists":
                    if bool(expected) != (value is not _MISSING):
                        return False
                elif operator == "$in":
                    if value is _MISSING:
                        return False
                    if isinstance(value, list):
                        if not any(item in expected for item in value):
                            return False
                    elif value not in expected:
                        return False
                elif operator == "$nin":
                    if value is _MISSING:
                        continue
                    if isinstance(value, list):
                        if any(item in expected for item in value):
                            return False
                    elif value in expected:
                        return False
                else:
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

    def find(self, query, _projection=None):
        return _Cursor([doc for doc in self._docs if _matches_query(doc, query)])

    def find_one(self, query, _projection=None):
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


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_rag_file_copy_collection_gateway_reads(monkeypatch):
    gateway = _build_gateway()
    docs = [
        {
            "concept_id": "#V#uploaded_file_copy_gateway",
            "name": "gateway.txt",
            "attributes": {
                "blob_key": "uploads/user/hash/gateway.txt",
                "blob_backend": "swift",
                "blob_uri": "swift://bucket/uploads/user/hash/gateway.txt",
                "content_type": "text/plain",
                "size_bytes": 12,
            },
            "relationships": {"specific_to_user": ["#V#user"]},
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]
    concepts = _ConceptCollection(docs)

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"concepts": concepts}),
    )

    listed = gateway.invoke(
        "rag_list_indexed",
        {
            "namespace": "#V#user@org",
            "collection": "file_copy_concepts",
            "limit": 10,
            "offset": 0,
        },
    ).payload
    assert listed.get("success") is True
    assert listed.get("collection") == "file_copy_concepts"
    assert listed.get("total") == 1

    got = gateway.invoke(
        "rag_get_item",
        {
            "namespace": "#V#user@org",
            "collection": "file_copy_concepts",
            "session_id": "#V#uploaded_file_copy_gateway",
        },
    ).payload
    assert got.get("success") is True
    assert got.get("concept_id") == "#V#uploaded_file_copy_gateway"
