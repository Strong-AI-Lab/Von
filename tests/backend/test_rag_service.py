import errno
import json
import os
import shutil

import pytest
from bson import ObjectId
from unittest.mock import MagicMock, patch
from src.backend.services.rag_service import get_rag_service


def test_base_rag_candidate_rechecks_subject_and_predicate_visibility(
    monkeypatch,
):
    from src.backend.services import scoped_rag_authority_service as authority

    relation_id = ObjectId()
    monkeypatch.setattr(
        authority.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "_id": relation_id,
                "subject_concept_id": "#V#subject",
                "predicate": "hasDescription",
            }
        ],
    )
    visible = {"#V#subject", "#V#hasDescription"}
    monkeypatch.setattr(
        authority,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids) & visible,
    )
    metadata = [
        {
            "type": "text_relation",
            "relation_id": str(relation_id),
            "subject_concept_id": "#V#subject",
            "predicate": "hasDescription",
        }
    ]

    assert authority.current_authorised_rag_candidate_keys(
        metadata,
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
    ) == {("text_relation", str(relation_id))}

    visible.remove("#V#hasDescription")
    assert (
        authority.current_authorised_rag_candidate_keys(
            metadata,
            user_concept_id="#V#user",
            organisation_concept_id="#V#org",
        )
        == set()
    )


def test_standalone_rag_candidate_uses_live_audience_status_and_revision(
    monkeypatch,
):
    import mongomock

    from src.backend.services import scoped_rag_authority_service as authority

    collection = mongomock.MongoClient()["von_test"]["scoped_knowledge_assertions"]
    collection.insert_one(
        {
            "assertion_id": "ska_raw",
            "assertion_form": "standalone_text",
            "assertion_revision": 3,
            "status": "asserted",
            "object_kind": "text",
            "scope": {"audience_keys": ["user:#V#user"]},
        }
    )
    monkeypatch.setattr(
        authority,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )

    def _concept_visibility_must_not_gate_raw_text(_concept_ids):
        raise AssertionError("raw assertion authority must not require a concept")

    monkeypatch.setattr(
        authority,
        "filter_accessible_concept_ids",
        _concept_visibility_must_not_gate_raw_text,
    )
    metadata = {
        "type": "scoped_knowledge_assertion",
        "assertion_id": "ska_raw",
        "assertion_form": "standalone_text",
        "assertion_revision": 3,
        "subject_concept_id": None,
        "predicate": None,
    }
    key = ("scoped_knowledge_assertion", "ska_raw")
    assert authority.current_authorised_rag_candidate_keys(
        [metadata],
        user_concept_id="#V#user",
        organisation_concept_id=None,
    ) == {key}
    assert (
        authority.current_authorised_rag_candidate_keys(
            [{**metadata, "assertion_revision": 2}],
            user_concept_id="#V#user",
            organisation_concept_id=None,
        )
        == set()
    )
    assert (
        authority.current_authorised_rag_candidate_keys(
            [metadata],
            user_concept_id="#V#other",
            organisation_concept_id=None,
        )
        == set()
    )
    collection.update_one(
        {"assertion_id": "ska_raw"},
        {"$set": {"status": "retracted", "assertion_revision": 4}},
    )
    assert (
        authority.current_authorised_rag_candidate_keys(
            [metadata],
            user_concept_id="#V#user",
            organisation_concept_id=None,
        )
        == set()
    )


# Mock LlamaIndex components to avoid real API calls and dependencies during unit tests
@pytest.fixture
def mock_llamaindex():
    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.VectorStoreIndex"
        ) as mock_index_cls,
        patch("src.backend.services.rag_backends.llamaindex_backend.ServiceContext"),
        patch("src.backend.services.rag_backends.llamaindex_backend.StorageContext"),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.load_index_from_storage"
        ) as mock_load,
    ):
        # Setup mock index instance
        mock_index_instance = MagicMock()
        mock_index_cls.from_documents.return_value = mock_index_instance
        # Crucially: make load_index_from_storage return the SAME mock instance
        mock_load.return_value = mock_index_instance

        # Setup retriever
        mock_retriever = MagicMock()
        mock_index_instance.as_retriever.return_value = mock_retriever

        # Setup nodes
        mock_node = MagicMock()
        mock_node.node.ref_doc_id = "doc1"
        mock_node.node.get_content.return_value = "content"
        mock_node.node.metadata = {"meta": "data"}
        mock_node.score = 0.9
        mock_retriever.retrieve.return_value = [mock_node]

        yield {
            "index_cls": mock_index_cls,
            "index": mock_index_instance,
            "load": mock_load,
        }


