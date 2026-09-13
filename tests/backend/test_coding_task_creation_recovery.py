"""JVNAUTOSCI-2750: isolated ordinary-turn → native task → worker replay.

No real task, invitation, message, workflow, or model request is issued.
"""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import mongomock
import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.integrations.internal_mcp import build_default_catalogue, catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import (
    concept_service,
    conversation_concept_service as conversations,
)
from src.backend.services import organisation_membership_service as membership
from src.backend.services import (
    task_management_service as tasks,
    text_value_service as texts,
)
from src.backend.services.adaptive_turn_service import (
    _trusted_tool_payload,
    _model_visible_input_schema,
)


@pytest.fixture
def native_store(monkeypatch):
    db = mongomock.MongoClient()["isolated_coding_task_acceptance_2750"]
    db.concepts.create_index("concept_id", unique=True)
    monkeypatch.setattr("src.backend.db.mongo_client.get_db", lambda: db)
    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_concepts_collection", lambda: db.concepts
    )
    monkeypatch.setattr(ConceptsRepository, "collection", lambda: db.concepts)
    # The fixture does not replicate the ontology catalogue used for target
    # visibility. Actor/org query filtering and the assignment checks stay real.
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.sanitize_concept_document",
        lambda doc: doc,
    )
    monkeypatch.setattr(TextRelationsRepository, "collection", lambda: db.relations)
    monkeypatch.setattr(TextValuesRepository, "collection", lambda: db.values)
    monkeypatch.setattr(texts, "can_access_concept", lambda _: True)
    monkeypatch.setattr(texts, "filter_accessible_concept_ids", lambda ids: set(ids))
    monkeypatch.setattr(texts, "_emit_text_relation_mutation_event", lambda **_: None)
    monkeypatch.setattr(texts, "_invalidate_stats_for_predicate_change", lambda _: None)
    monkeypatch.setattr(
        texts,
        "_invalidate_workflow_routing_projection_for_text_relation_change",
        lambda **_: None,
    )
    monkeypatch.setattr(
        tasks, "resolve_task_work_product", lambda _: {"status": "missing"}
    )
    monkeypatch.setattr(tasks, "maybe_launch_task_created_workflow", lambda **_: None)
    monkeypatch.setattr(tasks, "maybe_launch_task_status_workflow", lambda **_: None)
    monkeypatch.setattr(
        tasks, "maybe_launch_effort_unit_completed_workflow", lambda **_: None
    )
    monkeypatch.setattr(tasks, "ensure_effort_unit_ontology", lambda **_: None)
    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda cid: {
            "relationships": {
                "is_an_instance_of": (
                    ["#V#coding_agent"] if cid == "#V#worker" else ["#V#person"]
                )
            }
        },
    )
    monkeypatch.setattr(
        membership,
        "is_user_member_of_organisation",
        lambda user, org: user in {"#V#alice", "#V#worker"} and org == "#V#lab",
    )
    monkeypatch.setattr(
        tasks,
        "is_user_member_of_organisation",
        membership.is_user_member_of_organisation,
    )
    monkeypatch.setattr(
        conversations,
        "get_or_create_conversation_concept",
        lambda **kw: conversations._generate_conversation_concept_id(kw["session_id"]),
    )
    monkeypatch.setattr(
        conversations,
        "get_conversation_concept",
        lambda cid: {"session_id": "fixture-source", "name": "Isolated acceptance"},
    )
    return db


@pytest.fixture
def gateway():
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def payload(gateway, **changes):
    return _trusted_tool_payload(
        gateway=gateway,
        tool_name="task_create",
        model_payload={
            "title": "Isolated coding assignment acceptance",
            "description": "Fixture only; never launch coding work.",
            "assignee_concept_id": "#V#worker",
            "report_to_concept_id": "#V#alice",
            "requested_model": "gpt-6-astra",
            "requested_reasoning_effort": "high",
            "created_by_concept_id": "#V#spoof",
            "organisation_concept_id": "#V#spoof",
            "originating_session_id": "spoof",
            "session_id": "spoof",
            "request_id": "spoof",
            **changes,
        },
        trusted_argument_values={
            "actor_user_concept_id": "#V#alice",
            "actor_organisation_concept_id": "#V#lab",
            "turn_namespace": "#V#alice@lab",
            "conversation_id": "fixture-source",
            "turn_id": "fixture-turn",
        },
    )


