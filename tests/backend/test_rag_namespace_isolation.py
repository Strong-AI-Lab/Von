import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def tmp_rag_storage(workspace_tmp_path: Path):
    storage = workspace_tmp_path / "rag_storage"
    storage.mkdir(parents=True, exist_ok=True)
    yield storage
    if storage.exists():
        shutil.rmtree(storage, ignore_errors=True)


def test_llamaindex_backend_uses_separate_persist_dirs_per_namespace(
    tmp_rag_storage: Path,
):
    # Import inside test so patches apply cleanly.
    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.Document",
            side_effect=lambda *args, **kwargs: MagicMock(metadata={}),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.VectorStoreIndex.from_documents"
        ) as mock_from_documents,
    ):
        index_a = MagicMock()
        index_a.storage_context.persist = MagicMock()
        index_b = MagicMock()
        index_b.storage_context.persist = MagicMock()
        mock_from_documents.side_effect = [index_a, index_b]

        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(persistence_dir=str(tmp_rag_storage))

        rag.upsert_documents(
            [{"id": "doc_a", "text": "A", "metadata": {}}], namespace="#V#user_a"
        )
        rag.upsert_documents(
            [{"id": "doc_b", "text": "B", "metadata": {}}], namespace="#V#user_b"
        )

        assert mock_from_documents.call_count == 2

        # Persist should have been called in different directories under .../namespaces/
        persist_a = index_a.storage_context.persist.call_args.kwargs.get("persist_dir")
        persist_b = index_b.storage_context.persist.call_args.kwargs.get("persist_dir")

        assert persist_a != persist_b
        assert str(tmp_rag_storage) in str(persist_a)
        assert str(tmp_rag_storage) in str(persist_b)
        assert "namespaces" in str(persist_a)
        assert "namespaces" in str(persist_b)


def test_llamaindex_backend_stamps_namespace_into_metadata(tmp_rag_storage: Path):
    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.VectorStoreIndex.from_documents",
            return_value=MagicMock(storage_context=MagicMock(persist=MagicMock())),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.Document"
        ) as mock_doc,
    ):
        doc_instance = MagicMock()
        doc_instance.metadata = {}
        mock_doc.return_value = doc_instance

        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(persistence_dir=str(tmp_rag_storage))

        rag.upsert_documents(
            [{"id": "doc_a", "text": "hello", "metadata": {}}],
            namespace="#V#user_a@org",
        )

        assert doc_instance.metadata.get("namespace") == "#V#user_a@org"


def test_llamaindex_backend_omits_legacy_service_context_kwargs_when_deprecated(
    tmp_rag_storage: Path,
):
    with (
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
            side_effect=ValueError(
                "ServiceContext is deprecated. Use llama_index.settings.Settings instead."
            ),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.Document",
            side_effect=lambda *args, **kwargs: MagicMock(metadata={}),
        ),
        patch(
            "src.backend.services.rag_backends.llamaindex_backend.VectorStoreIndex.from_documents"
        ) as mock_from_documents,
    ):
        mock_index = MagicMock()
        mock_index.storage_context.persist = MagicMock()
        mock_from_documents.return_value = mock_index

        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(persistence_dir=str(tmp_rag_storage))
        rag.upsert_documents(
            [{"id": "doc_a", "text": "hello", "metadata": {}}],
            namespace="#V#user_a",
        )

        assert "service_context" not in mock_from_documents.call_args.kwargs