def _configure_rag_runtime_settings(
    monkeypatch,
    *,
    provider: str = "openai",
    model: str = "text-embedding-3-small",
    host: str | None = None,
) -> None:
    effective = {
        "provider": provider,
        "model": model,
    }
    if host:
        effective["host"] = host
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_embedder_setting",
        lambda *args, **kwargs: {
            "status": "resolved",
            "effective": effective,
            "selection_source": "explicit_setting",
            "reason": None,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_llm_setting",
        lambda *args, **kwargs: {
            "status": "disabled",
            "effective": None,
            "selection_source": "configured_disabled",
            "reason": "disabled_by_setting",
        },
    )


class _FakeSharingViolation(PermissionError):
    def __init__(self, path: str) -> None:
        super().__init__(
            13,
            "The process cannot access the file because it is being used by another process",
            path,
        )
        self.winerror = 32


def test_get_rag_service_llamaindex(mock_llamaindex):
    service = get_rag_service("llamaindex")
    assert service is not None
    # Check if it's the right class (by name, to avoid importing the class directly if lazy)
    assert type(service).__name__ == "LlamaIndexRAGService"


def test_upsert_documents(mock_llamaindex, monkeypatch, workspace_tmp_path):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    docs = [{"id": "doc1", "text": "content", "metadata": {"meta": "data"}}]

    success, failed = service.upsert_documents(docs)

    # Verify that documents were processed
    assert success == 1
    assert failed == 0


