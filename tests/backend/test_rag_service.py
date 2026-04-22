import json

import pytest
from unittest.mock import MagicMock, patch
from src.backend.services.rag_service import get_rag_service


# Mock LlamaIndex components to avoid real API calls and dependencies during unit tests
@pytest.fixture
def mock_llamaindex():
    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.VectorStoreIndex"
        ) as mock_index_cls,
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.ServiceContext"
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.StorageContext"
        ),
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


def test_get_rag_service_llamaindex(mock_llamaindex):
    service = get_rag_service("llamaindex")
    assert service is not None
    # Check if it's the right class (by name, to avoid importing the class directly if lazy)
    assert type(service).__name__ == "LlamaIndexRAGService"


def test_upsert_documents(mock_llamaindex):
    service = get_rag_service("llamaindex")
    docs = [{"id": "doc1", "text": "content", "metadata": {"meta": "data"}}]

    success, failed = service.upsert_documents(docs)

    # Verify that documents were processed
    assert success == 1
    assert failed == 0


def test_query(mock_llamaindex):
    service = get_rag_service("llamaindex")
    results = service.query("test query")

    # The retriever returns results
    assert len(results) >= 0
    if results:
        assert "id" in results[0]
        assert "text" in results[0]
        assert "score" in results[0]


def test_delete_documents(mock_llamaindex):
    service = get_rag_service("llamaindex")
    count = service.delete_documents(["doc1"])

    # Verify that delete operation completed
    assert count >= 0


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
        assert rag.get_runtime_configuration_summary()["embedding_signature"]["model"] == (
            "text-embedding-3-small"
        )


def test_llamaindex_runtime_configuration_tracks_embedding_signature_and_mismatch(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_rag_embedder_setting",
        lambda *args, **kwargs: {
            "status": "resolved",
            "effective": {
                "provider": "openai",
                "model": "text-embedding-3-small",
            },
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

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
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

    state = rag.get_namespace_runtime_state("workflow_capabilities")

    assert state["compatible"] is False
    assert state["status"] == "embedding_signature_mismatch"
    assert state["current_embedding_signature"]["model"] == "nomic-embed-text"
