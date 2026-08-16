from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.models import WorkflowSchedule
from src.backend.workflows.engine import WorkflowExecutor
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
    assert entry["action"]["tool_name"] == "gmail_modify_labels"
    assert entry["tool_arguments"]["message_id_context_key"] == "message_id"
    assert entry["tool_arguments"]["verify_after"] is True
    assert entry["upstream_filter"]["query_fragment"] == (
        '-label:"VON/PAPER/REPRESENTED" '
        '-label:"VON/PAPER/NEEDS_REVIEW" '
        '-label:"VON/PAPER/FAILED" '
        '-label:"VON/PAPER/SKIP"'
    )
    assert entry["represented_effects"][0]["effect_kind"] == (
        "verified_gmail_label_convergence"
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
        "inspect_existing_paper",
        "represent_arxiv_paper",
        "done",
        "failed",
    }
    assert all("claim" not in json.dumps(step).lower() for step in steps)


def _arxiv_resource_definition():
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    return build_repo_seed_workflow_definitions(
        bundle_paths=[mod._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[
            mod.ARXIV_RESOURCE_INGESTION_FROM_EMAIL_REFERENCE_WORKFLOW_ID
        ],
    )[mod.ARXIV_RESOURCE_INGESTION_FROM_EMAIL_REFERENCE_WORKFLOW_ID]


def test_arxiv_resource_workflow_reuses_existing_representation() -> None:
    definition = _arxiv_resource_definition()
    registry = ActionRegistry()
    register_control_flow_actions(registry)
    calls: list[str] = []

    def inspect(request: WorkflowActionRequest) -> WorkflowActionResult:
        calls.append("inspect")
        return WorkflowActionResult(
            status="success",
            outputs={
                "arxiv_id": request.inputs["arxiv_id"],
                "paper_concept_id": "#V#paper_on_arxiv_2607_10563_existing",
                "file_copy_concept_id": "#V#arxiv_pdf_existing",
            },
        )

    def represent(_request: WorkflowActionRequest) -> WorkflowActionResult:
        raise AssertionError("existing paper was represented again")

    registry.register(
        ActionSpec(action_id="arxiv.inspect_existing_state", handler=inspect)
    )
    registry.register(
        ActionSpec(action_id="workflow_invoke_subworkflow", handler=represent)
    )
    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "current_resource_reference": {
                "identifier": "2607.10563",
                "url": "https://arxiv.org/abs/2607.10563",
            }
        },
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert calls == ["inspect"]
    assert result.data["paper_concept_id"] == (
        "#V#paper_on_arxiv_2607_10563_existing"
    )


def test_arxiv_resource_workflow_represents_only_when_not_already_present() -> None:
    definition = _arxiv_resource_definition()
    registry = ActionRegistry()
    register_control_flow_actions(registry)
    calls: list[str] = []

    def inspect(request: WorkflowActionRequest) -> WorkflowActionResult:
        calls.append("inspect")
        return WorkflowActionResult(
            status="success",
            outputs={
                "arxiv_id": request.inputs["arxiv_id"],
                "paper_concept_id": None,
                "file_copy_concept_id": None,
            },
        )

    def represent(request: WorkflowActionRequest) -> WorkflowActionResult:
        calls.append("represent")
        assert request.inputs["arxiv_id"] == "2607.22925"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_concept_id": "#V#paper_on_arxiv_2607_22925_new",
                    "file_copy_concept_id": "#V#arxiv_pdf_new",
                    "article_readback": {
                        "concept_id": "#V#paper_on_arxiv_2607_22925_new"
                    },
                }
            },
        )

    registry.register(
        ActionSpec(action_id="arxiv.inspect_existing_state", handler=inspect)
    )
    registry.register(
        ActionSpec(action_id="workflow_invoke_subworkflow", handler=represent)
    )
    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "current_resource_reference": {
                "identifier": "2607.22925",
                "url": "https://arxiv.org/abs/2607.22925",
            }
        },
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert calls == ["inspect", "represent"]
    assert result.data["paper_concept_id"] == "#V#paper_on_arxiv_2607_22925_new"


def test_email_resource_extraction_fetches_body_needed_to_find_cited_resources() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload.get("workflows") or []
        if item.get("workflow_id") == mod.EMAIL_RESOURCE_LINK_EXTRACTION_WORKFLOW_ID
    )
    fetch_payload = next(
        step
        for step in workflow["publication_spec"]["steps"]
        if step["state_id"] == "fetch_payload"
    )
    bindings = dict(fetch_payload["static_input_bindings"])

    assert bindings["tool_name"] == "gmail_get_message"
    assert bindings["tool_arguments"]["format"] == "full"
    assert bindings["tool_arguments"]["include_body"] is True


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


