
from contextlib import nullcontext


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
    assert result["user_concept_id"] == "#V#user"
    assert result["organisation_concept_id"] == "#V#org"

    assert "provenance" in result
    assert result["provenance"]["item_kind"] == "rag_indexed_session_list"
    assert result["provenance"]["source_system"] == "mongo.interaction_sessions"
    assert result["provenance"]["user_concept_id"] == "#V#user"
    assert result["provenance"]["organisation_concept_id"] == "#V#org"

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
    assert result["user_concept_id"] == "#V#user"
    assert result["organisation_concept_id"] == "#V#org"

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


def test_search_knowledge_base_degrades_when_embedding_backend_fails(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    class _FailingRAG:
        def query(self, **_kwargs):
            raise RuntimeError("insufficient_quota while calling /embeddings")

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _FailingRAG(),
    )

    result = cat._search_knowledge_base(query="workflow", namespace="#V#user@org")

    assert result["success"] is False
    assert result["fallback_used"] is True
    assert result["fallback_mode"] == "typed_retrieval_state"
    assert result["count"] == 0
    assert result["results"] == []
    assert result["fallback_reason"] == "rag_query_degraded:RuntimeError"
    assert result["retrieval_state"] == {
        "schema_version": "rag_retrieval_state.v1",
        "status": "degraded",
        "usable": False,
        "authoritative_empty": False,
        "result_count": 0,
        "retryable": True,
        "rebuild_required": False,
        "recovery_affordances": [
            {"action_type": "retry"},
            {"action_type": "inspect_runtime"},
        ],
        "cause": "rag_query_degraded:RuntimeError",
        "detail": "The configured RAG retrieval surface degraded during this attempt.",
    }


def test_get_related_concepts_falls_back_to_graph_text_when_rag_degrades(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    class _FailingRAG:
        def query(self, **_kwargs):
            raise RuntimeError("insufficient_quota while calling /embeddings")

    def _preferred_rows(
        subject_concept_ids,
        *,
        predicate_precedence=None,
        **_kwargs,
    ):
        rows = {}
        first_group = predicate_precedence[0] if predicate_precedence else ()
        if "hasName" in first_group:
            labels = {
                "#V#michael_witbrock": "Michael Witbrock",
                "#V#lu_yunli": "Lu Yunli",
                "#V#timothy_pistotti": "Timothy Pistotti",
            }
        else:
            labels = {
                "#V#michael_witbrock": "Works on neuro-symbolic agents and memory systems.",
                "#V#lu_yunli": "Collaborates on agent systems and applied AI.",
                "#V#timothy_pistotti": "PhD student working on related representation problems.",
            }
        for concept_id in subject_concept_ids:
            if concept_id in labels:
                rows[concept_id] = {"text": labels[concept_id]}
        return rows

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _FailingRAG(),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.override_current_user",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.override_current_organisation",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "name": "Michael Witbrock"},
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_preferred_text_for_concept",
        lambda concept_id, **_kwargs: {
            "text": "Works on neuro-symbolic agents and memory systems."
        }
        if concept_id == "#V#michael_witbrock"
        else None,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_preferred_texts_for_concepts",
        _preferred_rows,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_relation_service.find_relations_with_argument",
        lambda *_args, **_kwargs: {
            "relations": [
                {
                    "source_concept_id": "#V#michael_witbrock",
                    "predicate_concept_id": "#V#related_to",
                    "target_value": "#V#lu_yunli",
                },
                {
                    "source_concept_id": "#V#timothy_pistotti",
                    "predicate_concept_id": "#V#supervises_phd_student",
                    "target_value": "#V#michael_witbrock",
                },
            ]
        },
    )

    result = cat._get_related_concepts(
        concept_id="#V#michael_witbrock",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        seed_text="research interests and collaborators",
        top_k=3,
    )

    assert result["success"] is True
    assert result["fallback_used"] is True
    assert result["fallback_mode"] == "graph_text"
    assert result["fallback_reason"] == "rag_query_degraded:RuntimeError"
    assert result["count"] == 3
    assert "graph/text fallback" in result["summary"].lower()
    texts = [row["text"] for row in result["results"]]
    assert any("Lu Yunli" in text for text in texts)
    assert any("Timothy Pistotti" in text for text in texts)


