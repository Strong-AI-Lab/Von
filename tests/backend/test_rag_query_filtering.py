from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_retrieved_node(
    *, metadata: dict, content: str = "content", score: float = 0.5
):
    inner = MagicMock()
    inner.metadata = metadata
    inner.ref_doc_id = metadata.get("doc_id") or "ref"
    inner.node_id = "node"
    inner.get_content.return_value = content

    wrapper = MagicMock()
    wrapper.node = inner
    wrapper.score = score
    return wrapper


def test_llamaindex_backend_filters_by_type_and_predicate(workspace_tmp_path: Path):
    with patch(
        "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
        return_value=MagicMock(),
    ):
        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(
            persistence_dir=str(workspace_tmp_path / "rag_storage")
        )

        nodes = [
            _make_retrieved_node(
                metadata={
                    "type": "chat_message",
                    "user_id": "#V#user",
                    "organisation_concept_id": "#V#org",
                },
                content="chat",
            ),
            _make_retrieved_node(
                metadata={
                    "type": "text_relation",
                    "source": "vontology_text_relation",
                    "predicate": "hasDescription",
                    "user_id": "#V#user",
                    "organisation_concept_id": "#V#org",
                },
                content="desc",
            ),
            _make_retrieved_node(
                metadata={
                    "type": "text_relation",
                    "source": "vontology_text_relation",
                    "predicate": "hasNote",
                    "user_id": "#V#user",
                    "organisation_concept_id": "#V#org",
                },
                content="note",
            ),
        ]

        fake_retriever = MagicMock()
        fake_retriever.retrieve.return_value = nodes

        fake_index = MagicMock()
        fake_index.as_retriever.return_value = fake_retriever

        rag._indices["#V#user@org"] = fake_index

        results = rag.query(
            "causal",
            namespace="#V#user@org",
            top_k=10,
            permissions_context={
                "user_id": "#V#user",
                "organisation_concept_id": "#V#org",
                "type": "text_relation",
                "predicate": "hasDescription",
            },
        )

        assert [r["text"] for r in results] == ["desc"]


def test_llamaindex_backend_filters_by_mode_chat(workspace_tmp_path: Path):
    with patch(
        "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
        return_value=MagicMock(),
    ):
        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(
            persistence_dir=str(workspace_tmp_path / "rag_storage")
        )

        nodes = [
            _make_retrieved_node(
                metadata={
                    "type": "chat_message",
                    "user_id": "#V#user",
                    "organisation_concept_id": "#V#org",
                },
                content="chat",
            ),
            _make_retrieved_node(
                metadata={
                    "type": "text_relation",
                    "source": "vontology_text_relation",
                    "predicate": "hasDescription",
                    "user_id": "#V#user",
                    "organisation_concept_id": "#V#org",
                },
                content="desc",
            ),
        ]

        fake_retriever = MagicMock()
        fake_retriever.retrieve.return_value = nodes

        fake_index = MagicMock()
        fake_index.as_retriever.return_value = fake_retriever

        rag._indices["#V#user@org"] = fake_index

        results = rag.query(
            "hello",
            namespace="#V#user@org",
            top_k=10,
            permissions_context={
                "user_id": "#V#user",
                "organisation_concept_id": "#V#org",
                "mode": "chat",
            },
        )

        assert [r["text"] for r in results] == ["chat"]


def test_llamaindex_backend_query_temporarily_bounds_embed_model(workspace_tmp_path: Path):
    with patch(
        "src.backend.services.rag_backends.llamaindex_backend.ServiceContext.from_defaults",
        return_value=MagicMock(),
    ):
        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        rag = LlamaIndexRAGService(
            persistence_dir=str(workspace_tmp_path / "rag_storage")
        )

        fake_embed_model = MagicMock()
        fake_embed_model.max_retries = 10
        fake_embed_model.timeout = 60.0
        fake_embed_model._client = object()
        fake_embed_model._aclient = object()
        original_client = fake_embed_model._client
        original_aclient = fake_embed_model._aclient
        rag.service_context.embed_model = fake_embed_model

        fake_retriever = MagicMock()

        def _retrieve(_query: str):
            assert fake_embed_model.max_retries == 0
            assert fake_embed_model.timeout == 8.0
            assert fake_embed_model._client is None
            assert fake_embed_model._aclient is None
            return []

        fake_retriever.retrieve.side_effect = _retrieve

        fake_index = MagicMock()
        fake_index.as_retriever.return_value = fake_retriever
        rag._indices["#V#user@org"] = fake_index

        results = rag.query("hello", namespace="#V#user@org", top_k=5)

        assert results == []
        assert fake_embed_model.max_retries == 10
        assert fake_embed_model.timeout == 60.0
        assert fake_embed_model._client is original_client
        assert fake_embed_model._aclient is original_aclient
