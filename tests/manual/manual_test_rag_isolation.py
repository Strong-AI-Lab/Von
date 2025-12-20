import os
import shutil
from src.backend.services.rag_backends.llamaindex_backend import LlamaIndexRAGService


def test_rag_isolation():
    # Setup clean storage
    storage_path = "./data/test_rag_isolation"
    if os.path.exists(storage_path):
        shutil.rmtree(storage_path)

    rag = LlamaIndexRAGService(persistence_dir=storage_path)

    # Upsert documents for different users
    docs = [
        {
            "id": "doc_a",
            "text": "This is a secret for User A.",
            "metadata": {"user_id": "user_A", "type": "secret"},
        },
        {
            "id": "doc_b",
            "text": "This is a secret for User B.",
            "metadata": {"user_id": "user_B", "type": "secret"},
        },
    ]
    rag.upsert_documents(docs)
    print("Documents upserted.")

    # Query as User A
    print("\nQuerying as User A...")
    results_a = rag.query("secret", permissions_context={"user_id": "user_A"})
    print(f"Results for User A: {[r['text'] for r in results_a]}")

    assert len(results_a) == 1
    assert "User A" in results_a[0]["text"]
    assert "User B" not in results_a[0]["text"]

    # Query as User B
    print("\nQuerying as User B...")
    results_b = rag.query("secret", permissions_context={"user_id": "user_B"})
    print(f"Results for User B: {[r['text'] for r in results_b]}")

    assert len(results_b) == 1
    assert "User B" in results_b[0]["text"]
    assert "User A" not in results_b[0]["text"]

    # Query without context (should return both or depend on policy - currently returns all if no filter)
    # Note: In a real secure system, we might want default deny, but for now we test the filter works when present.
    print("\nQuerying without context...")
    results_all = rag.query("secret")
    print(f"Results without context: {[r['text'] for r in results_all]}")
    assert len(results_all) >= 2

    print("\nSUCCESS: User isolation verified.")

    # Cleanup
    if os.path.exists(storage_path):
        shutil.rmtree(storage_path)


if __name__ == "__main__":
    test_rag_isolation()
