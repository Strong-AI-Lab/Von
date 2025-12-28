import pytest


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


class _Collection:
    def __init__(self, docs):
        self._docs = list(docs)
        self.last_find_query = None

    def find(self, query, _projection=None):
        self.last_find_query = dict(query)
        docs = []
        for doc in self._docs:
            if query.get("indexing_status") and doc.get("indexing_status") != query.get(
                "indexing_status"
            ):
                continue
            if query.get("namespace") and doc.get("namespace") != query.get(
                "namespace"
            ):
                continue
            docs.append(doc)
        return _Cursor(docs)

    def find_one(self, query, _projection=None):
        for doc in self.find(query, _projection=_projection):
            return doc
        return None

    def count_documents(self, query):
        return len(list(self.find(query)))


class _DB:
    def __init__(self, collections):
        self._collections = dict(collections)

    def __getitem__(self, key):
        return self._collections[key]


class _StubRAG:
    def __init__(self, results):
        self._results = results
        self.last_permissions_context = None

    def query(self, *, query_text, top_k, namespace, permissions_context=None):
        self.last_permissions_context = permissions_context
        return list(self._results)


def test_rag_list_indexed_includes_provenance(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "_id": "s1",
            "indexing_status": "indexed",
            "indexed_at": "2025-01-01T00:00:00Z",
            "namespace": "#V#user@org",
            "summary": "hello",
            "history": [{"content": "world"}],
        }
    ]

    coll = _Collection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"interaction_sessions": coll}),
    )

    result = cat._rag_list_indexed(namespace="#V#user@org", limit=10, offset=0)
    assert result["success"] is True

    # Echo effective scoping and defaults
    assert result["requested_collection"] is None
    assert result["effective_collection"] == "ka_sessions"
    assert result["collection_source"] == "default"
    assert result["effective_namespace"] == "#V#user@org"
    assert result["effective_namespace_source"] == "request.namespace"

    assert result["namespace"] == "#V#user@org"
    assert result["namespace_source"] == "request.namespace"

    assert "provenance" in result
    assert result["provenance"]["item_kind"] == "rag_indexed_session_list"
    assert result["provenance"]["source_system"] == "mongo.interaction_sessions"

    assert result["items"], "expected at least one item"
    item = result["items"][0]
    assert item["item_kind"] == "ka_interaction_session"
    assert item["source_system"] == "mongo.interaction_sessions"
    assert item["namespace_source"] == "request.namespace"


def test_search_knowledge_base_stamps_result_metadata(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    stub = _StubRAG(
        results=[
            {"id": "doc1", "score": 0.9, "text": "hi", "metadata": {"x": 1}},
            {"id": "doc2", "score": 0.1, "text": "lo", "metadata": None},
        ]
    )

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue.get_rag_service",
        lambda *_args, **_kwargs: stub,
        raising=False,
    )

    # Patch the imported symbol in rag_service module (which catalogue imports from)
    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: stub,
    )

    result = cat._search_knowledge_base(query="test", namespace="#V#user@org")
    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["namespace_source"] == "request.namespace"

    assert isinstance(result.get("elapsed_ms"), int)
    assert result["elapsed_ms"] >= 0
    assert result["effective_namespace"] == "#V#user@org"
    assert result["effective_namespace_source"] == "request.namespace"

    assert result["results"], "expected results"
    for row in result["results"]:
        assert "metadata" in row
        assert row["metadata"]["item_kind"] == "rag_chunk"
        assert row["metadata"]["source_system"] == "rag.llamaindex"
        assert row["metadata"]["namespace"] == "#V#user@org"
        assert row["metadata"]["namespace_source"] == "request.namespace"


def test_search_knowledge_base_derives_permissions_context_from_namespace(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    stub = _StubRAG(results=[])

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: stub,
    )

    cat._search_knowledge_base(
        query="workflow",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    )

    assert stub.last_permissions_context == {
        "user_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
    }


def test_rag_list_collections_requires_namespace():
    from src.backend.integrations.internal_mcp import catalogue as cat

    result = cat._rag_list_collections()
    assert result["success"] is False
    assert result["error"] == "namespace_required"


def test_rag_list_collections_includes_expected_collections():
    from src.backend.integrations.internal_mcp import catalogue as cat

    result = cat._rag_list_collections(namespace="#V#user@org")
    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["namespace_source"] == "request.namespace"

    assert result["provenance"]["item_kind"] == "rag_collection_list"
    assert result["collections"]
    collections = {c["collection"] for c in result["collections"]}
    assert "ka_sessions" in collections
    assert "chat_history_sessions" in collections
    assert "rag_documents" in collections

    # Capability flags prevent the model inferring behaviour from missing tools.
    by_name = {c["collection"]: c for c in result["collections"]}
    assert by_name["ka_sessions"]["list_supported"] is True
    assert by_name["ka_sessions"]["get_supported"] is True
    assert by_name["rag_documents"]["list_supported"] is False
    assert by_name["rag_documents"]["get_supported"] is False


def test_rag_list_indexed_supports_chat_history_sessions(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    chat_docs = [
        {
            "session_id": "chat-1",
            "namespace": "#V#user@org",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-02T00:00:00Z",
            "history": [{"content": "hello"}, {"content": "world"}],
            "rag_indexed_success": 2,
            "rag_indexed_failed": 0,
        }
    ]

    chat_coll = _Collection(chat_docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"chat_history": chat_coll}),
    )

    result = cat._rag_list_indexed(
        namespace="#V#user@org",
        collection="chat_history_sessions",
        limit=10,
        offset=0,
    )
    assert result["success"] is True
    assert result["collection"] == "chat_history_sessions"

    assert result["requested_collection"] == "chat_history_sessions"
    assert result["effective_collection"] == "chat_history_sessions"
    assert result["collection_source"] == "request.collection"
    assert result["effective_namespace"] == "#V#user@org"

    assert result["provenance"]["item_kind"] == "rag_chat_session_list"
    assert result["provenance"]["source_system"] == "mongo.chat_history"

    assert result["items"]
    item = result["items"][0]
    assert item["item_kind"] == "chat_history_session"
    assert item["source_system"] == "mongo.chat_history"