def test_email_source_seed_requires_current_markers_before_verified_gmail_writes() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    seed_text = json.dumps(payload)
    assert "gmail_modify_labels" in seed_text
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
    marker_arguments = steps["check_processed_marker"]["static_input_bindings"][0][
        1
    ]
    assert marker_arguments["require_represented_artifacts"] is True
    assert "source_fingerprint" in marker_arguments
    assert "processing_authority_fingerprint" in marker_arguments
    mark_done = steps["mark_done"]
    assert mark_done["mutation_authority"]["maximum_level"] == "additive_vontology"
    assert mark_done["static_input_bindings"][1] == [
        "tool_name",
        "record_source_processing_marker",
    ]
    assert "message_processing_marker" in mark_done["writes_context_keys"]
    assert mark_done["next_state"] == "verify_done_marker"
    assert mark_done["on_failure_state"] == "project_unmarked_result"

    verify_done_marker = steps["verify_done_marker"]
    assert verify_done_marker["static_input_bindings"][1] == [
        "tool_name",
        "get_source_processing_marker",
    ]
    assert verify_done_marker["on_failure_state"] == (
        "project_unmarked_result"
    )
    assert verify_done_marker["next_state"] == "project_unmarked_result"
    assert verify_done_marker["conditional_transitions"][0]["condition_spec"] == {
        "expected": True,
        "key": "source_processing_current",
        "kind": "context_flag",
    }
    assert verify_done_marker["conditional_transitions"][0]["to_state"] == (
        "converge_gmail_labels"
    )

    converge = steps["converge_gmail_labels"]
    assert converge["mutation_authority"]["maximum_level"] == (
        "external_system_guarded"
    )
    assert converge["static_input_bindings"][1] == [
        "tool_name",
        "gmail_modify_labels",
    ]
    gmail_arguments = converge["static_input_bindings"][0][1]
    assert gmail_arguments["add_labels"] == [
        {"$context_key": "represented_label_id"}
    ]
    assert gmail_arguments["remove_labels"] == ["INBOX"]
    assert gmail_arguments["verify_after"] is True
    assert converge["conditional_transitions"][0]["to_state"] == (
        "project_done_result"
    )
    assert converge["next_state"] == "project_gmail_unverified_result"
    assert converge["on_failure_state"] == "project_gmail_unverified_result"


def test_email_source_seed_dispositions_non_converging_mail_for_human_review() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
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

    assert steps["decide_arxiv_resources"]["next_state"] == "build_review_task"
    create_review = steps["create_review_task"]
    assert create_review["mutation_authority"]["maximum_level"] == (
        "additive_vontology"
    )
    assert create_review["static_input_bindings"][1] == [
        "tool_name",
        "task_create",
    ]
    task_arguments = create_review["static_input_bindings"][0][1]
    assert task_arguments["idempotency_key"] == {
        "$context_key": "review_task_idempotency_key"
    }
    assert task_arguments["assignee_id"] == {"$context_key": "user_concept_id"}
    assert task_arguments["created_by_concept_id"] == {
        "$context_key": "user_concept_id"
    }

    mark_review = steps["mark_needs_review"]
    assert mark_review["mutation_authority"]["maximum_level"] == (
        "external_system_guarded"
    )
    assert mark_review["static_input_bindings"][1] == [
        "tool_name",
        "gmail_modify_labels",
    ]
    gmail_arguments = mark_review["static_input_bindings"][0][1]
    assert gmail_arguments["add_labels"] == [
        {"$context_key": "needs_review_label_id"}
    ]
    assert gmail_arguments["remove_labels"] == []
    assert gmail_arguments["verify_after"] is True
    assert mark_review["conditional_transitions"][0]["to_state"] == (
        "project_needs_review_result"
    )
    assert steps["project_needs_review_result"]["next_state"] == (
        "done_needs_review"
    )
    assert "done_needs_review" in steps
    assert "done_no_resources" not in steps


