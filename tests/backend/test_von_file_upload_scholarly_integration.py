from __future__ import annotations

import io
from typing import Any

import pytest
from flask import Flask

from src.backend.db import mongo_client as mongo_client_module
from src.backend.server.routes.von_routes import von_bp
from src.backend.services.kb_clone_benchmark_service import (
    ONTOLOGY_SLICE_COLLECTIONS,
    clone_database_ontology_slice,
)
from src.backend.workflows.durable.file_copy_upload_handler_workflow import (
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
)
from src.backend.workflows.durable.models import WorkflowInstanceStatus
from src.backend.workflows.durable.registry_factory import (
    build_durable_action_registry,
    build_workflow_registry_read_only,
)
from src.backend.workflows.durable.startup import (
    create_durable_executor,
    get_instance_manager,
)
from workflow_test_support import bootstrap_authoritative_file_copy_workflows

_SOURCE_DB_NAME = "test_1337_source_db"
_CLONE_DB_NAME = "test_1337_clone_db"
_TEST_WORKER_ID = "worker-1337"
_TEST_ARXIV_ID = "2502.14996"
_TEST_ARXIV_TITLE = "Self-Issues in Face Recognition: Why Existing Methods Ignore Them and How to Correct Them?"
_TEST_ARXIV_AUTHORS = ["Yihan Wang", "Yingjie Xia", "Haoran Wang"]
_TEST_ARXIV_CATEGORIES = ["cs.CV", "cs.AI"]
_TEST_ARXIV_SUMMARY = (
    "A deterministic metadata fixture used to validate upload-triggered "
    "scholarly representation postconditions for arXiv integration."
)


class _FakeStore:
    def __init__(self, blob_ref_cls):
        self._blob_ref_cls = blob_ref_cls
        self._bytes_by_key: dict[str, bytes] = {}

    def put_bytes(self, key, data, content_type=None, metadata=None):
        key_str = str(key)
        payload = bytes(data)
        self._bytes_by_key[key_str] = payload
        return self._blob_ref_cls(
            backend="local",
            key=key_str,
            uri=f"local://{key_str}",
            content_type=content_type,
            size_bytes=len(payload),
            metadata=dict(metadata or {}),
        )

    def get_bytes(self, key):
        key_str = str(key)
        if key_str not in self._bytes_by_key:
            raise FileNotFoundError(key_str)
        return self._bytes_by_key[key_str]

    def delete(self, key):
        self._bytes_by_key.pop(str(key), None)


@pytest.fixture(autouse=True)
def _integration_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "off")
    monkeypatch.setenv("VON_DB_NAME", _SOURCE_DB_NAME)

    from src.backend.services import workflow_event_integration_service
    from src.backend.workflows.durable import instance_manager, startup, registry_factory

    mongo_client_module.invalidate_connection()
    startup._instance_manager = None
    instance_manager._indexes_ensured = False
    workflow_event_integration_service._DEFAULT_BINDINGS_ENSURED = False
    registry_factory._durable_mcp_gateway = None
    yield
    mongo_client_module.invalidate_connection()


def _seed_source_database() -> Any:
    db = mongo_client_module.get_db()
    assert db is not None
    for collection_name in db.list_collection_names():
        db.drop_collection(collection_name)

    db["concepts"].insert_many(
        [
            {
                "concept_id": "#V#thing",
                "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
            },
            {
                "concept_id": "#V#predicate",
                "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
            },
            {
                "concept_id": "#V#store_of_information",
                "relationships": {
                    "is_a_type_of": ["#V#thing"],
                    "is_an_instance_of": [],
                },
            },
            {
                "concept_id": "#V#computer_file_copy",
                "relationships": {
                    "is_a_type_of": ["#V#store_of_information"],
                    "is_an_instance_of": [],
                },
            },
            {
                "concept_id": "#V#person",
                "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
            },
            {
                "concept_id": "#V#organisation",
                "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
            },
            {
                "concept_id": "#V#user_test",
                "relationships": {"is_an_instance_of": ["#V#person"]},
            },
            {
                "concept_id": "#V#org_test",
                "relationships": {"is_an_instance_of": ["#V#organisation"]},
            },
        ]
    )
    db["text_values"].insert_one({"text": "seed", "lang": "en-NZ"})
    db["text_relations"].insert_one(
        {
            "subject_concept_id": "#V#thing",
            "predicate": "hasDescription",
            "text": "seed description",
            "lang": "en-NZ",
        }
    )

    db["interaction_sessions"].insert_one(
        {"session_id": "noise-session", "history": [{"role": "user", "content": "noise"}]}
    )
    db["chat_history"].insert_one(
        {"session_id": "noise-chat", "messages": [{"role": "assistant", "content": "noise"}]}
    )
    db["workflow_use_episodes"].insert_one({"stable_key": "noise-episode"})

    return db.client


