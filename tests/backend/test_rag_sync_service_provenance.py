class _StubRAGService:
    def __init__(self):
        self.upserts = []

    def upsert_documents(self, docs, namespace):
        self.upserts.append({"docs": docs, "namespace": namespace})
        return len(docs), 0


def test_sync_to_chat_store_emits_namespace_component_provenance(monkeypatch):
    from src.backend.services import rag_sync_service

    rag_stub = _StubRAGService()
    observations = []

    monkeypatch.setattr(
        "src.backend.services.rag_sync_service.get_rag_service",
        lambda: rag_stub,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_sync_service.collect_indexed_sessions",
        lambda limit=1000: [
            {
                "id": "s1",
                "namespace": "#V#user@org",
                "text": "hello world",
                "metadata": {"source": "interaction_session"},
            }
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.rag_sync_service._record_namespace_sync_observation",
        lambda **kwargs: observations.append(kwargs),
    )

    result = rag_sync_service.sync_to_chat_store(
        namespace="#V#user@org",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
    )

    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["namespace_source"] == "request.namespace"
    assert result["user_concept_id"] == "#V#user"
    assert result["organisation_concept_id"] == "#V#org"
    assert result["namespace_mismatch"] is False
    assert result["mismatch_count"] == 0

    assert rag_stub.upserts
    upsert = rag_stub.upserts[0]
    assert upsert["namespace"] == "#V#user@org"
    metadata = upsert["docs"][0]["metadata"]
    assert metadata["namespace"] == "#V#user@org"
    assert metadata["namespace_source"] == "request.namespace"
    assert metadata["user_concept_id"] == "#V#user"
    assert metadata["organisation_concept_id"] == "#V#org"

    assert observations
    assert observations[0]["mismatch_detected"] is False


def test_sync_to_chat_store_reports_namespace_mismatch_without_failing(monkeypatch):
    from src.backend.services import rag_sync_service

    rag_stub = _StubRAGService()
    observations = []

    monkeypatch.setattr(
        "src.backend.services.rag_sync_service.get_rag_service",
        lambda: rag_stub,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_sync_service.collect_indexed_sessions",
        lambda limit=1000: [
            {
                "id": "s-mismatch",
                "namespace": "#V#user@other_org",
                "text": "hello world",
                "metadata": {"source": "interaction_session"},
            }
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.rag_sync_service._record_namespace_sync_observation",
        lambda **kwargs: observations.append(kwargs),
    )

    result = rag_sync_service.sync_to_chat_store(
        namespace="#V#user@org",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
    )

    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["namespace_source"] == "request.namespace"
    assert result["namespace_mismatch"] is True
    assert result["mismatch_count"] == 1
    assert result["mismatch_session_ids"] == ["s-mismatch"]

    assert rag_stub.upserts
    assert rag_stub.upserts[0]["namespace"] == "#V#user@org"

    assert observations
    assert observations[0]["mismatch_detected"] is True