def test_non_converging_message_creates_one_review_task_and_preserves_inbox() -> None:
    calls: list[str] = []
    observed_task_arguments: dict[str, object] = {}
    observed_gmail_arguments: dict[str, object] = {}

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        tool_name = str(request.inputs.get("tool_name") or "")
        calls.append(tool_name)
        arguments = dict(request.inputs.get("tool_arguments") or {})
        if tool_name == "task_create":
            observed_task_arguments.update(arguments)
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "success": True,
                        "task_concept_id": "#V#task_review_message_1",
                        "changed": True,
                        "idempotent_replay": False,
                        "canonical_read_back": {
                            "task_concept_id": "#V#task_review_message_1",
                            "status": "pending",
                        },
                    }
                },
            )
        if tool_name == "gmail_list_labels":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "label_id": "Label_NEEDS_REVIEW",
                        "label_name": "VON/PAPER/NEEDS_REVIEW",
                    }
                },
            )
        assert tool_name == "gmail_modify_labels"
        observed_gmail_arguments.update(arguments)
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "gmail_label_state_verified": True,
                    "readback_label_ids": [
                        "INBOX",
                        "UNREAD",
                        "Label_NEEDS_REVIEW",
                    ],
                }
            },
        )

    definition = _exact_message_definition(
        "decide_arxiv_resources",
        "build_review_task",
        "create_review_task",
        "ensure_needs_review_label",
        "resolve_needs_review_label",
        "mark_needs_review",
        "project_needs_review_result",
        "done_needs_review",
        "failed",
        initial_state="decide_arxiv_resources",
    )
    registry = ActionRegistry()
    register_control_flow_actions(registry)
    registry.register(ActionSpec(action_id="workflow_mcp.invoke_tool", handler=handler))
    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "arxiv_resource_references": [],
            "message_id": "gmail-message-1",
            "gmail_profile": "vonwitbrock-gmail",
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
        },
    )

    assert result.completed is True
    assert result.final_state == "done_needs_review"
    assert calls == ["task_create", "gmail_list_labels", "gmail_modify_labels"]
    assert observed_task_arguments["idempotency_key"] == (
        "paper-ingestion-review:v1:gmail:vonwitbrock-gmail:gmail-message-1"
    )
    assert observed_task_arguments["assignee_id"] == "#V#michael_witbrock"
    assert observed_task_arguments["organisation_concept_id"] == (
        "#V#university_of_auckland_strong_ai_lab"
    )
    assert observed_gmail_arguments["add_labels"] == ["Label_NEEDS_REVIEW"]
    assert observed_gmail_arguments["remove_labels"] == []
    assert "INBOX" in result.data["needs_review_gmail_readback_label_ids"]
    assert result.data["review_task_concept_id"] == "#V#task_review_message_1"
    assert result.data["result"] == {
        "source_item_id": "gmail-message-1",
        "source_profile": "vonwitbrock-gmail",
        "disposition": "needs_review",
        "review_task_concept_id": "#V#task_review_message_1",
        "gmail_readback_label_ids": [
            "INBOX",
            "UNREAD",
            "Label_NEEDS_REVIEW",
        ],
    }


def test_parent_batch_surfaces_human_review_disposition_count() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.engine import evaluate_transition_condition_spec

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    parent = next(
        item
        for item in payload.get("workflows") or []
        if item.get("workflow_id") == mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID
    )
    process = next(
        step
        for step in parent["publication_spec"]["steps"]
        if step.get("state_id") == "process_messages"
    )

    mappings = {
        item["context_key"]: item["tool_output_field"]
        for item in process["tool_output_mapping_specs"]
    }
    assert mappings["message_final_state_counts"] == "for_each_final_state_counts"
    project = next(
        step
        for step in parent["publication_spec"]["steps"]
        if step.get("state_id") == "project_batch_result"
    )
    review_transition = project["conditional_transitions"][1]
    assert review_transition["to_state"] == "done_with_review_required"
    assert review_transition["condition_spec"]["conditions"][0]["key"] == (
        "message_final_state_counts.#V#workflow_step_email_arxiv_ingestion_from_message_workflow_done_needs_review"
    )
    assert evaluate_transition_condition_spec(
        context={
            "message_final_state_counts": {
                "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_done": 2,
                "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_done_needs_review": 1,
            },
            "message_error_count": 0,
        },
        condition_spec=review_transition["condition_spec"],
    )