def _build_upload_app() -> Flask:
    app = Flask(__name__)
    app.testing = True
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")
    return app


def _mock_paper_metadata(**kwargs):
    arxiv_id = str(kwargs.get("arxiv_id") or "").strip().lower()
    if arxiv_id != _TEST_ARXIV_ID:
        return {"success": False, "error": "unexpected_arxiv_id", "arxiv_id": arxiv_id}
    return {
        "success": True,
        "paper": {
            "id": _TEST_ARXIV_ID,
            "title": _TEST_ARXIV_TITLE,
            "authors": list(_TEST_ARXIV_AUTHORS),
            "summary": _TEST_ARXIV_SUMMARY,
            "categories": list(_TEST_ARXIV_CATEGORIES),
        },
    }


def _run_pending_file_copy_instance(instance_id: str):
    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    assert instance is not None
    workflow_id = str(instance.workflow_id or "").strip()
    assert workflow_id

    claimed = manager.find_and_claim_instance(
        _TEST_WORKER_ID,
        workflow_ids=[workflow_id],
    )
    assert claimed is not None
    assert claimed.instance_id == instance_id
    assert claimed.status == WorkflowInstanceStatus.RUNNING

    registry = build_workflow_registry_read_only()
    registration = registry.get_registration(workflow_id)
    assert registration is not None
    assert str(registration.source or "").strip().lower() == "vontology"
    definition = registration.definition

    executor = create_durable_executor(
        build_durable_action_registry(),
        instance_manager=manager,
        max_transitions=40,
    )
    result = executor.run_durable(
        instance_id,
        definition,
        worker_id=_TEST_WORKER_ID,
        resume_from_checkpoint=True,
    )
    if result.completed:
        manager.mark_completed(
            instance_id,
            outputs=result.data,
            final_state=result.final_state,
        )
    elif result.error != "cancelled":
        manager.mark_failed(
            instance_id,
            error=result.error or "unknown_error",
            error_step=result.final_state,
        )
    manager.release_lock(instance_id, _TEST_WORKER_ID)
    final_instance = manager.get_instance(instance_id)
    assert final_instance is not None
    return result, final_instance


def _get_paper_doc_for_file_copy(file_copy_concept_id: str) -> dict[str, Any]:
    db = mongo_client_module.get_db()
    assert db is not None
    paper_doc = db["concepts"].find_one(
        {
            "relationships.#V#propositional_information_thing_has_computer_file": {
                "$in": [file_copy_concept_id]
            }
        }
    )
    assert isinstance(paper_doc, dict)
    return paper_doc


def _collect_text_values(concept_id: str, predicate: str) -> list[str]:
    from src.backend.services.text_value_service import get_texts_for_concept

    rows = get_texts_for_concept(concept_id, predicate=predicate, limit=200)
    texts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def _is_complete_terminal_state(state_id: str) -> bool:
    state = str(state_id or "").strip()
    return state == "complete" or state.endswith("_complete")


