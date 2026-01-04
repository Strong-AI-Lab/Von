from datetime import datetime, timezone

from src.backend.services.rag_service import get_rag_service


def test_rag():
    print("Getting RAG service...")
    try:
        rag = get_rag_service()
        print(f"Got RAG service: {rag}")
    except Exception as e:
        print(f"Failed to get RAG service: {e}")
        return

    doc_id = "test_doc_1"
    text = "Spirals are interesting geometric shapes found in nature."
    metadata = {"type": "test", "timestamp": datetime.now(timezone.utc).isoformat()}

    print(f"Indexing document: {text}")
    try:
        success, failed = rag.upsert_documents(
            [{"id": doc_id, "text": text, "metadata": metadata}],
            namespace="test_namespace",
        )
        print(f"Upsert result: success={success}, failed={failed}")
    except Exception as e:
        print(f"Upsert failed: {e}")
        return

    print("Querying for 'spiral'...")
    try:
        results = rag.query("spiral", top_k=5, namespace="chat_history")
        print(f"Query results: {results}")
    except Exception as e:
        print(f"Query failed: {e}")


if __name__ == "__main__":
    test_rag()