def test_parent_batch_preserves_all_22_item_results_and_one_retryable_failure() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[mod._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID],
    )[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID]
    definition = replace(
        definition,
        initial_state="project_batch_result",
        states={
            state_id: definition.states[state_id]
            for state_id in ("project_batch_result", "done_with_retryable_failures")
        },
        termination_states=(),
    )

    unique_arxiv_ids = [f"2606.{index:05d}" for index in range(1, 20)]
    successful_arxiv_ids = [*unique_arxiv_ids, unique_arxiv_ids[0], unique_arxiv_ids[1]]
    successful_items = [
        {
            "index": index,
            "item": {"id": f"gmail-message-{index + 1}"},
            "completed": True,
            "final_state": "done",
            "error": None,
            "result": {
                "result": {
                    "schema_version": "gmail_arxiv_message_result.v1",
                    "source_item_id": f"gmail-message-{index + 1}",
                    "disposition": "represented",
                    "arxiv_ids": [arxiv_id],
                    "paper_concept_ids": [f"#V#paper_{arxiv_id.replace('.', '_')}"],
                    "gmail_label_state_verified": True,
                }
            },
        }
        for index, arxiv_id in enumerate(successful_arxiv_ids)
    ]
    failed_item = {
        "index": 21,
        "item": {"id": "gmail-message-22"},
        "completed": False,
        "final_state": "failed_unmarked",
        "error": "workflow_failed_terminal_state",
        "result": {
            "result": {
                "schema_version": "gmail_arxiv_message_result.v1",
                "source_item_id": "gmail-message-22",
                "disposition": "failed_unmarked",
                "arxiv_ids": ["2606.99999"],
            }
        },
    }
    iteration_results = [*successful_items, failed_item]
    iteration_errors = [
        {
            "index": 21,
            "item": {"id": "gmail-message-22"},
            "final_state": "failed_unmarked",
            "error": "workflow_failed_terminal_state",
            "result": failed_item["result"],
        }
    ]

    registry = ActionRegistry()
    register_control_flow_actions(registry)
    result = WorkflowExecutor(registry=registry, max_transitions=4).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "gmail_profile": "vonwitbrock-gmail",
            "effective_gmail_query": "newer_than:30d arxiv",
            "gmail_max_results": 100,
            "gmail_result_size_estimate": 22,
            "gmail_page_token": None,
            "gmail_next_page_token": None,
            "message_item_count": 22,
            "message_selected_item_count": 22,
            "message_unattempted_count": 0,
            "message_success_count": 21,
            "message_error_count": 1,
            "message_partial_success": True,
            "message_final_state_counts": {
                "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_done": 21,
                "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_failed_unmarked": 1,
            },
            "message_iteration_results": iteration_results,
            "message_iteration_errors": iteration_errors,
        },
    )

    assert result.completed is True
    assert result.final_state == "done_with_retryable_failures"
    batch_result = result.data["batch_result"]
    assert batch_result["schema_version"] == "gmail_arxiv_batch_result.v1"
    assert batch_result["discovered_message_count"] == 22
    assert batch_result["page_token_used"] is None
    assert batch_result["successful_message_count"] == 21
    assert batch_result["failed_message_count"] == 1
    assert batch_result["partial_success"] is True
    assert batch_result["items"] == iteration_results
    assert batch_result["failures"] == iteration_errors
    represented_ids = {
        arxiv_id
        for item in batch_result["items"]
        if item["completed"]
        for arxiv_id in item["result"]["result"]["arxiv_ids"]
    }
    assert len(represented_ids) == 19
    assert batch_result["items"][-1]["result"]["result"]["disposition"] == (
        "failed_unmarked"
    )


def test_parent_batch_passes_prior_continuation_token_to_gmail_listing() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[mod._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID],
    )[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID]
    definition = replace(
        definition,
        initial_state="list_messages",
        states={
            state_id: definition.states[state_id]
            for state_id in ("list_messages", "decide_messages", "done_no_messages")
        },
        termination_states=(),
    )
    observed_arguments: dict[str, object] = {}

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        assert request.inputs["tool_name"] == "gmail_list_messages"
        observed_arguments.update(request.inputs["tool_arguments"])
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "messages": [],
                    "profile": "vonwitbrock-gmail",
                    "effective_query": {"effective_query_string": "arxiv.org"},
                    "nextPageToken": None,
                    "resultSizeEstimate": 22,
                }
            },
        )

    registry = ActionRegistry()
    register_control_flow_actions(registry)
    registry.register(ActionSpec(action_id="workflow_mcp.invoke_tool", handler=handler))
    result = WorkflowExecutor(registry=registry, max_transitions=4).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "gmail_profile": "vonwitbrock-gmail",
            "gmail_query": "arxiv.org",
            "gmail_max_results": 100,
            "gmail_page_token": "gmail-page-2",
        },
    )

    assert result.completed is True
    assert result.final_state == "done_no_messages"
    assert observed_arguments["page_token"] == "gmail-page-2"


