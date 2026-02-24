"""Gateway-level coverage for file-copy import/index MCP tools."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_file_copy_ingestion_tools_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.import_local_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#imported_file_gateway",
            "type_concept_id": "#V#computer_file_copy",
            "artifact_record": {"artifact_id": "#V#imported_file_gateway"},
            "storage": {"backend": "local", "key": "imports/user/hash/file.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "gateway indexed text",
            "content_type": "text/plain",
            "original_filename": "gateway.txt",
            "size_bytes": 19,
            "blob": {"backend": "local", "key": "imports/user/hash/file.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#imported_file_gateway"},
    )

    class _StubRAG:
        def upsert_documents(
            self, docs, *, namespace=None, allow_partial_failures=True
        ):
            return 1, 0

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _StubRAG(),
    )

    imported = gateway.invoke(
        "import_local_file_copy",
        {"local_path": "README.md", "namespace": "#V#user@org"},
    ).payload
    assert imported.get("success") is True
    assert imported.get("concept_id") == "#V#imported_file_gateway"

    indexed = gateway.invoke(
        "index_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload
    assert indexed.get("success") is True
    assert indexed.get("indexed_count") == 1
