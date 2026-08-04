from __future__ import annotations

import json

from src.backend.workflows.durable.models import WorkflowSchedule
from src.backend.workflows.workflow_launch_input_contracts import (
    resolve_workflow_launch_inputs,
)


class _FakeManager:
    def __init__(self, schedules: list[WorkflowSchedule] | None = None) -> None:
        self.schedules = schedules or []
        self.created: list[WorkflowSchedule] = []
        self.enabled_updates: list[tuple[str, bool]] = []

    def list_schedules(self, limit: int = 50):
        return list(self.schedules)[:limit]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        self.enabled_updates.append((schedule_id, enabled))
        for schedule in self.schedules:
            if schedule.schedule_id == schedule_id:
                schedule.enabled = enabled
                return True
        return False

    def create_schedule(self, schedule: WorkflowSchedule) -> str:
        self.created.append(schedule)
        self.schedules.append(schedule)
        return schedule.schedule_id


def _reset_schedule_bootstrap_state(mod) -> None:
    mod._bootstrap_completed = False


def test_email_source_workflow_bootstrap_materialises_bundle_and_hint(
    monkeypatch,
) -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    captured_hint: dict[str, object] = {}
    captured_bundle: dict[str, object] = {}

    def fake_upsert(**kwargs):
        captured_hint.update(kwargs)
        return {"updated": True}

    def fake_bootstrap(**kwargs):
        captured_bundle.update(kwargs)
        return {
            "publication": {"counts": {"errors": 0}},
            "workflow_ids": [mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID],
        }

    monkeypatch.setattr(mod, "upsert_singleton_text_relation", fake_upsert)
    monkeypatch.setattr(mod, "bootstrap_repo_seed_workflow_bundle", fake_bootstrap)

    report = mod.bootstrap_canonical_email_source_representation_convergence_workflows()

    assert report["success"] is True
    assert captured_bundle["asset_path"] == mod._REPO_SEED_ASSET_PATH
    assert captured_hint["subject_concept_id"] == mod.GMAIL_GET_MESSAGE_TOOL_ID
    assert captured_hint["predicate"] == mod.GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE
    payload = json.loads(str(captured_hint["text"]))
    entry = payload["entries"][0]
    assert entry["action_kind"] == "terminal_completion"
    assert entry["action"]["tool_name"] == "record_source_processing_marker"
    assert entry["tool_arguments"]["source_system"] == "gmail"
    assert entry["tool_arguments"]["source_item_id_context_key"] == "message_id"
    assert entry["upstream_filter"]["query_fragment"] == ""
    assert entry["represented_effects"][0]["effect_kind"] == (
        "record_source_processing_marker"
    )


def test_email_source_seed_keeps_claim_enrichment_out_of_source_completion() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflows = {
        item["workflow_id"]: item
        for item in payload.get("workflows") or []
        if isinstance(item, dict)
    }
    arxiv_resource = workflows[
        mod.ARXIV_RESOURCE_INGESTION_FROM_EMAIL_REFERENCE_WORKFLOW_ID
    ]
    steps = arxiv_resource["publication_spec"]["steps"]
    state_ids = {step["state_id"] for step in steps}

    assert state_ids == {
        "normalise_reference",
        "represent_arxiv_paper",
        "done",
        "failed",
    }
    assert all("claim" not in json.dumps(step).lower() for step in steps)


def test_email_source_seed_excludes_mail_profile_status_questions() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload.get("workflows") or []
        if item.get("workflow_id") == mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID
    )
    discovery_relation = next(
        item
        for item in workflow.get("text_relations") or []
        if item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
    )
    discovery = json.loads(discovery_relation["text"])

    assert "which gmail profile" in discovery["excluded_query_cues"]
    assert any(
        "profile or account" in note
        for note in discovery["routing_notes"]
    )


def test_email_source_seed_keeps_batch_effects_out_of_singular_routing() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload.get("workflows") or []
        if item.get("workflow_id") == mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID
    )
    relations = {
        item["predicate"]: json.loads(item["text"])
        for item in workflow.get("text_relations") or []
        if item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
    }
    routing_notes = relations["#V#hasWorkflowDiscoveryExemplarsJson"][
        "routing_notes"
    ]
    assert "batch workflow" in workflow["description"]
    assert any("every Gmail message" in note for note in routing_notes)
    assert any("every supported arXiv reference" in note for note in routing_notes)
    assert any("Do not route a singular" in note for note in routing_notes)


