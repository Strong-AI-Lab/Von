
import pytest
from unittest.mock import MagicMock, patch
from backend.services.rag_service import get_rag_service, RAGService

# Mock LlamaIndex components to avoid real API calls and dependencies during unit tests
@pytest.fixture
def mock_llamaindex():
    with patch("backend.services.rag_backends.llamaindex_backend.VectorStoreIndex") as mock_index_cls, \
         patch("backend.services.rag_backends.llamaindex_backend.ServiceContext") as mock_service_context, \
         patch("backend.services.rag_backends.llamaindex_backend.StorageContext") as mock_storage_context, \
         patch("backend.services.rag_backends.llamaindex_backend.load_index_from_storage") as mock_load:

        # Setup mock index instance
        mock_index_instance = MagicMock()
        mock_index_cls.from_documents.return_value = mock_index_instance
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
            "load": mock_load
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

    assert success == 1
    assert failed == 0
    # Verify from_documents was called (since index was None initially in mock)
    # Note: In our implementation, we try to load first. If load fails (mock_load returns None?), it creates.
    # In the fixture, mock_load returns an instance, so it should use that.
    # Wait, if load returns instance, upsert uses insert().

    # Let's adjust expectation based on fixture: load returns instance.
    mock_llamaindex["index"].insert.assert_called()

def test_query(mock_llamaindex):
    service = get_rag_service("llamaindex")
    results = service.query("test query")

    assert len(results) == 1
    assert results[0]["id"] == "doc1"
    assert results[0]["text"] == "content"
    assert results[0]["score"] == 0.9

def test_delete_documents(mock_llamaindex):
    service = get_rag_service("llamaindex")
    count = service.delete_documents(["doc1"])

    # Our implementation returns count if successful
    # It calls delete_ref_doc
    mock_llamaindex["index"].delete_ref_doc.assert_called_with("doc1", delete_from_docstore=True)
    assert count == 1
