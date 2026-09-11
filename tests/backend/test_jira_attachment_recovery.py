import base64
import copy
from types import SimpleNamespace

import pytest

from src.backend.services import jira_task_import_service as importer
from src.backend.services import task_management_service as tasks


def test_checkpointed_metadata_receives_bytes_once_and_paginates(monkeypatch):
    native = {"source": {"source_system": "jira", "external_id": "123"}}
    uploads, added, offsets = [], [], []

    def page(task, *, limit, offset):
        offsets.append(offset)
        return {"attachments": [{}] if offset == 0 else [native], "total": 2}

    def upload(**kwargs):
        uploads.append(kwargs["data"])
        return {"success": True, "concept_id": "#V#retained_file"}

    def attach(task, **kwargs):
        added.append(kwargs)
        native["file_copy_concept_id"] = kwargs["file_copy_concept_id"]
        return native

    monkeypatch.setattr(importer, "list_task_attachments", page)
    monkeypatch.setattr(importer, "add_task_attachment", attach)
    monkeypatch.setattr(importer, "_ensure_jira_participant_concept", lambda **kw: {})
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.import_bytes_file_copy", upload
    )
    checkpoint = {
        "comment_ids": set(),
        "attachment_ids": {"123"},
        "worklog_ids": set(),
        "status_history_signatures": set(),
    }
    arguments = {
        "task_concept_id": "#V#task",
        "issue_key": "PROJ-1",
        "raw_issue": {
            "fields": {
                "attachment": [
                    {
                        "id": "123",
                        "filename": "original.bin",
                        "content_base64": base64.b64encode(b"original").decode(),
                    }
                ]
            }
        },
        "actor_concept_id": "#V#actor",
        "organisation_concept_id": None,
        "namespace": None,
        "participant_map": {},
        "participant_account_cache": {},
        "participant_email_cache": {},
        "auto_resolve_participants": False,
        "create_missing_participant_concepts": False,
        "dry_run": False,
        "existing_activity_checkpoint": checkpoint,
    }
    first, mapped, dropped = importer._materialise_jira_issue_activity(**arguments)
    second, _, _ = importer._materialise_jira_issue_activity(**arguments)
    assert first["attachment_ids"] == second["attachment_ids"] == ["123"]
    assert mapped == ["attachments"] and not dropped
    assert uploads == [b"original"] and len(added) == 1
    assert offsets == [0, 1, 0, 1]


@pytest.mark.parametrize("mode", ["missing", "existing", "concurrent"])
def test_native_attachment_enrichment_preserves_identity_and_existing_file(
    monkeypatch, mode
):
    old = {
        "attachment_id": "stable_attachment",
        "filename": "Original filename",
        "uri": "jira://attachment/123",
        "note": "Keep my annotation",
        "created_at": "2020-01-01T00:00:00Z",
        "source": {"source_system": "jira", "external_id": "123"},
    }
    if mode == "existing":
        old["file_copy_concept_id"] = "#V#existing_file"
    doc = {"metadata": {"attachments": [{"attachment_id": "unrelated"}, old]}}
    writes = []

    def update(query, change):
        if "$push" in change:
            return SimpleNamespace(matched_count=0)
        writes.append((query, change))
        prefix = "metadata.attachments.1"
        assert query[f"{prefix}.attachment_id"] == "stable_attachment"
        assert query[f"{prefix}.file_copy_concept_id"] == {"$in": [None, ""]}
        assert set(change["$set"]) == {
            f"{prefix}.file_copy_concept_id", f"{prefix}.uri", "updated_at"
        }
        old["file_copy_concept_id"] = (
            "#V#concurrent_file" if mode == "concurrent" else "#V#retained_file"
        )
        old["uri"] = "blob://retained"
        return SimpleNamespace(matched_count=0 if mode == "concurrent" else 1)

    monkeypatch.setattr(tasks.ConceptsRepository, "update_one", update)
    monkeypatch.setattr(tasks, "_get_task_doc", lambda task: (task, copy.deepcopy(doc)))
    result = tasks.add_task_attachment(
        "#V#task",
        filename="New source filename",
        uri="blob://retained",
        source={"source_system": "jira", "external_id": "123"},
        file_copy_concept_id="#V#retained_file",
    )
    assert len(doc["metadata"]["attachments"]) == 2
    assert result["attachment_id"] == "stable_attachment"
    assert result["filename"] == "Original filename"
    assert result["note"] == "Keep my annotation"
    assert result["created_at"] == "2020-01-01T00:00:00Z"
    expected = {
        "missing": "#V#retained_file",
        "existing": "#V#existing_file",
        "concurrent": "#V#concurrent_file",
    }
    assert result["file_copy_concept_id"] == expected[mode]
    assert len(writes) == (0 if mode == "existing" else 1)