def test_parent_batch_allows_gmail_to_omit_optional_pagination_metadata() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[mod._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID],
    )[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID]
    definition = replace(
        definition,
        initial_state="list_messages",
        states={
            state_id: definition.states[state_id]
            for state_id in ("list_messages", "decide_messages", "done_no_messages")
        },
        termination_states=(),
    )

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        assert request.inputs["tool_name"] == "gmail_list_messages"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "messages": [],
                    "profile": "vonwitbrock-gmail",
                    "effective_query": {"effective_query_string": "arxiv.org"},
                }
            },
        )

    registry = ActionRegistry()
    register_control_flow_actions(registry)
    registry.register(ActionSpec(action_id="workflow_mcp.invoke_tool", handler=handler))
    result = WorkflowExecutor(registry=registry, max_transitions=4).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "gmail_profile": "vonwitbrock-gmail",
            "gmail_query": "arxiv.org",
            "gmail_max_results": 100,
        },
    )

    assert result.completed is True
    assert result.final_state == "done_no_messages"
    assert "gmail_next_page_token" not in result.data
    assert "gmail_result_size_estimate" not in result.data


def test_parent_batch_result_is_lossless_across_durable_checkpoints() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.durable.checkpoint_context_projection import (
        project_workflow_context_for_checkpoint,
    )
    from src.backend.workflows.durable.durable_executor import (
        _execution_required_checkpoint_context_keys,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[mod._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID],
    )[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID]
    lossless_keys = _execution_required_checkpoint_context_keys(definition)
    assert "batch_result" in lossless_keys

    batch_result = {
        "schema_version": "gmail_arxiv_batch_result.v1",
        "items": [
            {
                "index": index,
                "source_item_id": f"gmail-message-{index + 1}",
                "paper_concept_ids": [f"#V#paper_{index + 1}"],
                "evidence": "x" * 200,
            }
            for index in range(100)
        ],
    }
    projected = project_workflow_context_for_checkpoint(
        {"batch_result": batch_result},
        max_value_bson_bytes=512,
        lossless_keys=lossless_keys,
    )

    assert projected["batch_result"] == batch_result
    assert len(projected["batch_result"]["items"]) == 100


def test_email_source_seed_routes_exact_message_unit_and_explicit_batch() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflows = {
        item["workflow_id"]: item
        for item in payload.get("workflows") or []
        if isinstance(item, dict)
    }
    parent = workflows[mod.ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID]
    child = workflows[mod.EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID]

    def relations(workflow: dict) -> dict[str, dict]:
        return {
            item["predicate"]: json.loads(item["text"])
            for item in workflow.get("text_relations") or []
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        }

    parent_relations = relations(parent)
    child_relations = relations(child)
    parent_lifecycle = parent_relations["#V#hasWorkflowLifecycleJson"]
    parent_routing = parent_relations["#V#hasWorkflowRoutingProfileJson"]
    child_lifecycle = child_relations["#V#hasWorkflowLifecycleJson"]
    child_routing = child_relations["#V#hasWorkflowRoutingProfileJson"]
    child_discovery = child_relations["#V#hasWorkflowDiscoveryExemplarsJson"]

    assert parent_lifecycle["routing_eligible"] is True
    assert parent_routing["routing_eligible"] is True
    assert parent_routing["explicit_workflow_context_required"] is False
    assert parent_routing["prefer_existing_capability"] is True

    assert child_lifecycle["routing_eligible"] is True
    assert child_lifecycle["postconditions_verified"] is True
    assert child_routing["routing_eligible"] is True
    assert child_routing["prefer_existing_capability"] is True
    assert any("exact Gmail" in example for example in child_discovery["examples"])
    assert any(
        "does not choose a message" in note
        for note in child_discovery["routing_notes"]
    )
    assert any(
        "include_body=true" in note and "older message" in note
        for note in child_discovery["routing_notes"]
    )


def test_email_message_launch_contract_accepts_exact_selected_message() -> None:
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload.get("workflows") or []
        if item.get("workflow_id")
        == mod.EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID
    )

    current_message = {
        "id": "gmail-message-1",
        "message_id": "gmail-message-1",
        "subject": "A paper https://arxiv.org/abs/2606.12345",
    }
    resolution = resolve_workflow_launch_inputs(
        workflow_id=mod.EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID,
        contract=workflow["launch_input_contract"],
        inputs={
            "gmail_profile": "#V#gmail_profile_vonwitbrock_gmail",
            "current_message": current_message,
        },
        contract_source="repo_seed_test",
    )

    assert resolution.diagnostics["status"] == "resolved"
    assert resolution.resolved_inputs == {
        "gmail_profile": "#V#gmail_profile_vonwitbrock_gmail",
        "current_message": current_message,
    }