def load_worker(path):
    spec = spec_from_file_location("fixture_worker", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ordinary_create_retry_native_readback_and_worker_settings(
    native_store, gateway, monkeypatch
):
    request = payload(gateway)
    assert request["created_by_concept_id"] == "#V#alice"
    assert request["organisation_concept_id"] == "#V#lab"
    assert request["originating_session_id"] == "fixture-source"
    assert request["session_id"] is None
    with override_current_actor("#V#alice", "#V#lab"):
        first = gateway.invoke("task_create", request).payload
        if not first.get("success") and first.get("task_concept_id"):
            tasks.get_task(first["task_concept_id"])
        assert first.get("success"), first
        repeated = gateway.invoke("task_create", request).payload
        assert repeated.get("success"), repeated
        task = tasks.get_task(first["task_concept_id"])
    assert repeated["task_concept_id"] == task["task_concept_id"]
    assert repeated["changed"] is False
    assert native_store.concepts.count_documents({}) == 1
    assert task["created_by_concept_id"] == "#V#alice"
    assert task["organisation_concept_id"] == "#V#lab"
    assert task["assignee_concept_id"] == "#V#worker"
    assert task["report_to_concept_id"] == "#V#alice"
    assert task[
        "originating_conversation_id"
    ] == conversations._generate_conversation_concept_id("fixture-source")
    assert task["requested_model"] == "gpt-6-astra"
    assert task["requested_reasoning_effort"] == "high"
    assert task["project_concept_id"] is None and task["collection_concept_ids"] == []
    assert task["parent_task_concept_id"] is None
    assert first["canonical_readback"]["task_execution_verified"] is False

    worker = load_worker(
        Path(__file__).resolve().parents[2] / "scripts/codex_von_worker.py"
    )
    config = {
        "agent_id": "#V#worker",
        "delegator_id": "#V#alice",
        "organisation_id": "#V#lab",
    }
    api = worker.Von(config)
    monkeypatch.setattr(
        tasks, "search_tasks", lambda **_: {"tasks": [task], "total": 1}
    )
    monkeypatch.setattr(
        tasks, "list_task_comments", lambda *a, **kw: {"comments": [], "total": 0}
    )
    monkeypatch.setattr(api.messages, "get_messages_for_user", lambda *a, **kw: [])
    assert list(api.pending()) == [task]
    assert worker.resolve_execution_settings(
        config, api.inputs(api.task(task["task_concept_id"]))
    ) == {
        "model": "gpt-6-astra",
        "model_source": "task",
        "reasoning_effort": "high",
        "reasoning_effort_source": "task",
    }
    from src.backend.services import shared_conversation_service as shares

    monkeypatch.setattr(shares, "list_invites_for_user", lambda **_: [])
    assert api.conversation(task)["available"] is False


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"assignee_concept_id": "#V#outsider"}, "task_assignment_scope_denied"),
        ({"report_to_concept_id": "#V#outsider"}, "task_report_scope_denied"),
        ({"assignee_id": "#V#alice"}, "INVALID_PARAM"),
        ({"reports_to_concept_id": "#V#outsider"}, "INVALID_PARAM"),
        (
            {"parent_task_concept_id": "#V#required_parent"},
            "task_create_parent_unsupported",
        ),
    ],
)
def test_invalid_assignment_or_parent_has_no_partial_creation(
    native_store, gateway, changes, code
):
    with override_current_actor("#V#alice", "#V#lab"):
        result = gateway.invoke("task_create", payload(gateway, **changes)).payload
    assert result["error_code"] == code
    assert native_store.concepts.count_documents({}) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("assignee_concept_id", "#V#alice"),
        ("report_to_concept_id", None),
        ("requested_model", "gpt-5.6-terra"),
        ("requested_reasoning_effort", "medium"),
        ("originating_conversation_id", "#V#wrong"),
        ("task_concept_id", "#V#wrong"),
    ],
)
def test_retry_does_not_verify_wrong_assignment_preferences_or_identity(
    native_store, gateway, monkeypatch, field, value
):
    with override_current_actor("#V#alice", "#V#lab"):
        first = gateway.invoke("task_create", payload(gateway)).payload
        assert first.get("success"), first
        wrong = {**first["canonical_read_back"], field: value}
        monkeypatch.setattr(
            tasks, "find_task_by_agent_creation_fingerprint", lambda **_: wrong
        )
        result = gateway.invoke("task_create", payload(gateway)).payload
    assert result["success"] is False
    assert result["changed"] is False
    assert field in result["required_field_mismatches"]
    assert native_store.concepts.count_documents({}) == 1


