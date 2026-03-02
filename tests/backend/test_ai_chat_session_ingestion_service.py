from __future__ import annotations

from pathlib import Path


def _record(*, tmp_path: Path, environment: str = "codex"):
    from src.backend.services.ai_chat_session_ingestion_service import SessionRecord

    local_file = tmp_path / "session.jsonl"
    local_file.write_text('{"id":"abc"}\n', encoding="utf-8")
    resolved = str(local_file.resolve())
    return SessionRecord(
        environment=environment,
        source_session_id="session-1",
        canonical_source_path=resolved,
        source_uri=local_file.resolve().as_uri(),
        source_modified_at_utc="2026-03-02T00:00:00+00:00",
        source_size_bytes=local_file.stat().st_size,
        content_sha256="hash-1",
        local_path=resolved,
        title="Session 1",
        metadata={},
    )


def test_stable_document_concept_id_is_deterministic():
    from src.backend.services.ai_chat_session_ingestion_service import (
        stable_document_concept_id,
    )

    concept_id_1 = stable_document_concept_id(
        environment="codex",
        source_session_id="abc",
        canonical_source_path="C:\\example\\session.jsonl",
    )
    concept_id_2 = stable_document_concept_id(
        environment="codex",
        source_session_id="abc",
        canonical_source_path="C:\\example\\session.jsonl",
    )
    concept_id_3 = stable_document_concept_id(
        environment="codex",
        source_session_id="different",
        canonical_source_path="C:\\example\\session.jsonl",
    )

    assert concept_id_1 == concept_id_2
    assert concept_id_1 != concept_id_3
    assert concept_id_1.startswith("#V#ai_programming_chat_session_codex_")


def test_copilot_adapter_warns_when_required_root_missing(tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import CopilotSessionAdapter

    missing_root = tmp_path / "missing"
    adapter = CopilotSessionAdapter(roots=[missing_root])
    records, warnings = adapter.discover()

    assert records == []
    assert any("required root missing" in warning for warning in warnings)


def test_run_new_record_counts_created_mutation(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
        SessionDecision,
    )

    record = _record(tmp_path=tmp_path)

    class _Adapter:
        environment = "codex"

        def discover(self):
            return [record], []

    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[_Adapter()],
    )

    monkeypatch.setattr(service, "ensure_ontology_types", lambda: [])
    monkeypatch.setattr(
        service,
        "_classify_record",
        lambda _rec: SessionDecision(
            action="new",
            reason="document_not_found",
            record=_rec,
            document_concept_id=_rec.document_concept_id,
        ),
    )
    monkeypatch.setattr(
        service,
        "_apply_new",
        lambda _rec, _doc_id: {"success": True, "file_copy_concept_id": "#V#file_1"},
    )

    result = service.run(dry_run=False)

    assert result.success is True
    assert result.status == "ok"
    assert result.counters.classified_new == 1
    assert result.counters.created == 1
    assert result.counters.intended_mutations == 1
    assert result.counters.executed_mutations == 1


def test_run_unchanged_record_skips_mutation(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
        SessionDecision,
    )

    record = _record(tmp_path=tmp_path)

    class _Adapter:
        environment = "codex"

        def discover(self):
            return [record], []

    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[_Adapter()],
    )

    monkeypatch.setattr(service, "ensure_ontology_types", lambda: [])
    monkeypatch.setattr(
        service,
        "_classify_record",
        lambda _rec: SessionDecision(
            action="unchanged",
            reason="content_hash_match",
            record=_rec,
            document_concept_id=_rec.document_concept_id,
        ),
    )

    result = service.run(dry_run=False)

    assert result.success is True
    assert result.status == "ok"
    assert result.counters.classified_unchanged == 1
    assert result.counters.skipped == 1
    assert result.counters.intended_mutations == 0
    assert result.counters.executed_mutations == 0


def test_dry_run_uses_ontology_validation_not_bootstrap(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
    )

    record = _record(tmp_path=tmp_path)

    class _Adapter:
        environment = "codex"

        def discover(self):
            return [record], []

    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[_Adapter()],
    )

    bootstrap_called = {"value": False}
    validate_called = {"value": False}

    def _bootstrap():
        bootstrap_called["value"] = True
        return []

    def _validate():
        validate_called["value"] = True
        return []

    monkeypatch.setattr(service, "ensure_ontology_types", _bootstrap)
    monkeypatch.setattr(service, "validate_ontology_types", _validate)

    result = service.run(dry_run=True)

    assert result.status == "dry_run"
    assert bootstrap_called["value"] is False
    assert validate_called["value"] is True