def _exact_message_definition(*state_ids: str, initial_state: str):
    from src.backend.services import (
        email_source_representation_convergence_workflow_vontology_service as mod,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[mod._REPO_SEED_ASSET_PATH],
        target_workflow_ids=[mod.EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID],
    )[mod.EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID]
    return replace(
        definition,
        initial_state=initial_state,
        states={state_id: definition.states[state_id] for state_id in state_ids},
        termination_states=(),
    )


def _run_exact_message_slice(definition, handler):
    registry = ActionRegistry()
    register_control_flow_actions(registry)
    registry.register(ActionSpec(action_id="workflow_mcp.invoke_tool", handler=handler))
    return WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "message_id": "gmail-message-1",
            "gmail_profile": "vonwitbrock-gmail",
            "represented_label_id": "Label_3",
            "source_fingerprint": (
                "gmail-message-id:v1:vonwitbrock-gmail:gmail-message-1"
            ),
            "processing_authority_fingerprint": "email-arxiv-ingestion:v2",
            "arxiv_iteration_results": [
                {"paper_concept_id": "#V#paper_2606_12345", "arxiv_id": "2606.12345"}
            ],
        },
    )


def test_exact_message_workflow_records_then_reads_back_marker_before_success() -> None:
    calls: list[str] = []

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        tool_name = str(request.inputs.get("tool_name") or "")
        calls.append(tool_name)
        if tool_name == "record_source_processing_marker":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "message_processing_marker": "#V#marker_1",
                        "processed_message_id": "gmail-message-1",
                        "paper_concept_id": "#V#paper_2606_12345",
                        "paper_concept_ids": ["#V#paper_2606_12345"],
                        "file_copy_concept_id": "#V#file_2606_12345",
                        "file_copy_concept_ids": ["#V#file_2606_12345"],
                        "arxiv_id": "2606.12345",
                        "arxiv_ids": ["2606.12345"],
                    }
                },
            )
        if tool_name == "gmail_modify_labels":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "gmail_label_state_verified": True,
                        "readback_label_ids": ["Label_3"],
                        "modify_reconciled_after_error": False,
                    }
                },
            )
        assert tool_name == "get_source_processing_marker"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "source_processing_marker_exists": True,
                    "message_processing_marker": "#V#marker_1",
                    "processed_message_id": "gmail-message-1",
                    "paper_concept_id": "#V#paper_2606_12345",
                    "paper_concept_ids": ["#V#paper_2606_12345"],
                    "file_copy_concept_id": "#V#file_2606_12345",
                    "file_copy_concept_ids": ["#V#file_2606_12345"],
                    "arxiv_id": "2606.12345",
                    "arxiv_ids": ["2606.12345"],
                    "source_processing_current": True,
                    "represented_artifacts_exist": True,
                }
            },
        )

    definition = _exact_message_definition(
        "mark_done",
        "verify_done_marker",
        "converge_gmail_labels",
        "project_done_result",
        "project_unmarked_result",
        "project_gmail_unverified_result",
        "done",
        "failed_unmarked",
        "failed_gmail_state",
        initial_state="mark_done",
    )
    result = _run_exact_message_slice(definition, handler)

    assert result.completed is True
    assert result.final_state == "done"
    assert calls == [
        "record_source_processing_marker",
        "get_source_processing_marker",
        "gmail_modify_labels",
    ]
    assert result.data["source_processing_marker_exists"] is True
    assert result.data["message_processing_marker"] == "#V#marker_1"
    assert result.data["result"]["disposition"] == "represented"
    assert result.data["result"]["paper_concept_id"] == (
        "#V#paper_2606_12345"
    )
    assert result.data["result"]["arxiv_id"] == "2606.12345"
    assert result.data["result"]["gmail_label_state_verified"] is True


