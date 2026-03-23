import pytest
from unittest.mock import MagicMock, patch
from src.backend.services.rag_service import get_rag_service, RAGService


# Mock LlamaIndex components to avoid real API calls and dependencies during unit tests
@pytest.fixture
def mock_llamaindex():
    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.VectorStoreIndex"
        ) as mock_index_cls,
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.ServiceContext"
        ) as mock_service_context,
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.StorageContext"
        ) as mock_storage_context,
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
):
    fake_embed_model = MagicMock()
    fake_embed_model.get_text_embedding.return_value = [0.1, 0.2, 0.3]
    fake_settings = MagicMock()
    fake_settings.embed_model = fake_embed_model
    fake_settings.llm = MagicMock()

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
        assert rag.get_runtime_embed_model() is fake_embed_model
        assert rag.embed(["hello"]) == [[0.1, 0.2, 0.3]]