def test_rag_list_collections_requires_namespace(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.delenv("VON_DEFAULT_NAMESPACE", raising=False)

    result = cat._rag_list_collections()
    assert result["success"] is False
    assert result["error"] == "namespace_required"


def test_rag_list_collections_includes_expected_collections():
    from src.backend.integrations.internal_mcp import catalogue as cat

    result = cat._rag_list_collections(namespace="#V#user@org")
    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["namespace_source"] == "request.namespace"
    assert result["user_concept_id"] == "#V#user"
    assert result["organisation_concept_id"] == "#V#org"

    assert result["provenance"]["item_kind"] == "rag_collection_list"
    assert result["provenance"]["user_concept_id"] == "#V#user"
    assert result["provenance"]["organisation_concept_id"] == "#V#org"
    assert result["collections"]
    collections = {c["collection"] for c in result["collections"]}
    assert "ka_sessions" in collections
    assert "chat_history_sessions" in collections
    assert "file_copy_concepts" in collections
    assert "turn_execution_records" in collections
    assert "experiment_runs" in collections
    assert "rag_documents" in collections
    assert "vontology_text_relations" in collections

    # Capability flags prevent the model inferring behaviour from missing tools.
    by_name = {c["collection"]: c for c in result["collections"]}
    assert by_name["ka_sessions"]["list_supported"] is True
    assert by_name["ka_sessions"]["get_supported"] is True
    assert by_name["file_copy_concepts"]["list_supported"] is True
    assert by_name["file_copy_concepts"]["get_supported"] is True
    assert by_name["turn_execution_records"]["list_supported"] is True
    assert by_name["turn_execution_records"]["get_supported"] is True
    assert by_name["experiment_runs"]["list_supported"] is True
    assert by_name["experiment_runs"]["get_supported"] is True
    assert by_name["rag_documents"]["list_supported"] is False
    assert by_name["rag_documents"]["get_supported"] is False

    # Text relations are discoverable via search once indexed.
    assert by_name["vontology_text_relations"]["search_supported"] is True
    assert by_name["vontology_text_relations"]["list_supported"] is True
    assert by_name["vontology_text_relations"]["get_supported"] is True


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


def test_rag_namespace_resolver_supports_user_only_path():
    from src.backend.integrations.internal_mcp import catalogue as cat

    report = cat._resolve_rag_namespace_from_kwargs({"user": {"id": "#V#user"}})

    assert report["namespace"] == "#V#user"
    assert report["namespace_source"] == "derived.user_org"
    assert report["namespace_resolution_note"] == "derived_from_user_org"
    assert report["namespace_mismatch"] is False
    assert report["user_concept_id"] == "#V#user"
    assert report["organisation_concept_id"] is None


def test_rag_namespace_resolver_supports_user_org_path():
    from src.backend.integrations.internal_mcp import catalogue as cat

    report = cat._resolve_rag_namespace_from_kwargs(
        {
            "user": {"id": "#V#user"},
            "organisation_concept_id": "#V#org",
        }
    )

    assert report["namespace"] == "#V#user@org"
    assert report["namespace_source"] == "derived.user_org"
    assert report["namespace_resolution_note"] == "derived_from_user_org"
    assert report["namespace_mismatch"] is False
    assert report["user_concept_id"] == "#V#user"
    assert report["organisation_concept_id"] == "#V#org"


def test_rag_namespace_resolver_derives_components_from_explicit_namespace():
    from src.backend.integrations.internal_mcp import catalogue as cat

    report = cat._resolve_rag_namespace_from_kwargs({"namespace": "#V#user@org"})

    assert report["namespace"] == "#V#user@org"
    assert report["namespace_source"] == "request.namespace"
    assert report["namespace_mismatch"] is False
    assert report["user_concept_id"] == "#V#user"
    assert report["organisation_concept_id"] == "#V#org"


def test_rag_list_indexed_fails_closed_on_namespace_mismatch(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"interaction_sessions": _Collection([])}),
    )

    result = cat._rag_list_indexed(
        namespace="#V#user",
        user={"id": "#V#user"},
        organisation_concept_id="#V#org",
    )

    assert result["success"] is False
    assert result["error"] == "namespace_mismatch"
    assert result["namespace"] is None
    assert result["provided_namespace"] == "#V#user"
    assert result["derived_namespace"] == "#V#user@org"
    assert result["user_concept_id"] == "#V#user"
    assert result["organisation_concept_id"] == "#V#org"