def test_run_flags_mutation_not_executed(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
        SessionDecision,
    )

    record = _record(tmp_path=tmp_path)

    class _Adapter:
        environment = "codex"

        def discover(self):
            return [record], []

    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[_Adapter()],
    )

    monkeypatch.setattr(service, "ensure_ontology_types", lambda: [])
    monkeypatch.setattr(
        service,
        "_classify_record",
        lambda _rec: SessionDecision(
            action="new",
            reason="document_not_found",
            record=_rec,
            document_concept_id=_rec.document_concept_id,
        ),
    )
    monkeypatch.setattr(
        service,
        "_apply_new",
        lambda _rec, _doc_id: {"success": False, "error": "simulated_failure"},
    )

    result = service.run(dry_run=False)

    assert result.success is False
    assert result.status == "escalation_required"
    assert result.error_code == "mutation_not_executed"
    assert result.requires_follow_up is True
    assert result.counters.intended_mutations == 1
    assert result.counters.executed_mutations == 0


def test_apply_update_relinks_document_to_new_file_copy(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
    )

    record = _record(tmp_path=tmp_path)
    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[],
    )

    existing_doc = {
        "concept_id": record.document_concept_id,
        "relationships": {
            "#V#propositional_information_thing_has_computer_file_copy": [
                "#V#file_old"
            ]
        },
        "attributes": {"source_content_sha256": "old-hash"},
    }
    monkeypatch.setattr(service, "_get_concept", lambda _concept_id: existing_doc)
    monkeypatch.setattr(
        service,
        "_import_record_file",
        lambda _record, _type_id, _source: {
            "success": True,
            "concept_id": "#V#file_new",
        },
    )
    monkeypatch.setattr(service, "_ensure_document_type", lambda *_args, **_kwargs: None)

    removed_pairs: list[tuple[str, str]] = []
    added_pairs: list[tuple[str, str]] = []
    metadata_updates: list[dict[str, object]] = []

    monkeypatch.setattr(
        service,
        "_remove_link_pair",
        lambda document_id, file_id: removed_pairs.append((document_id, file_id)),
    )
    monkeypatch.setattr(
        service,
        "_ensure_link_pair",
        lambda document_id, file_id: added_pairs.append((document_id, file_id)),
    )

    def _capture_metadata(**kwargs):
        metadata_updates.append(kwargs)

    monkeypatch.setattr(service, "_update_document_metadata", _capture_metadata)

    result = service._apply_update(record, record.document_concept_id)

    assert result["success"] is True
    assert result["file_copy_concept_id"] == "#V#file_new"
    assert removed_pairs == [(record.document_concept_id, "#V#file_old")]
    assert added_pairs == [(record.document_concept_id, "#V#file_new")]
    assert metadata_updates
    assert metadata_updates[0]["current_file_copy_concept_id"] == "#V#file_new"
    assert metadata_updates[0]["previous_file_copy_concept_ids"] == ["#V#file_old"]


def test_classify_record_requires_repair_for_legacy_links(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
    )

    record = _record(tmp_path=tmp_path)
    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[],
    )

    existing_doc = {
        "concept_id": record.document_concept_id,
        "relationships": {
            "#V#propositional_information_thing_has_computer_file_copy": [
                "#V#file_old"
            ]
        },
        "attributes": {
            "source_content_sha256": record.content_sha256,
            "current_file_copy_concept_id": "#V#file_old",
        },
    }
    monkeypatch.setattr(service, "_get_concept", lambda _concept_id: existing_doc)

    decision = service._classify_record(record)

    assert decision.action == "repair"
    assert decision.reason == "canonical_link_missing"
    assert decision.repair_file_copy_concept_id == "#V#file_old"


def test_apply_repair_updates_links_without_reimport(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
    )

    record = _record(tmp_path=tmp_path)
    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[],
    )

    existing_doc = {
        "concept_id": record.document_concept_id,
        "relationships": {
            "#V#propositional_information_thing_has_computer_file_copy": [
                "#V#file_old"
            ]
        },
        "attributes": {
            "source_content_sha256": record.content_sha256,
            "current_file_copy_concept_id": "#V#file_old",
        },
    }
    monkeypatch.setattr(service, "_get_concept", lambda _concept_id: existing_doc)
    monkeypatch.setattr(service, "_ensure_document_type", lambda *_args, **_kwargs: None)

    added_pairs: list[tuple[str, str]] = []
    metadata_updates: list[dict[str, object]] = []

    monkeypatch.setattr(
        service,
        "_ensure_link_pair",
        lambda document_id, file_id: added_pairs.append((document_id, file_id)),
    )

    def _capture_metadata(**kwargs):
        metadata_updates.append(kwargs)

    monkeypatch.setattr(service, "_update_document_metadata", _capture_metadata)

    result = service._apply_repair(
        record,
        record.document_concept_id,
        preferred_file_copy_concept_id="#V#file_old",
    )

    assert result["success"] is True
    assert result["file_copy_concept_id"] == "#V#file_old"
    assert added_pairs == [(record.document_concept_id, "#V#file_old")]
    assert metadata_updates
    assert metadata_updates[0]["current_file_copy_concept_id"] == "#V#file_old"