def test_upload_event_workflow_materialises_scholarly_representation_in_ontology_clone(
    monkeypatch: pytest.MonkeyPatch,
):
    mongo_client = _seed_source_database()
    source_db = mongo_client[_SOURCE_DB_NAME]
    source_noise_counts_before = {
        "interaction_sessions": source_db["interaction_sessions"].count_documents({}),
        "chat_history": source_db["chat_history"].count_documents({}),
        "workflow_use_episodes": source_db["workflow_use_episodes"].count_documents({}),
    }

    clone_counts = clone_database_ontology_slice(
        mongo_client,
        source_db_name=_SOURCE_DB_NAME,
        clone_db_name=_CLONE_DB_NAME,
    )
    assert set(clone_counts.keys()) <= set(ONTOLOGY_SLICE_COLLECTIONS)
    assert "concepts" in clone_counts
    assert "text_relations" in clone_counts
    assert "text_values" in clone_counts

    clone_db = mongo_client[_CLONE_DB_NAME]
    clone_collections = set(clone_db.list_collection_names())
    assert "interaction_sessions" not in clone_collections
    assert "chat_history" not in clone_collections
    assert "workflow_use_episodes" not in clone_collections

    monkeypatch.setenv("VON_DB_NAME", _CLONE_DB_NAME)
    mongo_client_module.invalidate_connection()
    from src.backend.services.paper_representation_workflow_vontology_service import (
        bootstrap_canonical_paper_representation_workflows,
    )

    paper_bootstrap_report = bootstrap_canonical_paper_representation_workflows()
    assert paper_bootstrap_report.get("success") is True
    bootstrap_report = bootstrap_authoritative_file_copy_workflows()
    graph_errors = (bootstrap_report.get("graph_publication") or {}).get(
        "errors_by_workflow_id"
    ) or {}
    assert graph_errors == {}

    from src.backend.services import workflow_event_integration_service

    workflow_event_integration_service._DEFAULT_BINDINGS_ENSURED = False

    from src.backend.services.blob_store import BlobRef

    fake_store = _FakeStore(BlobRef)
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: fake_store,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._record_file_upload_in_chat_history",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._get_paper_metadata",
        _mock_paper_metadata,
    )

    app = _build_upload_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_test"
        sess["session_id"] = "integration-session-1337"

    upload_payload = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    first_upload = client.post(
        "/von/api/files/upload",
        data={"file": (io.BytesIO(upload_payload), "2502.14996.pdf")},
        content_type="multipart/form-data",
    )
    assert first_upload.status_code == 200
    first_body = first_upload.get_json()
    assert isinstance(first_body, dict)
    assert first_body.get("success") is True
    launch_payload = first_body.get("workflow_event_launch") or {}
    assert launch_payload.get("success") is True
    assert launch_payload.get("triggered") is True
    assert launch_payload.get("workflow_id") == FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID

    first_instance_id = str(launch_payload.get("instance_id") or "").strip()
    assert first_instance_id
    first_result, first_instance = _run_pending_file_copy_instance(first_instance_id)
    assert first_result.completed is True
    assert _is_complete_terminal_state(first_result.final_state)
    assert first_instance.status == WorkflowInstanceStatus.COMPLETED

    first_file_copy_id = str((first_body.get("uploaded") or {}).get("concept_id") or "")
    assert first_file_copy_id
    paper_doc = _get_paper_doc_for_file_copy(first_file_copy_id)
    paper_id = str(paper_doc.get("concept_id") or "").strip()
    assert paper_id

    relationships = paper_doc.get("relationships") or {}
    assert "#V#scholarly_article" in (relationships.get("is_an_instance_of") or [])
    assert first_file_copy_id in (
        relationships.get("#V#propositional_information_thing_has_computer_file") or []
    )
    assert len(relationships.get("#V#authored_by") or []) >= len(_TEST_ARXIV_AUTHORS)
    assert len(relationships.get("#V#about") or []) >= 1

    has_name_values = _collect_text_values(paper_id, "hasName")
    has_description_values = _collect_text_values(paper_id, "hasDescription")
    topic_label_values = _collect_text_values(paper_id, "#V#has_topic_labels")

    assert _TEST_ARXIV_ID in has_name_values
    assert _TEST_ARXIV_TITLE in has_name_values
    assert _TEST_ARXIV_SUMMARY in has_description_values
    assert topic_label_values
    assert _TEST_ARXIV_CATEGORIES[0] in topic_label_values[0]

    second_upload = client.post(
        "/von/api/files/upload",
        data={"file": (io.BytesIO(upload_payload), "2502.14996.pdf")},
        content_type="multipart/form-data",
    )
    assert second_upload.status_code == 200
    second_body = second_upload.get_json()
    assert isinstance(second_body, dict)
    assert second_body.get("success") is True
    second_launch = second_body.get("workflow_event_launch") or {}
    second_instance_id = str(second_launch.get("instance_id") or "").strip()
    assert second_instance_id

    second_result, second_instance = _run_pending_file_copy_instance(second_instance_id)
    assert second_result.completed is True
    assert second_instance.status == WorkflowInstanceStatus.COMPLETED

    second_file_copy_id = str((second_body.get("uploaded") or {}).get("concept_id") or "")
    assert second_file_copy_id
    paper_after_repeat = _get_paper_doc_for_file_copy(second_file_copy_id)
    assert str(paper_after_repeat.get("concept_id")) == paper_id
    repeat_relationships = paper_after_repeat.get("relationships") or {}
    linked_files = repeat_relationships.get(
        "#V#propositional_information_thing_has_computer_file"
    ) or []
    assert first_file_copy_id in linked_files
    assert second_file_copy_id in linked_files

    source_noise_counts_after = {
        "interaction_sessions": source_db["interaction_sessions"].count_documents({}),
        "chat_history": source_db["chat_history"].count_documents({}),
        "workflow_use_episodes": source_db["workflow_use_episodes"].count_documents({}),
    }
    assert source_noise_counts_after == source_noise_counts_before