@pytest.mark.parametrize("failure_mode", ["write_failed", "readback_missing"])
def test_exact_message_workflow_never_claims_success_without_verified_marker(
    failure_mode: str,
) -> None:
    calls: list[str] = []

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        tool_name = str(request.inputs.get("tool_name") or "")
        calls.append(tool_name)
        if tool_name == "record_source_processing_marker":
            if failure_mode == "write_failed":
                return WorkflowActionResult(status="failure", error="write failed")
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "message_processing_marker": "#V#marker_1",
                        "processed_message_id": "gmail-message-1",
                        "paper_concept_id": "#V#paper_2606_12345",
                        "paper_concept_ids": ["#V#paper_2606_12345"],
                        "file_copy_concept_id": "#V#file_2606_12345",
                        "file_copy_concept_ids": ["#V#file_2606_12345"],
                        "arxiv_id": "2606.12345",
                        "arxiv_ids": ["2606.12345"],
                    }
                },
            )
        assert tool_name == "get_source_processing_marker"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "source_processing_marker_exists": False,
                    "message_processing_marker": None,
                    "processed_message_id": None,
                    "paper_concept_id": None,
                    "paper_concept_ids": [],
                    "file_copy_concept_id": None,
                    "file_copy_concept_ids": [],
                    "arxiv_id": None,
                    "arxiv_ids": [],
                    "source_processing_current": False,
                    "represented_artifacts_exist": False,
                }
            },
        )

    definition = _exact_message_definition(
        "mark_done",
        "verify_done_marker",
        "project_unmarked_result",
        "done",
        "failed_unmarked",
        initial_state="mark_done",
    )
    result = _run_exact_message_slice(definition, handler)

    assert result.completed is False
    assert result.final_state == "failed_unmarked"
    assert result.data["result"]["disposition"] == "failed_unmarked"
    assert calls == (
        ["record_source_processing_marker", "record_source_processing_marker"]
        if failure_mode == "write_failed"
        else ["record_source_processing_marker", "get_source_processing_marker"]
    )


def test_exact_message_workflow_reuses_current_marker_then_converges_gmail() -> None:
    calls: list[str] = []

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        tool_name = str(request.inputs.get("tool_name") or "")
        calls.append(tool_name)
        if tool_name == "gmail_modify_labels":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "gmail_label_state_verified": True,
                        "readback_label_ids": ["Label_3"],
                        "modify_reconciled_after_error": False,
                    }
                },
            )
        assert tool_name == "get_source_processing_marker"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "source_processing_marker_exists": True,
                    "message_processing_marker": "#V#marker_existing",
                    "processed_message_id": "gmail-message-1",
                    "paper_concept_id": "#V#paper_2606_12345",
                    "file_copy_concept_id": "#V#file_2606_12345",
                    "arxiv_id": "2606.12345",
                    "source_processing_current": True,
                    "represented_artifacts_exist": True,
                    "paper_concept_ids": ["#V#paper_2606_12345"],
                    "file_copy_concept_ids": ["#V#file_2606_12345"],
                    "arxiv_ids": ["2606.12345"],
                }
            },
        )

    definition = _exact_message_definition(
        "check_processed_marker",
        "converge_gmail_labels",
        "project_done_result",
        "project_gmail_unverified_result",
        "done",
        "failed_gmail_state",
        "failed",
        initial_state="check_processed_marker",
    )
    result = _run_exact_message_slice(definition, handler)

    assert result.completed is True
    assert result.final_state == "done"
    assert calls == ["get_source_processing_marker", "gmail_modify_labels"]


def test_exact_message_workflow_upgrades_legacy_marker_before_gmail() -> None:
    calls: list[str] = []
    marker_reads = 0

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        nonlocal marker_reads
        tool_name = str(request.inputs.get("tool_name") or "")
        calls.append(tool_name)
        if tool_name == "record_source_processing_marker":
            assert request.inputs["tool_arguments"]["source_fingerprint"] == (
                "gmail-message-id:v1:vonwitbrock-gmail:gmail-message-1"
            )
            return WorkflowActionResult(status="success", outputs={"result": {}})
        if tool_name == "gmail_modify_labels":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "gmail_label_state_verified": True,
                        "readback_label_ids": ["Label_3"],
                        "modify_reconciled_after_error": False,
                    }
                },
            )
        assert tool_name == "get_source_processing_marker"
        marker_reads += 1
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "source_processing_marker_exists": True,
                    "message_processing_marker": "#V#marker_existing",
                    "processed_message_id": "gmail-message-1",
                    "paper_concept_id": "#V#paper_2606_12345",
                    "file_copy_concept_id": "#V#file_2606_12345",
                    "arxiv_id": "2606.12345",
                    "paper_concept_ids": ["#V#paper_2606_12345"],
                    "file_copy_concept_ids": ["#V#file_2606_12345"],
                    "arxiv_ids": ["2606.12345"],
                    "represented_artifacts_exist": True,
                    "source_processing_current": marker_reads > 1,
                }
            },
        )

    definition = _exact_message_definition(
        "check_processed_marker",
        "upgrade_legacy_marker",
        "verify_done_marker",
        "converge_gmail_labels",
        "project_done_result",
        "project_unmarked_result",
        "project_gmail_unverified_result",
        "done",
        "failed",
        "failed_unmarked",
        "failed_gmail_state",
        initial_state="check_processed_marker",
    )
    result = _run_exact_message_slice(definition, handler)

    assert result.completed is True
    assert result.final_state == "done"
    assert calls == [
        "get_source_processing_marker",
        "record_source_processing_marker",
        "get_source_processing_marker",
        "gmail_modify_labels",
    ]