def test_email_source_seed_uses_represented_markers_instead_of_gmail_writes() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    seed_text = json.dumps(payload)
    assert "gmail_modify_labels" not in seed_text
    assert "vontology/ingested" not in seed_text

    workflows = {
        item["workflow_id"]: item
        for item in payload.get("workflows") or []
        if isinstance(item, dict)
    }
    child = workflows[mod.EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID]
    steps = {
        step["state_id"]: step
        for step in child["publication_spec"]["steps"]
        if isinstance(step, dict)
    }

    assert steps["check_processed_marker"]["static_input_bindings"][1] == [
        "tool_name",
        "get_source_processing_marker",
    ]
    mark_done = steps["mark_done"]
    assert mark_done["mutation_authority"]["maximum_level"] == "additive_vontology"
    assert mark_done["static_input_bindings"][1] == [
        "tool_name",
        "record_source_processing_marker",
    ]
    assert "message_processing_marker" in mark_done["writes_context_keys"]


def test_zhan_seed_launch_contract_can_narrow_from_prior_arxiv_evidence() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflows = {
        item["workflow_id"]: item
        for item in payload.get("workflows") or []
        if isinstance(item, dict)
    }
    workflow = workflows[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID]

    resolution = resolve_workflow_launch_inputs(
        workflow_id=mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID,
        contract=workflow["launch_input_contract"],
        inputs={"arxiv_id": "2606.30544"},
        contract_source="repo_seed_test",
    )

    assert resolution.resolved_inputs["base_gmail_query"] == "2606.30544"
    assert resolution.resolved_inputs["arxiv_id"] == "2606.30544"
    assert resolution.diagnostics["status"] == "resolved"


def test_email_arxiv_schedule_bootstrap_creates_managed_interval_schedule(
    monkeypatch,
) -> None:
    from src.backend.services import (
        email_source_representation_convergence_schedule_bootstrap_service as mod,
    )

    _reset_schedule_bootstrap_state(mod)
    fake = _FakeManager()
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_ENABLE", "1")
    monkeypatch.setenv("VON_EMAIL_ARXIV_CONVERGENCE_INTERVAL_SECONDS", "7200")
    monkeypatch.setenv("VON_EMAIL_ARXIV_CONVERGENCE_MAX_RESULTS", "2")

    report = mod.ensure_email_arxiv_representation_convergence_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert report["ensured"] is True
    assert len(fake.created) == 1
    schedule = fake.created[0]
    assert schedule.workflow_id == mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID
    assert schedule.interval_seconds == 7200
    assert schedule.default_inputs["managed_schedule_key"] == (
        mod.EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY
    )
    assert schedule.default_inputs["gmail_profile"] == "#V#gmail_profile_zhan_gmail"
    assert schedule.default_inputs["gmail_max_results"] == 2
    assert "arxiv papers" in schedule.default_inputs["prompt"]


def test_email_arxiv_schedule_bootstrap_reuses_existing_matching_schedule(
    monkeypatch,
) -> None:
    from src.backend.services import (
        email_source_representation_convergence_schedule_bootstrap_service as mod,
    )

    _reset_schedule_bootstrap_state(mod)
    desired = mod._desired_schedule_config()
    existing = WorkflowSchedule.create_interval(
        desired["workflow_id"],
        interval_seconds=desired["interval_seconds"],
        user_id=desired["user_id"],
        org_id=desired["org_id"],
        namespace=desired["namespace"],
        default_inputs=dict(desired["default_inputs"]),
        description="managed",
    )
    existing.enabled = False
    fake = _FakeManager([existing])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_ENABLE", "1")

    report = mod.ensure_email_arxiv_representation_convergence_schedule()

    assert report["success"] is True
    assert report["created_count"] == 0
    assert report["updated_count"] == 1
    assert (existing.schedule_id, True) in fake.enabled_updates


def test_email_arxiv_schedule_bootstrap_disables_mismatched_schedule(
    monkeypatch,
) -> None:
    from src.backend.services import (
        email_source_representation_convergence_schedule_bootstrap_service as mod,
    )

    _reset_schedule_bootstrap_state(mod)
    desired = mod._desired_schedule_config()
    mismatched = WorkflowSchedule.create_interval(
        desired["workflow_id"],
        interval_seconds=900,
        user_id=desired["user_id"],
        org_id=desired["org_id"],
        namespace=desired["namespace"],
        default_inputs={
            "managed_schedule_key": (
                mod.EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY
            ),
            "gmail_profile": "#V#gmail_profile_zhan_gmail",
        },
        description="managed",
    )
    mismatched.enabled = True
    fake = _FakeManager([mismatched])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_ENABLE", "1")

    report = mod.ensure_email_arxiv_representation_convergence_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert report["disabled_count"] == 1
    assert (mismatched.schedule_id, False) in fake.enabled_updates