def test_run_repair_action_counts_as_updated(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
        SessionDecision,
    )

    record = _record(tmp_path=tmp_path)

    class _Adapter:
        environment = "codex"

        def discover(self):
            return [record], []

    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[_Adapter()],
    )

    monkeypatch.setattr(service, "ensure_ontology_types", lambda: [])
    monkeypatch.setattr(
        service,
        "_classify_record",
        lambda _rec: SessionDecision(
            action="repair",
            reason="canonical_link_missing",
            record=_rec,
            document_concept_id=_rec.document_concept_id,
            repair_file_copy_concept_id="#V#file_legacy",
        ),
    )
    monkeypatch.setattr(
        service,
        "_apply_repair",
        lambda _rec, _doc_id, _file_id: {
            "success": True,
            "file_copy_concept_id": "#V#file_legacy",
        },
    )

    result = service.run(dry_run=False)

    assert result.success is True
    assert result.status == "ok"
    assert result.counters.classified_updated == 1
    assert result.counters.updated == 1
    assert result.counters.intended_mutations == 1
    assert result.counters.executed_mutations == 1


def test_run_emits_incremental_progress_events(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
        SessionDecision,
    )

    record = _record(tmp_path=tmp_path)

    class _Adapter:
        environment = "codex"

        def discover(self):
            return [record], []

    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[_Adapter()],
    )

    monkeypatch.setattr(service, "ensure_ontology_types", lambda: [])
    monkeypatch.setattr(
        service,
        "_classify_record",
        lambda _rec: SessionDecision(
            action="new",
            reason="document_not_found",
            record=_rec,
            document_concept_id=_rec.document_concept_id,
        ),
    )
    monkeypatch.setattr(
        service,
        "_apply_new",
        lambda _rec, _doc_id: {
            "success": True,
            "file_copy_concept_id": "#V#file_new",
            "storage_object_written": True,
            "storage_backend": "swift",
            "storage_key": "imports/key",
            "storage_uri": "swift://bucket/imports/key",
            "ontology_links_aligned": True,
            "ontology_type_aligned": True,
        },
    )

    events: list[dict[str, object]] = []
    result = service.run(dry_run=False, progress_callback=events.append)

    assert result.success is True
    assert result.status == "ok"
    event_names = [str(evt.get("event")) for evt in events]
    assert event_names == [
        "discovery_complete",
        "record_classified",
        "record_processed",
        "sync_completed",
    ]
    processed = events[2]
    assert processed["storage_object_written"] is True
    assert processed["storage_backend"] == "swift"
    assert processed["ontology_links_aligned"] is True
    assert processed["ontology_type_aligned"] is True


def test_apply_new_returns_storage_and_alignment_telemetry(monkeypatch, tmp_path):
    from src.backend.services.ai_chat_session_ingestion_service import (
        AIChatSessionIngestionService,
    )

    record = _record(tmp_path=tmp_path)
    service = AIChatSessionIngestionService(
        user_concept_id="#V#user_test",
        adapters=[],
    )

    monkeypatch.setattr(service, "_get_concept", lambda _concept_id: None)
    monkeypatch.setattr(
        service,
        "_import_record_file",
        lambda _record, _type_id, _source: {
            "success": True,
            "concept_id": "#V#file_new",
            "storage": {
                "backend": "swift",
                "key": "imports/key",
                "uri": "swift://bucket/imports/key",
            },
        },
    )
    monkeypatch.setattr(service, "_create_document_concept", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_ensure_document_type", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_ensure_link_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_update_document_metadata", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_is_document_file_link_aligned", lambda *_args: True)
    monkeypatch.setattr(service, "_is_document_type_aligned", lambda *_args: True)

    result = service._apply_new(record, record.document_concept_id)

    assert result["success"] is True
    assert result["storage_object_written"] is True
    assert result["storage_backend"] == "swift"
    assert result["storage_key"] == "imports/key"
    assert result["ontology_links_aligned"] is True
    assert result["ontology_type_aligned"] is True