def test_create_schema_exposes_preferences_without_exposing_trusted_creator(gateway):
    schema = _model_visible_input_schema(gateway.get_method_definition("task_create"))
    assert {
        "assignee_id",
        "assignee_concept_id",
        "report_to_concept_id",
        "requested_model",
        "requested_reasoning_effort",
    } <= schema["properties"].keys()
    assert "created_by_concept_id" not in schema["properties"]


@pytest.mark.parametrize(
    "field,value,hint",
    [
        ("requested_model", "DGX Codex", "assignee_concept_id"),
        ("requested_model", "Codex DGX", "assignee_concept_id"),
        ("requested_model", "#V#codex_dgx", "assignee_concept_id"),
        ("requested_model", "CodexDGX", "exact model ID"),
        ("requested_model", "Astra", "exact model ID"),
        ("requested_reasoning_effort", "extra_high", "xhigh"),
        ("requested_reasoning_effort", "extra-high", "xhigh"),
    ],
)
def test_malformed_preferences_reject_before_writes_and_corrected_retry_is_unique(
    native_store, gateway, field, value, hint
):
    with override_current_actor("#V#alice", "#V#lab"):
        invalid = payload(gateway, **{field: value})
        rejected = gateway.invoke("task_create", invalid).payload
        assert rejected["error_code"] == "INVALID_DATA", rejected
        assert hint in rejected["error"]
        assert rejected["changed"] is False
        assert native_store.concepts.count_documents({}) == 0
        assert native_store.relations.count_documents({}) == 0

        # The canonical service also protects non-MCP task creation.
        with pytest.raises(tasks.InvalidTaskDataError, match=hint):
            tasks.create_task(
                title="Invalid fixture", description="Never launch", **{field: value}
            )
        assert native_store.concepts.count_documents({}) == 0

        corrected = payload(gateway, requested_reasoning_effort="xhigh")
        created = gateway.invoke("task_create", corrected).payload
        assert created["success"], created
        task_id = created["task_concept_id"]
        repeated = gateway.invoke("task_create", corrected).payload
        assert repeated["success"] and repeated["task_concept_id"] == task_id
        assert repeated["changed"] is False
        before = tasks.get_task(task_id)
        assert before["requested_model"] == "gpt-6-astra"
        assert before["requested_reasoning_effort"] == "xhigh"
        assert before["assignee_concept_id"] == "#V#worker"

        # Reject the complete edit before status/notes or preferences can change.
        rejected_update = gateway.invoke(
            "task_update_fields",
            {
                "task_concept_id": task_id,
                "fields": {
                    "status": "completed",
                    "notes": "Must not persist",
                    field: value,
                },
            },
        ).payload
        assert rejected_update["error_code"] == "INVALID_DATA", rejected_update
        assert hint in rejected_update["error"]
        assert tasks.get_task(task_id) == before
        assert native_store.concepts.count_documents({}) == 1