def test_exact_message_workflow_does_not_succeed_when_gmail_readback_fails() -> None:
    calls: list[str] = []

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        calls.append(str(request.inputs.get("tool_name") or ""))
        return WorkflowActionResult(status="failure", error="gmail state unverified")

    definition = _exact_message_definition(
        "converge_gmail_labels",
        "project_gmail_unverified_result",
        "done",
        "failed_gmail_state",
        initial_state="converge_gmail_labels",
    )
    result = _run_exact_message_slice(definition, handler)

    assert result.completed is False
    assert result.final_state == "failed_gmail_state"
    assert result.data["result"]["disposition"] == (
        "represented_gmail_unverified"
    )
    assert calls == ["gmail_modify_labels", "gmail_modify_labels"]


def test_exact_message_workflow_does_not_mark_after_arxiv_ingestion_failure() -> None:
    from src.backend.workflows.action_registry import ActionRegistry

    calls: list[str] = []

    def failed_ingestion(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls.append("workflow_control.for_each")
        return WorkflowActionResult(
            status="success",
            outputs={
                "for_each_error_count": 1,
                "for_each_item_count": 1,
                "for_each_partial_success": False,
                "for_each_success_count": 0,
                "iteration_results": [{"status": "failed"}],
                "iteration_errors": [
                    {"item_index": 0, "error": "paper representation failed"}
                ],
            },
        )

    def marker_must_not_run(_request: WorkflowActionRequest) -> WorkflowActionResult:
        raise AssertionError("marker action ran after failed arXiv ingestion")

    definition = _exact_message_definition(
        "ingest_arxiv_resources",
        "mark_done",
        "verify_done_marker",
        "project_unmarked_result",
        "done",
        "failed_unmarked",
        initial_state="ingest_arxiv_resources",
    )
    registry = ActionRegistry()
    registry.register(
        ActionSpec(action_id="workflow_control.for_each", handler=failed_ingestion)
    )
    register_control_flow_actions(registry)
    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=marker_must_not_run)
    )
    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"arxiv_resource_references": [{"identifier": "2606.12345"}]},
    )

    assert result.completed is False
    assert result.final_state == "failed_unmarked"
    assert calls == ["workflow_control.for_each"]


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
        inputs={
            "arxiv_id": "2606.30544",
            "next_page_token": "gmail-page-2",
        },
        contract_source="repo_seed_test",
    )

    assert resolution.resolved_inputs["base_gmail_query"] == "2606.30544"
    assert resolution.resolved_inputs["arxiv_id"] == "2606.30544"
    assert resolution.resolved_inputs["gmail_page_token"] == "gmail-page-2"
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
    assert schedule.default_inputs["gmail_profile"] == (
        "#V#gmail_profile_vonwitbrock_gmail"
    )
    assert schedule.default_inputs["base_gmail_query"] == "arxiv.org"
    assert schedule.default_inputs["gmail_max_results"] == 2
    assert "arxiv" in schedule.default_inputs["prompt"].lower()
    assert schedule.enabled is False
    assert report["schedule_enabled"] is False


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

    report = mod.ensure_email_arxiv_representation_convergence_schedule(
        activate=True
    )

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


def test_email_arxiv_schedule_bootstrap_replaces_inert_legacy_schedule(
    monkeypatch,
) -> None:
    from src.backend.services import (
        email_source_representation_convergence_schedule_bootstrap_service as mod,
    )

    _reset_schedule_bootstrap_state(mod)
    desired = mod._desired_schedule_config()
    legacy = WorkflowSchedule.create_interval(
        desired["workflow_id"],
        interval_seconds=desired["interval_seconds"],
        user_id=desired["user_id"],
        org_id=desired["org_id"],
        namespace=desired["namespace"],
        default_inputs={
            **dict(desired["default_inputs"]),
            "managed_schedule_key": "email_arxiv_representation_convergence_v1",
            "gmail_profile": "#V#gmail_profile_zhan_gmail",
            "base_gmail_query": "arxiv.org newer_than:365d",
        },
        description="email_arxiv_representation_convergence_v1",
    )
    legacy.enabled = True
    legacy.next_run_at = None
    fake = _FakeManager([legacy])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)

    report = mod.ensure_email_arxiv_representation_convergence_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert report["disabled_count"] == 1
    assert (legacy.schedule_id, False) in fake.enabled_updates
    assert fake.created[0].enabled is False
    assert fake.created[0].next_run_at is not None
