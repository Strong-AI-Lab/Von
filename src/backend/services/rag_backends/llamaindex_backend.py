"""
LlamaIndex RAG Backend Implementation (Apache-2.0)

This module implements the RAGService protocol using LlamaIndex.
It is imported lazily to avoid hard dependencies.
"""

import os
from typing import Iterable, Dict, Any, Optional, List, Tuple
from ..rag_service import RAGService, RAGBackendUnavailable

try:
    from llama_index import VectorStoreIndex, Document, ServiceContext, StorageContext, load_index_from_storage
    # from llama_index.llms import OpenAI
    # from llama_index.embeddings import OpenAIEmbedding
except ImportError as e:
    raise ImportError(f"LlamaIndex dependencies missing: {e}")


class LlamaIndexRAGService(RAGService):
    def __init__(self, persistence_dir: str = "./data/rag_storage"):
        self.persistence_dir = persistence_dir
        self.index = None

        # Ensure persistence directory exists
        if not os.path.exists(self.persistence_dir):
            os.makedirs(self.persistence_dir)

        # Initialize ServiceContext (can be customized with specific LLM/Embed model)
        # For now, we rely on env vars (OPENAI_API_KEY) or defaults
        # Note: In 0.9.x ServiceContext.from_defaults() uses OpenAI by default if key is present
        self.service_context = ServiceContext.from_defaults()

        # Try to load existing index
        try:
            storage_context = StorageContext.from_defaults(persist_dir=self.persistence_dir)
            self.index = load_index_from_storage(storage_context, service_context=self.service_context)
        except Exception:
            # If load fails (e.g. empty dir), start with empty index
            self.index = None

    def _get_or_create_index(self, documents: List[Document] = []) -> Any:
        if self.index:
            return self.index

        self.index = VectorStoreIndex.from_documents(
            documents, service_context=self.service_context
        )
        self.index.storage_context.persist(persist_dir=self.persistence_dir)
        return self.index

    def upsert_documents(
        self,
        docs: Iterable[Dict[str, Any]],
        *,
        namespace: Optional[str] = None,
        allow_partial_failures: bool = True,
    ) -> Tuple[int, int]:
        """
        Upsert documents into the index.
        LlamaIndex 0.9.x simple index doesn't support granular updates easily without a vector store.
        For this implementation, we will convert dicts to Documents and insert them.
        """
        llama_docs = []
        for doc in docs:
            # Convert dict to LlamaIndex Document
            text = doc.get("text", "")
            metadata = doc.get("metadata", {})
            doc_id = doc.get("id")

            if not text:
                continue

            # Use doc_id parameter if present, ensuring it is a string
            if doc_id:
                l_doc = Document(text=text, doc_id=str(doc_id))
            else:
                l_doc = Document(text=text)

            l_doc.metadata = metadata
            llama_docs.append(l_doc)

        if not llama_docs:
            return (0, 0)

        if self.index is None:
            self._get_or_create_index(llama_docs)
        else:
            # For simple VectorStoreIndex, insert() adds to the index
            for l_doc in llama_docs:
                self.index.insert(l_doc)
            self.index.storage_context.persist(persist_dir=self.persistence_dir)

        return (len(llama_docs), 0)

    def delete_documents(
        self,
        ids: Iterable[str],
        *,
        namespace: Optional[str] = None,
    ) -> int:
        """
        Delete documents from the index.
        Note: Simple VectorStoreIndex delete might be limited depending on the store.
        """
        if not self.index:
            return 0

        count = 0
        for doc_id in ids:
            try:
                self.index.delete_ref_doc(doc_id, delete_from_docstore=True)
                count += 1
            except Exception:
                pass

        if count > 0:
            self.index.storage_context.persist(persist_dir=self.persistence_dir)

        return count

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        namespace: Optional[str] = None,
        hybrid: bool = True,
        permissions_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.index:
            return []

        filters = None
        if permissions_context and "user_id" in permissions_context:
            try:
                from llama_index.vector_stores.types import MetadataFilters, MetadataFilter
                filters = MetadataFilters(
                    filters=[
                        MetadataFilter(key="user_id", value=permissions_context["user_id"])
                    ]
                )
            except ImportError:
                # Fallback or log warning if types cannot be imported (unlikely given check)
                pass

        retriever = self.index.as_retriever(similarity_top_k=top_k, filters=filters)
        nodes = retriever.retrieve(query_text)

        results = []
        for node in nodes:
            results.append({
                "id": node.node.ref_doc_id or node.node.node_id,
                "text": node.node.get_content(),
                "metadata": node.node.metadata,
                "score": node.score
            })

        return results

    def embed(
        self,
        texts: Iterable[str],
    ) -> List[List[float]]:
        # Use the embedding model from service context
        embed_model = self.service_context.embed_model
        return [embed_model.get_text_embedding(t) for t in texts]