def test_standalone_assertion_embedding_uses_exact_body_without_metadata(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from llama_index.core.schema import MetadataMode

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    exact_text = "  Exact assertion body.\n"
    service.upsert_documents(
        [
            {
                "id": "scoped_assertion:ska_raw",
                "text": exact_text,
                "metadata": {
                    "type": "scoped_knowledge_assertion",
                    "assertion_form": "standalone_text",
                    "assertion_id": "ska_raw",
                    "assertion_revision": 1,
                    "audience_keys": ["user:#V#member"],
                    "user_id": "#V#member",
                },
            }
        ],
        namespace="#V#member",
    )

    document = mock_llamaindex["index_cls"].from_documents.call_args.args[0][0]
    assert document.get_content(metadata_mode=MetadataMode.EMBED) == exact_text
    assert document.metadata["assertion_id"] == "ska_raw"
    assert set(document.excluded_embed_metadata_keys) == set(document.metadata)
    assert set(document.excluded_llm_metadata_keys) == set(document.metadata)


def test_upsert_refreshes_stable_document_id_and_propagates_persist_failure(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    service.upsert_documents([{"id": "stable", "text": "revision one", "metadata": {}}])
    success, failed = service.upsert_documents(
        [{"id": "stable", "text": "revision two", "metadata": {}}]
    )

    assert (success, failed) == (1, 0)
    refreshed = mock_llamaindex["index"].refresh_ref_docs.call_args.args[0]
    assert [doc.doc_id for doc in refreshed] == ["stable"]
    assert [doc.text for doc in refreshed] == ["revision two"]
    mock_llamaindex["index"].insert_documents.assert_not_called()

    mock_llamaindex["index"].storage_context.persist.side_effect = RuntimeError(
        "disk unavailable"
    )
    with pytest.raises(RuntimeError, match="disk unavailable"):
        service.upsert_documents(
            [{"id": "stable", "text": "revision three", "metadata": {}}]
        )


def test_namespace_generation_reloads_stale_cache_before_next_mutation(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    persistence_dir = str(workspace_tmp_path / "rag_storage")
    writer = LlamaIndexRAGService(persistence_dir=persistence_dir)
    sibling = LlamaIndexRAGService(persistence_dir=persistence_dir)
    namespace = "#V#member@org"
    writer.upsert_documents(
        [{"id": "writer", "text": "writer revision one"}],
        namespace=namespace,
    )

    sibling_v1 = MagicMock()
    sibling_v2 = MagicMock()
    mock_llamaindex["load"].side_effect = [sibling_v1, sibling_v2]
    assert sibling._maybe_load_index(namespace) is sibling_v1
    first_generation = sibling._index_cache_generations[namespace]

    writer.upsert_documents(
        [{"id": "writer", "text": "writer revision two"}],
        namespace=namespace,
    )
    assert writer._index_cache_generations[namespace] != first_generation

    assert sibling.upsert_documents(
        [{"id": "sibling", "text": "sibling assertion"}],
        namespace=namespace,
    ) == (1, 0)
    sibling_v1.refresh_ref_docs.assert_not_called()
    refreshed = sibling_v2.refresh_ref_docs.call_args.args[0]
    assert [doc.doc_id for doc in refreshed] == ["sibling"]
    with open(
        sibling._namespace_metadata_path(namespace),
        encoding="utf-8",
    ) as handle:
        current_generation = json.load(handle)["index_generation"]
    assert sibling._index_cache_generations[namespace] == current_generation


def test_reset_evicts_sibling_persisted_cache_before_recreate(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    persistence_dir = str(workspace_tmp_path / "rag_storage")
    resetter = LlamaIndexRAGService(persistence_dir=persistence_dir)
    sibling = LlamaIndexRAGService(persistence_dir=persistence_dir)
    namespace = "#V#member@org"
    resetter.upsert_documents(
        [{"id": "old", "text": "old assertion"}],
        namespace=namespace,
    )
    assert sibling._maybe_load_index(namespace) is mock_llamaindex["index"]
    assert namespace in sibling._persisted_index_cache_namespaces

    resetter.reset_namespace(namespace)

    assert sibling.query("old", namespace=namespace) == []
    assert namespace not in sibling._indices
    assert sibling.upsert_documents(
        [{"id": "new", "text": "new assertion"}],
        namespace=namespace,
    ) == (1, 0)
    rebuilt_documents = mock_llamaindex["index_cls"].from_documents.call_args.args[0]
    assert [doc.doc_id for doc in rebuilt_documents] == ["new"]
    assert [doc.text for doc in rebuilt_documents] == ["new assertion"]


def test_initial_index_failure_does_not_publish_incomplete_namespace(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    final_dir = service._namespace_persist_dir("chat_history")

    def _partial_persist(*, persist_dir):
        os.makedirs(persist_dir, exist_ok=True)
        partial_path = os.path.join(persist_dir, "partial")
        with open(partial_path, "w", encoding="utf-8") as handle:
            handle.write("incomplete")
        raise RuntimeError("initial persist failed")

    mock_llamaindex["index"].storage_context.persist.side_effect = _partial_persist
    with pytest.raises(RuntimeError, match="initial persist failed"):
        service.upsert_documents([{"id": "stable", "text": "content"}])
    assert not os.path.exists(final_dir)

    mock_llamaindex["index"].storage_context.persist.side_effect = None
    assert service.upsert_documents([{"id": "stable", "text": "content"}]) == (1, 0)
    assert os.path.isfile(os.path.join(final_dir, "index_metadata.json"))


def test_query(mock_llamaindex):
    service = get_rag_service("llamaindex")
    results = service.query("test query")

    # The retriever returns results
    assert len(results) >= 0
    if results:
        assert "id" in results[0]
        assert "text" in results[0]
        assert "score" in results[0]


def test_query_honours_workflow_capability_retrieval_candidate_limit(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    rag.upsert_documents(
        [
            {
                "id": "workflow_capability:#V#test_workflow",
                "text": "Capability text for retrieval candidate testing",
                "metadata": {
                    "workflow_id": "#V#test_workflow",
                    "type": "workflow_capability",
                },
            }
        ],
        namespace="workflow_capabilities",
    )

    rag.query(
        "test workflow capability",
        top_k=3,
        namespace="workflow_capabilities",
        permissions_context={
            "type": "workflow_capability",
            "retrieval_candidate_limit": 7,
        },
    )

    mock_llamaindex["index"].as_retriever.assert_called_with(similarity_top_k=7)


def test_concepts_mode_includes_current_scoped_assertion_candidates(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )
    from src.backend.services import scoped_rag_authority_service

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    rag.upsert_documents(
        [
            {
                "id": "scoped_assertion:ska_1",
                "text": "Scoped programme context",
                "metadata": {"type": "scoped_knowledge_assertion"},
            }
        ],
        namespace="#V#user@org",
    )
    node = mock_llamaindex["index"].as_retriever.return_value.retrieve.return_value[0]
    node.node.ref_doc_id = "scoped_assertion:ska_1"
    node.node.metadata = {
        "type": "scoped_knowledge_assertion",
        "assertion_id": "ska_1",
        "subject_concept_id": "#V#subject",
        "predicate": "hasDescription",
        "user_id": "#V#user",
        # User-scoped canonical knowledge remains valid in an organisation
        # turn even though its assertion audience is not organisation-scoped.
        "organisation_concept_id": None,
    }
    authority_calls = []
    monkeypatch.setattr(
        scoped_rag_authority_service,
        "current_authorised_rag_candidate_keys",
        lambda rows, **kwargs: (
            authority_calls.append((rows, kwargs))
            or {("scoped_knowledge_assertion", "ska_1")}
        ),
    )

    results = rag.query(
        "programme context",
        namespace="#V#user@org",
        permissions_context={
            "mode": "concepts",
            "user_id": "#V#user",
            "organisation_concept_id": "#V#org",
        },
    )

    assert [row["id"] for row in results] == ["scoped_assertion:ska_1"]
    assert authority_calls == [
        (
            [node.node.metadata],
            {
                "user_concept_id": "#V#user",
                "organisation_concept_id": "#V#org",
            },
        )
    ]


def test_concepts_mode_returns_live_standalone_user_assertion_in_org_turn(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    import mongomock

    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services import scoped_rag_authority_service
    from src.backend.services.knowledge_assertion_rag_service import (
        build_standalone_text_assertion_rag_document,
    )
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    assertion = {
        "assertion_id": "ska_raw_live",
        "assertion_form": "standalone_text",
        "assertion_revision": 2,
        "status": "asserted",
        "object_kind": "text",
        "object_text": {"text": "Exact raw evidence.", "language": "en-NZ"},
        "scope": {
            "mode": "user",
            "user_concept_id": "#V#user",
            "organisation_concept_id": None,
            "audience_keys": ["user:#V#user"],
        },
        "assertion_context": {
            "context_id": "intake:user:#V#user",
            "selection": "implicit",
        },
    }
    collection = mongomock.MongoClient()["von_test"]["scoped_knowledge_assertions"]
    collection.insert_one(assertion)
    monkeypatch.setattr(
        scoped_rag_authority_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )

    def _concept_visibility_must_not_run(_concept_ids):
        raise AssertionError("linkless text authority has no concept dependency")

    monkeypatch.setattr(
        scoped_rag_authority_service,
        "filter_accessible_concept_ids",
        _concept_visibility_must_not_run,
    )
    rag_doc = build_standalone_text_assertion_rag_document(assertion)
    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    rag.upsert_documents([rag_doc], namespace="#V#user@org")
    node = mock_llamaindex["index"].as_retriever.return_value.retrieve.return_value[0]
    node.node.ref_doc_id = rag_doc["id"]
    node.node.metadata = rag_doc["metadata"]
    node.node.get_content.return_value = rag_doc["text"]

    results = rag.query(
        "raw evidence",
        namespace="#V#user@org",
        permissions_context={
            "mode": "concepts",
            "user_id": "#V#user",
            "organisation_concept_id": "#V#org",
        },
    )

    assert [(row["id"], row["text"]) for row in results] == [
        ("scoped_assertion:ska_raw_live", "Exact raw evidence.")
    ]


def test_scoped_rag_hit_is_live_filtered_after_assertion_revocation(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    import mongomock

    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )
    from src.backend.services import scoped_rag_authority_service

    collection = mongomock.MongoClient()["von_test"]["scoped_knowledge_assertions"]
    collection.insert_one(
        {
            "assertion_id": "ska_live",
            "status": "asserted",
            "object_kind": "text",
            "scope": {"audience_keys": ["org:#V#org"]},
            "subject_concept_id": "#V#subject",
            "predicate": "hasDescription",
        }
    )
    monkeypatch.setattr(
        scoped_rag_authority_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    visible_concepts = {"#V#subject", "#V#hasDescription"}
    monkeypatch.setattr(
        scoped_rag_authority_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids) & visible_concepts,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    rag.upsert_documents(
        [
            {
                "id": "scoped_assertion:ska_live",
                "text": "Scoped programme context",
                "metadata": {"type": "scoped_knowledge_assertion"},
            }
        ],
        namespace="#V#user@org",
    )
    node = mock_llamaindex["index"].as_retriever.return_value.retrieve.return_value[0]
    node.node.ref_doc_id = "scoped_assertion:ska_live"
    node.node.metadata = {
        "type": "scoped_knowledge_assertion",
        "assertion_id": "ska_live",
        "subject_concept_id": "#V#subject",
        "predicate": "hasDescription",
        "user_id": "#V#user",
        "organisation_concept_id": "#V#org",
    }
    permissions = {
        "mode": "concepts",
        "user_id": "#V#user",
        "organisation_concept_id": "#V#org",
    }

    assert (
        len(
            rag.query(
                "programme context",
                namespace="#V#user@org",
                permissions_context=permissions,
            )
        )
        == 1
    )

    collection.update_one(
        {"assertion_id": "ska_live"},
        {"$set": {"status": "retracted"}},
    )
    assert (
        rag.query(
            "programme context",
            namespace="#V#user@org",
            permissions_context=permissions,
        )
        == []
    )

    collection.update_one(
        {"assertion_id": "ska_live"},
        {"$set": {"status": "asserted"}},
    )
    visible_concepts.remove("#V#hasDescription")
    assert (
        rag.query(
            "programme context",
            namespace="#V#user@org",
            permissions_context=permissions,
        )
        == []
    )

    visible_concepts.add("#V#hasDescription")
    collection.update_one(
        {"assertion_id": "ska_live"},
        {"$set": {"scope.audience_keys": ["org:#V#other"]}},
    )
    assert (
        rag.query(
            "programme context",
            namespace="#V#user@org",
            permissions_context=permissions,
        )
        == []
    )

    collection.update_one(
        {"assertion_id": "ska_live"},
        {"$set": {"scope.audience_keys": ["org:#V#org"]}},
    )
    visible_concepts.remove("#V#subject")
    assert (
        rag.query(
            "programme context",
            namespace="#V#user@org",
            permissions_context=permissions,
        )
        == []
    )


def test_delete_documents(mock_llamaindex):
    service = get_rag_service("llamaindex")
    count = service.delete_documents(["doc1"])

    # Verify that delete operation completed
    assert count >= 0


def test_delete_documents_propagates_backend_failure(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    service.upsert_documents([{"id": "doc1", "text": "content"}])
    mock_llamaindex["index"].delete_ref_doc.side_effect = RuntimeError("delete failed")

    with pytest.raises(RuntimeError, match="delete failed"):
        service.delete_documents(["doc1"])


def test_delete_publishes_new_namespace_generation(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    service.upsert_documents([{"id": "doc1", "text": "content"}])
    initial_generation = service._index_cache_generations["chat_history"]

    assert service.delete_documents(["doc1"]) == 1
    with open(
        service._namespace_metadata_path("chat_history"),
        encoding="utf-8",
    ) as handle:
        deleted_generation = json.load(handle)["index_generation"]
    assert deleted_generation != initial_generation
    assert service._index_cache_generations["chat_history"] == deleted_generation


def test_delete_does_not_report_absence_when_existing_index_cannot_load(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    service = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    persist_dir = (
        workspace_tmp_path
        / "rag_storage"
        / "namespaces"
        / service._namespace_dirname("chat_history")
    )
    persist_dir.mkdir(parents=True, exist_ok=True)
    (persist_dir / "unreadable_state").write_text("present", encoding="utf-8")
    service._write_namespace_metadata("chat_history")
    mock_llamaindex["load"].side_effect = RuntimeError("load failed")

    with pytest.raises(RuntimeError, match="could not be loaded"):
        service.delete_documents(["doc1"])


def test_llamaindex_servicecontext_deprecation_falls_back_to_settings(
    workspace_tmp_path,
    monkeypatch,
):
    fake_settings = MagicMock()
    fake_settings.embed_model = None
    fake_settings.llm = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_embedder_setting",
        lambda *args, **kwargs: {
            "status": "resolved",
            "effective": {"provider": "openai", "model": "text-embedding-3-small"},
            "selection_source": "explicit_setting",
            "reason": None,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_llm_setting",
        lambda *args, **kwargs: {
            "status": "disabled",
            "effective": None,
            "selection_source": "configured_disabled",
            "reason": "disabled_by_setting",
        },
    )

    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
            side_effect=ValueError(
                "ServiceContext is deprecated. Use llama_index.settings.Settings instead."
            ),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.Settings",
            fake_settings,
        ),
    ):
        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(
            persistence_dir=str(workspace_tmp_path / "rag_storage")
        )

        assert rag.service_context is None
        assert rag.llamaindex_settings is fake_settings
        assert rag.get_runtime_embed_model() is fake_settings.embed_model
        assert rag.get_runtime_configuration_summary()["embedding_signature"][
            "model"
        ] == ("text-embedding-3-small")


def test_llamaindex_runtime_configuration_tracks_embedding_signature_and_mismatch(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    summary = rag.get_runtime_configuration_summary()

    assert summary["embedding_signature"] == {
        "schema_version": "rag_component_signature.v1",
        "kind": "embedder",
        "provider": "openai",
        "model": "text-embedding-3-small",
        "host": None,
    }
    assert summary["llm_resolution"]["status"] == "disabled"

    success, failed = rag.upsert_documents(
        [{"id": "doc1", "text": "content", "metadata": {"meta": "data"}}],
        namespace="workflow_capabilities",
    )

    assert success == 1
    assert failed == 0

    metadata_path = workspace_tmp_path / "rag_storage" / "namespaces"
    metadata_files = list(metadata_path.rglob("index_metadata.json"))
    assert metadata_files
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["embedding_signature"]["model"] == "text-embedding-3-small"

    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_embedder_setting",
        lambda *args, **kwargs: {
            "status": "resolved",
            "effective": {
                "provider": "ollama",
                "model": "nomic-embed-text",
                "host": "http://localhost:11434",
            },
            "selection_source": "explicit_setting",
            "reason": None,
        },
    )
    rag.invalidate_runtime_configuration_cache()

    state = rag.get_namespace_runtime_state("workflow_capabilities")

    assert state["compatible"] is False
    assert state["status"] == "embedding_signature_mismatch"
    assert state["current_embedding_signature"]["model"] == "nomic-embed-text"


def test_llamaindex_upsert_rejects_unresolved_embedder_before_index_mutation(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_embedder_setting",
        lambda *args, **kwargs: {
            "status": "unresolved",
            "effective": None,
            "selection_source": "unavailable",
            "reason": "embedding_provider_unavailable",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_llm_setting",
        lambda *args, **kwargs: {
            "status": "disabled",
            "effective": None,
            "selection_source": "configured_disabled",
            "reason": "disabled_by_setting",
        },
    )

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    # LlamaIndex may expose a truthy MockEmbedding when no real embedder was
    # resolved. Mutation authority comes from the resolved configuration, not
    # from that compatibility fallback object.
    assert rag.llamaindex_settings.embed_model is not None

    with pytest.raises(RuntimeError, match="embedding_provider_unavailable"):
        rag.upsert_documents(
            [{"id": "doc1", "text": "content", "metadata": {}}],
            namespace="#V#user@org",
        )

    mock_llamaindex["index_cls"].from_documents.assert_not_called()
    assert not (workspace_tmp_path / "rag_storage" / "namespaces").exists()


@pytest.mark.parametrize(
    "persisted_signature",
    ["different", "missing"],
)
def test_llamaindex_upsert_requires_explicit_rebuild_for_incompatible_signature(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
    persisted_signature,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    rag.upsert_documents(
        [{"id": "doc1", "text": "first", "metadata": {}}],
        namespace="#V#user@org",
    )
    metadata_path = next(
        (workspace_tmp_path / "rag_storage").rglob("index_metadata.json")
    )
    if persisted_signature == "missing":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["embedding_signature"] = None
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    else:
        monkeypatch.setattr(
            "src.backend.services.settings_service.resolve_rag_embedder_setting",
            lambda *args, **kwargs: {
                "status": "resolved",
                "effective": {
                    "provider": "ollama",
                    "model": "nomic-embed-text",
                    "host": "http://localhost:11434",
                },
                "selection_source": "explicit_setting",
                "reason": None,
            },
        )
        rag.invalidate_runtime_configuration_cache()
    metadata_before = metadata_path.read_bytes()

    with pytest.raises(RuntimeError, match="rebuild_required"):
        rag.upsert_documents(
            [{"id": "doc2", "text": "second", "metadata": {}}],
            namespace="#V#user@org",
        )

    assert metadata_path.read_bytes() == metadata_before
    assert mock_llamaindex["index_cls"].from_documents.call_count == 1
    mock_llamaindex["index"].insert_documents.assert_not_called()


def test_llamaindex_metadata_uses_embedder_captured_before_index_creation(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends import llamaindex_backend as backend

    backend.ServiceContext.from_defaults.side_effect = ValueError(
        "ServiceContext is deprecated. Use llama_index.settings.Settings instead."
    )
    rag = backend.LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )

    def _create_index_after_configuration_change(*args, **kwargs):
        monkeypatch.setattr(
            "src.backend.services.settings_service.resolve_rag_embedder_setting",
            lambda *inner_args, **inner_kwargs: {
                "status": "resolved",
                "effective": {
                    "provider": "ollama",
                    "model": "nomic-embed-text",
                    "host": "http://localhost:11434",
                },
                "selection_source": "explicit_setting",
                "reason": None,
            },
        )
        rag.invalidate_runtime_configuration_cache()
        return mock_llamaindex["index"]

    mock_llamaindex[
        "index_cls"
    ].from_documents.side_effect = _create_index_after_configuration_change

    rag.upsert_documents(
        [{"id": "doc1", "text": "content", "metadata": {}}],
        namespace="#V#user@org",
    )

    call_kwargs = mock_llamaindex["index_cls"].from_documents.call_args.kwargs
    assert call_kwargs["embed_model"].model_name == "text-embedding-3-small"
    metadata_path = next(
        (workspace_tmp_path / "rag_storage").rglob("index_metadata.json")
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["embedding_signature"]["model"] == "text-embedding-3-small"

    state = rag.get_namespace_runtime_state("#V#user@org")
    assert state["status"] == "embedding_signature_mismatch"
    assert state["current_embedding_signature"]["model"] == "nomic-embed-text"


def test_llamaindex_metadata_write_retries_transient_sharing_violation(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    namespace = "workflow_capabilities"
    metadata_path = workspace_tmp_path / "rag_storage" / "namespaces"

    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def flaky_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        if len(replace_calls) == 1:
            raise _FakeSharingViolation(dst)
        real_replace(src, dst)

    monkeypatch.setattr(
        "src.backend.services.rag_backends.llamaindex_backend.os.replace",
        flaky_replace,
    )

    rag._write_namespace_metadata(namespace)

    metadata_files = list(metadata_path.rglob("index_metadata.json"))
    assert len(replace_calls) == 2
    assert metadata_files
    payload = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert payload["namespace"] == namespace
    assert payload["embedding_signature"]["model"] == "text-embedding-3-small"


def test_namespace_process_lock_windows_retries_only_contention(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends import llamaindex_backend as backend

    rag = backend.LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    fake_msvcrt = MagicMock()
    fake_msvcrt.LK_NBLCK = 1
    fake_msvcrt.LK_UNLCK = 2
    fake_msvcrt.locking.side_effect = [
        OSError(errno.EACCES, "lock busy"),
        None,
        None,
    ]
    monkeypatch.setattr(backend, "_fcntl", None)
    monkeypatch.setattr(backend, "_msvcrt", fake_msvcrt)
    monkeypatch.setattr(backend.time, "sleep", lambda _seconds: None)

    with rag._namespace_process_lock("#V#member@org"):
        pass

    assert [call.args[1] for call in fake_msvcrt.locking.call_args_list] == [1, 1, 2]


def test_namespace_process_lock_windows_propagates_non_contention_error(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    _configure_rag_runtime_settings(monkeypatch)
    from src.backend.services.rag_backends import llamaindex_backend as backend

    rag = backend.LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    fake_msvcrt = MagicMock()
    fake_msvcrt.LK_NBLCK = 1
    fake_msvcrt.LK_UNLCK = 2
    fake_msvcrt.locking.side_effect = OSError(errno.EINVAL, "unsupported lock")
    monkeypatch.setattr(backend, "_fcntl", None)
    monkeypatch.setattr(backend, "_msvcrt", fake_msvcrt)

    with pytest.raises(OSError, match="unsupported lock"):
        with rag._namespace_process_lock("#V#member@org"):
            pass
    assert fake_msvcrt.locking.call_count == 1


def test_llamaindex_reset_namespace_retries_transient_sharing_violation(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    namespace = "workflow_capabilities"
    persist_dir = (
        workspace_tmp_path
        / "rag_storage"
        / "namespaces"
        / rag._namespace_dirname(namespace)
    )
    persist_dir.mkdir(parents=True, exist_ok=True)
    (persist_dir / "index_metadata.json").write_text("{}", encoding="utf-8")

    rmtree_calls: list[str] = []
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path: str, *args, **kwargs) -> None:
        rmtree_calls.append(str(path))
        if len(rmtree_calls) == 1 and not kwargs.get("ignore_errors"):
            raise _FakeSharingViolation(str(path))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "src.backend.services.rag_backends.llamaindex_backend.shutil.rmtree",
        flaky_rmtree,
    )

    rag.reset_namespace(namespace)

    assert len(rmtree_calls) == 2
    assert not persist_dir.exists()


def test_namespace_runtime_state_is_non_blocking_under_lock_contention(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    """JVNAUTOSCI-2124: diagnostic reads must not stall behind a rebuild.

    Holds the per-namespace write lock from a worker thread and asserts that
    the diagnostic ``get_namespace_runtime_state`` returns promptly with a
    ``rebuild_in_progress`` snapshot rather than blocking the caller.
    """

    import threading
    import time

    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    namespace = "workflow_capabilities"

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    namespace_lock = rag._get_namespace_lock(namespace)

    def _hold_lock() -> None:
        with namespace_lock:
            holder_acquired.set()
            # Simulate a slow rebuild holding the lock.
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    try:
        assert holder_acquired.wait(timeout=2.0), "holder thread failed to acquire lock"
        start = time.perf_counter()
        state = rag.get_namespace_runtime_state(namespace)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.5, (
            f"get_namespace_runtime_state blocked for {elapsed:.3f}s "
            "under lock contention; must return non-blocking snapshot"
        )
        assert state["status"] == "rebuild_in_progress"
        assert state["compatible"] is False
        assert state.get("lock_contended") is True
    finally:
        holder_release.set()
        holder.join(timeout=2.0)


def test_namespace_runtime_state_strict_blocks_until_lock_released(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    """JVNAUTOSCI-2124: callers can opt back into strict serialised reads."""

    import threading
    import time

    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(persistence_dir=str(workspace_tmp_path / "rag_storage"))
    namespace = "workflow_capabilities"

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    namespace_lock = rag._get_namespace_lock(namespace)

    def _hold_lock() -> None:
        with namespace_lock:
            holder_acquired.set()
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()

    result_holder: dict = {}

    def _strict_probe() -> None:
        result_holder["state"] = rag.get_namespace_runtime_state(
            namespace, non_blocking=False
        )
        result_holder["finished_at"] = time.perf_counter()

    try:
        assert holder_acquired.wait(timeout=2.0)
        probe = threading.Thread(target=_strict_probe, daemon=True)
        probe.start()
        # Strict probe must not have completed while the holder still has the lock.
        time.sleep(0.2)
        assert "state" not in result_holder, (
            "strict (non_blocking=False) probe returned while another thread "
            "still held the namespace lock"
        )
        holder_release.set()
        probe.join(timeout=2.0)
        assert "state" in result_holder, "strict probe never returned"
        assert result_holder["state"].get("lock_contended") is False
    finally:
        holder_release.set()
        holder.join(timeout=2.0)