def test_exact_future_execution_tokens_are_preserved(native_store, gateway):
    # Availability belongs to the execution provider, not a frozen task allowlist.
    model = "provider/Model-Preview_vNext:2026"
    effort = "future_effort"
    with override_current_actor("#V#alice", "#V#lab"):
        result = gateway.invoke(
            "task_create",
            payload(gateway, requested_model=model, requested_reasoning_effort=effort),
        ).payload
        assert result["success"], result
        task_id = result["task_concept_id"]
        task = tasks.get_task(task_id)
        assert task["requested_model"] == model
        assert task["requested_reasoning_effort"] == effort
        tasks.update_task_fields(
            task_id, fields={"requested_reasoning_effort": "ultra"}
        )
        task = tasks.get_task(task_id)
        worker = load_worker(
            Path(__file__).resolve().parents[2] / "scripts/codex_von_worker.py"
        )
        resolved = worker.resolve_execution_settings({}, task)
        assert resolved["model"] == model and resolved["reasoning_effort"] == "ultra"


def test_creation_failed_parent_update_and_assignment_recovery_answer(
    native_store, gateway, monkeypatch
):
    from src.backend.languagemodels.structured_tool_calling.types import (
        LLMResponse,
        ToolCall,
    )
    from src.backend.services.adaptive_turn_service import execute_adaptive_turn
    from src.backend.services import turn_execution_record_service

    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_concepts_collection",
        lambda: native_store.concepts,
    )
    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        lambda **kw: {
            "updated": True,
            "duplicate": False,
            "phase": kw.get("phase"),
            "stored_phase": kw.get("observation", {}),
        },
    )
    fields = {
        "assignee_concept_id": "#V#worker",
        "report_to_concept_id": "#V#alice",
        "requested_model": "gpt-6-astra",
        "requested_reasoning_effort": "high",
    }

    class IncidentReplay:
        call = 0

        def generate_with_tools(self, prompt, available_tools, **kwargs):
            self.call += 1
            if self.call == 1:
                name = "task_create"
                arguments = {
                    "title": "Isolated recovered assignment",
                    "description": "Fixture, no real work.",
                    "assignee_concept_id": "#V#alice",
                }
            else:
                task_id = native_store.concepts.find_one({})["concept_id"]
                if self.call == 4:
                    return LLMResponse(
                        text_response=f"Created {task_id} and assigned it to #V#worker with Astra/high. The requested parent relationship is unresolved. Worker pickup is unverified."
                    )
                assert self.call < 4, "unexpected replay model call"
                name = "task_update_fields"
                arguments = {
                    "task_concept_id": task_id,
                    "fields": {
                        **fields,
                        **(
                            {"parent_task_concept_id": "#V#required_parent"}
                            if self.call == 2
                            else {}
                        ),
                    },
                }
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id=f"fixture-{self.call}",
                        payload={"name": name, "arguments": arguments},
                    )
                ],
            )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create a coding task assigned to our coding agent with Astra/high, reporting to me, and link it as a child of the required parent.",
        context=[],
        llm_client=IncidentReplay(),
        model="fixture-no-model-request",
        user_namespace="#V#alice@lab",
        user_concept_id="#V#alice",
        org_concept_id="#V#lab",
        conversation_id="fixture-source",
        turn_id="fixture-incident-recovery",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )
    assert result.terminal_status == "effect_partially_completed", result.response_text
    task_id = native_store.concepts.find_one({})["concept_id"]
    with override_current_actor("#V#alice", "#V#lab"):
        task = tasks.get_task(task_id)
    assert task["assignee_concept_id"] == "#V#worker"
    assert task["requested_reasoning_effort"] == "high"
    assert task["parent_task_concept_id"] is None
    authoritative = result.response_text.split("### Model draft")[0]
    assert f"[{task_id}]" in authoritative
    assert "#V#worker" in authoritative
    assert "gpt-6-astra" in authoritative
    assert "later canonical read-back confirmed fields" in authoritative
    assert "still unresolved: parent_task_concept_id" in authoritative
    assert "pickup" in authoritative
    assert native_store.concepts.count_documents({}) == 1


