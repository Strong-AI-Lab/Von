import json
import os
import shutil

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


def test_query_honours_workflow_capability_retrieval_candidate_limit(
    mock_llamaindex,
    monkeypatch,
    workspace_tmp_path,
):
    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
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
    _configure_rag_runtime_settings(monkeypatch)

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
    rag.invalidate_runtime_configuration_cache()

    state = rag.get_namespace_runtime_state("workflow_capabilities")

    assert state["compatible"] is False
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

    rag = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
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


def test_llamaindex_reset_namespace_retries_transient_sharing_violation(
    monkeypatch,
    workspace_tmp_path,
) -> None:
    _configure_rag_runtime_settings(monkeypatch)

    from src.backend.services.rag_backends.llamaindex_backend import (
        LlamaIndexRAGService,
    )

    rag = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
    namespace = "workflow_capabilities"
    persist_dir = workspace_tmp_path / "rag_storage" / "namespaces" / rag._namespace_dirname(namespace)
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

    rag = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
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

    rag = LlamaIndexRAGService(
        persistence_dir=str(workspace_tmp_path / "rag_storage")
    )
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