@pytest.mark.parametrize("members", [{"#V#alice"}, {"#V#worker"}])
def test_create_requires_both_live_memberships(
    native_store, gateway, monkeypatch, members
):
    monkeypatch.setattr(
        membership,
        "is_user_member_of_organisation",
        lambda user, org: user in members and org == "#V#lab",
    )
    with override_current_actor("#V#alice", "#V#lab"):
        result = gateway.invoke("task_create", payload(gateway)).payload
    assert result["error_code"] == "task_assignment_scope_denied"
    assert native_store.concepts.count_documents({}) == 0


def test_retry_keeps_progressed_task_and_detects_changed_explicit_intent(
    native_store, gateway
):
    with override_current_actor("#V#alice", "#V#lab"):
        request = payload(gateway, idempotency_key="fixture")
        first = gateway.invoke("task_create", request).payload
        assert first.get("success"), first
        task_id = first["task_concept_id"]
        tasks.update_task_fields(task_id, fields={"status": "completed"})
        replay = gateway.invoke("task_create", request).payload
        assert replay["success"] and replay["status"] == "completed"
        conflicting = gateway.invoke(
            "task_create", {**request, "requested_reasoning_effort": "medium"}
        ).payload
        assert conflicting["success"] is False and conflicting["changed"] is False
        assert tasks.get_task(task_id)["status"] == "completed"
    assert native_store.concepts.count_documents({}) == 1


def test_recovery_requires_same_task_and_matching_canonical_fields():
    from src.backend.services.adaptive_turn_service import (
        _reconcile_effect_attempts_by_postcondition,
    )

    failed = {
        "effect_id": "failed",
        "tool": "task_update_fields",
        "effective_arguments": {
            "task_concept_id": "#V#task",
            "fields": {"assignee_concept_id": "#V#worker"},
        },
    }
    succeeded = {
        "effect_id": "success",
        "tool": "task_update_fields",
        "effective_arguments": {
            "task_concept_id": "#V#task",
            "fields": {"assignee_concept_id": "#V#worker"},
        },
    }
    state = {
        "failed": {"effect_status": "failed", "changed": False},
        "success": {
            "effect_status": "succeeded",
            "canonical_readback": {
                "verified": True,
                "task_concept_id": "#V#task",
                "task_fields": {"assignee_concept_id": "#V#worker"},
            },
        },
    }
    reconciled = _reconcile_effect_attempts_by_postcondition(
        tool_invocations=[failed, succeeded], effect_snapshot=state
    )
    assert reconciled["failed"]["recovery_status"] == "succeeded"
    for changes in (
        {"task_concept_id": "#V#other"},
        {"verified": False},
        {"task_fields": {"assignee_concept_id": "#V#wrong"}},
    ):
        changed = deepcopy(state)
        changed["success"]["canonical_readback"].update(changes)
        reconciled = _reconcile_effect_attempts_by_postcondition(
            tool_invocations=[failed, succeeded], effect_snapshot=changed
        )
        assert "recovery_status" not in reconciled["failed"]
    # A subsequent contradictory update invalidates the earlier matching value.
    changed = deepcopy(state)
    changed["reassignment"] = {
        "effect_status": "succeeded",
        "canonical_readback": {
            "verified": True,
            "task_concept_id": "#V#task",
            "task_fields": {"assignee_concept_id": "#V#different"},
        },
    }
    reconciled = _reconcile_effect_attempts_by_postcondition(
        tool_invocations=[
            failed,
            succeeded,
            {**succeeded, "effect_id": "reassignment"},
        ],
        effect_snapshot=changed,
    )
    assert "recovery_status" not in reconciled["failed"]
