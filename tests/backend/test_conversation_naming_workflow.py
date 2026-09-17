"""Naming consumer execution with fictional evidence and scripted title choice.

These tests prove wiring and effect boundaries, not live model title quality.
"""

from dataclasses import replace

import pytest

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services.conversation_naming_workflow_vontology_service import (
    CONVERSATION_NAMING_WORKFLOW_ID,
    bootstrap_canonical_conversation_naming_workflow,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_mcp_tool_actions import (
    register_workflow_mcp_tool_actions,
)
from test_lab_status_digest_workflow_vontology_service import (
    _reset_mock_db,
)  # noqa: F401

ACTOR = "#V#naming_fixture_owner"
NAMESPACE = "#V#naming_fixture_owner@org"


@pytest.fixture
def world(monkeypatch, _reset_mock_db):
    from src.backend.services import chat_history_service as history
    from src.backend.services import conversation_management_service as management
    from src.backend.services import conversation_search_service as search

    report = bootstrap_canonical_conversation_naming_workflow()
    assert report["success"], report
    state = {
        "names": {"one": None, "two": None, "named": "User chosen title"},
        "calls": [],
        "renames": [],
        "proposals": [],
        "race": False,
    }

    def summary(user_id, session_id, **kwargs):
        if user_id != ACTOR or session_id not in state["names"]:
            return None
        return {
            "session_id": session_id,
            "session_name": state["names"][session_id],
            "namespace": NAMESPACE,
            "user_id": ACTOR,
        }

    def evidence(*, user_id, session_id, **kwargs):
        assert user_id == ACTOR
        return {
            "session_id": session_id,
            "session_name": state["names"][session_id],
            "updated_at": "2026-09-12T12:00:00+00:00",
            "message_count": 2,
            "user_message_count": 1,
            "evidence_sufficient": True,
            "first_user_message": {
                "content": "Plan a research seminar.",
                "content_sha256": "seminar",
            },
            "latest_user_message": {
                "content": "Plan a research seminar.",
                "content_sha256": "seminar",
            },
        }

    def rename(**kwargs):
        sid = kwargs["session_id"]
        assert kwargs["user_id"] == ACTOR
        assert kwargs["require_unnamed"] is True
        assert state["names"][sid] is None
        state["renames"].append(sid)
        state["names"][sid] = kwargs["session_name"]
        return {"matched": True, "updated": True}

    def enumerate_page(**kwargs):
        assert kwargs["actor_user_id"] == ACTOR
        assert kwargs["namespace"] == NAMESPACE
        assert kwargs["filters"] == {"name_present": False, "access_mode": "owner"}
        cursor = kwargs.get("cursor")
        state["calls"].append(("search_cursor", cursor))
        ids = (
            []
            if cursor is None
            else ["one", "named"] if cursor == "page-2" else ["two"]
        )
        if state.get("missing") and cursor == "page-2":
            ids.append("missing")
        return {
            "success": True,
            "schema_version": "conversation_search_result.v1",
            "query": "*",
            "match_mode": "lexical",
            "page_size": 5,
            "has_more": cursor != "page-3",
            "index_coverage": {},
            "retrieval": {},
            "candidate_session_ids": ids,
            "results": [],
            "count": len(ids),
            "next_cursor": (
                "page-2" if cursor is None else "page-3" if cursor == "page-2" else None
            ),
            "coverage_complete": cursor == "page-3",
        }

    monkeypatch.setattr(search, "search_actor_conversations", enumerate_page)
    monkeypatch.setattr(history, "get_chat_history_session_summary", summary)
    monkeypatch.setattr(
        history,
        "has_chat_history_session",
        lambda user_id, session_id, **kwargs: user_id == ACTOR
        and session_id in state["names"],
    )
    monkeypatch.setattr(history, "get_chat_history_title_evidence", evidence)
    monkeypatch.setattr(history, "rename_chat_session", rename)
    monkeypatch.setattr(
        management,
        "apply_conversation_preferences",
        lambda *, actor_user_id, conversations: list(conversations),
    )

    catalogue = MethodCatalogue()
    defaults = build_default_catalogue()
    for name in [
        "conversation_search",
        "conversation_inspect_batch",
        "conversation_manage_batch",
    ]:
        method = defaults.get(name)

        def handler(_name=name, _handler=method.handler, **kwargs):
            state["calls"].append((_name, kwargs))
            result = _handler(**kwargs)
            if _name == "conversation_inspect_batch":
                state["last_inspection"] = result
            return result

        catalogue.register(replace(method, handler=handler))
    gateway = InternalMCPGateway(
        catalogue=catalogue, transport=InternalMCPTransport(), enabled=True
    )
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)
    register_control_flow_actions(registry)

    def choose(request):
        assert request.data["requested_model"] == "gpt-5.4"
        inspection = request.data["inspection"]
        assert inspection.get("results"), inspection
        items = []
        for row in inspection.get("results", []):
            ev = row.get("evidence", {})
            if ev.get("eligible_for_rename"):
                items.append(
                    {
                        "session_id": ev["session_id"],
                        "session_name": "Research Seminar",
                        "evidence_token": ev["evidence_token"],
                    }
                )
        state["proposals"].extend(items)
        if state["race"] and "one" in [i["session_id"] for i in items]:
            state["names"]["one"] = "Concurrent user title"
        return WorkflowActionResult(
            status="success",
            outputs={"validated_json": {"rename_items": items, "skipped": []}},
        )

    from src.backend.workflows.llm_step_executor import execute_llm_step

    state["real_llm_step"] = execute_llm_step
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step", choose
    )
    definition = load_workflow_definition_from_vontology(
        CONVERSATION_NAMING_WORKFLOW_ID
    )
    assert definition is not None

    def run(**data):
        with override_current_actor(ACTOR, "#V#org"):
            return WorkflowExecutor(registry=registry, max_transitions=150).run(
                definition,
                environment=WorkflowEnvironment(
                    llm_client=None,
                    gateway=gateway,
                    user_concept_id=ACTOR,
                    org_concept_id="#V#org",
                    user_namespace=NAMESPACE,
                ),
                data={
                    "schedule_occurrence_id": "fixture-occurrence-1",
                    "requested_model": "gpt-5.4",
                    **data,
                },
            )

    state["run"] = run
    state["gateway"] = gateway
    state["definition"] = definition
    state["registry"] = registry
    state["choose"] = choose
    return state


def test_empty_page_continues_and_names_are_canonically_read_back(world):
    result = world["run"]()
    assert result.completed, __import__("json").dumps(
        {
            k: v
            for k, v in result.data.items()
            if k
            in [
                "last_failed_action_outputs",
                "last_metadata_validation",
                "workflow_result_envelope",
            ]
        },
        default=str,
        indent=2,
    )
    assert result.data["coverage_complete"] is True
    assert world["names"] == {
        "one": "Research Seminar",
        "two": "Research Seminar",
        "named": "User chosen title",
    }
    assert [v for k, v in world["calls"] if k == "search_cursor"] == [
        None,
        "page-2",
        "page-3",
    ]
    receipt = result.data["rename_result"]["results"][0]
    assert receipt["canonical_read_back"]["session_name"] == "Research Seminar"
    assert result.data["rename_result"]["request_id"] == "fixture-occurrence-1"
    again = world["run"]()
    assert again.completed, (again.error, again.data)
    assert world["renames"] == ["one", "two"]


def test_concurrent_title_is_preserved_and_later_page_still_progresses(world):
    world["race"] = True
    result = world["run"]()
    assert not result.completed
    assert result.data["had_partial_failure"] is True
    assert result.data["coverage_complete"] is True
    assert world["names"]["one"] == "Concurrent user title"
    assert world["names"]["two"] == "Research Seminar"
    assert world["names"]["named"] == "User chosen title"


def test_bootstrap_preserves_published_authority(world):
    report = bootstrap_canonical_conversation_naming_workflow()
    assert report["success"], report
    assert report["prompt_support"]["seeded_prompt_count"] == 0
    assert report["publication"]["counts"]["workflows_published"] == 0


def test_hourly_schedule_verifies_and_due_occurrence_executes(monkeypatch, world):
    from datetime import datetime, timedelta, timezone
    from src.backend.services import concept_service
    from src.backend.services.workflow_actor_scope_service import WorkflowActorScope
    from src.backend.services import workflow_schedule_service as schedules
    from src.backend.workflows.workflow_registry import (
        WorkflowRegistry,
        WorkflowRegistration,
    )
    from src.backend.workflows.durable import registry_factory as factory
    from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
    from src.backend.workflows.durable.scheduler import WorkflowScheduler
    from src.backend.workflows.durable.durable_executor import DurableWorkflowExecutor
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        verify_workflow_runnable,
    )
    from src.backend.languagemodels import llm_interface

    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=CONVERSATION_NAMING_WORKFLOW_ID,
            definition=world["definition"],
            source="vontology",
        )
    )
    monkeypatch.setattr(
        factory, "get_shared_workflow_registry_read_only", lambda **kwargs: registry
    )
    monkeypatch.setattr(
        factory, "get_shared_durable_action_registry", lambda: world["registry"]
    )
    monkeypatch.setattr(
        factory, "_get_or_build_durable_mcp_gateway", lambda: world["gateway"]
    )

    class ScriptedClient:
        def generate(self, prompt, **kwargs):
            assert kwargs["model"] == "gpt-5.4"
            assert kwargs["llm_params"]["reasoning_effort"] == "medium"
            assert "untrusted source material" in prompt
            assert "Plan a research seminar." in prompt
            items = []
            for row in world["last_inspection"]["results"]:
                ev = row.get("evidence", {})
                if ev.get("eligible_for_rename"):
                    assert ev["evidence_token"] in prompt
                    items.append(
                        {
                            "session_id": ev["session_id"],
                            "session_name": "Research Seminar",
                            "evidence_token": ev["evidence_token"],
                        }
                    )
            return __import__("json").dumps({"rename_items": items, "skipped": []})

    monkeypatch.setattr(
        llm_interface, "get_llm_client", lambda **kwargs: ScriptedClient()
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        world["real_llm_step"],
    )
    with override_current_actor(ACTOR, "#V#org"):
        verified = verify_workflow_runnable(
            CONVERSATION_NAMING_WORKFLOW_ID, actor_user_id=ACTOR, actor_org_id="#V#org"
        )
        assert verified.runnable_verification_success, verified
        for cid in [
            ACTOR,
            "#V#org",
            "#V#triggers_workflow",
            "#V#workflow_schedule",
            "#V#interval_schedule",
        ]:
            concept_service.create_concept(
                concept_id=cid,
                name=cid[3:],
                parent_concept_ids=(
                    ["#V#workflow_schedule"] if cid == "#V#interval_schedule" else []
                ),
            )
        manager = WorkflowInstanceManager()
        actor = WorkflowActorScope(ACTOR, "#V#org", NAMESPACE, "fixture")
        command = {
            "workflow_id": CONVERSATION_NAMING_WORKFLOW_ID,
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "default_inputs": {
                "requested_model": "gpt-5.4",
                "requested_model_parameters": {"reasoning_effort": "medium"},
            },
            "idempotency_key": "hourly-owned-unnamed-conversation-naming-v1",
        }
        first = schedules.create_actor_schedule(
            manager, command, actor, request_id="fixture-turn"
        )
        retry = schedules.create_actor_schedule(
            manager, command, actor, request_id="fixture-turn"
        )
        assert first["schedule_id"] == retry["schedule_id"]
        assert retry["idempotent_replay"] is True
        assert first["user_id"] == ACTOR and first["namespace"] == NAMESPACE
        schedule = manager.get_schedule(first["schedule_id"])
        assert schedule.enabled and schedule.interval_seconds == 3600
        assert schedule.next_run_at > datetime.now(timezone.utc) + timedelta(minutes=59)
        # Fast-forward only the fixture clock to a naturally due occurrence.
        scheduler = WorkflowScheduler(manager)
        due = schedule.next_run_at
        instance_id = scheduler._trigger_schedule(schedule, due)
        duplicate = scheduler._trigger_schedule(schedule, due)
        assert duplicate == instance_id
        instance = manager.get_instance(instance_id)
        assert instance.inputs["schedule_occurrence_id"].endswith(due.isoformat())
        claimed = manager.find_and_claim_instance("naming-fixture-worker")
        assert claimed.instance_id == instance_id
        result = DurableWorkflowExecutor(
            registry=world["registry"], instance_manager=manager, max_transitions=10
        ).run_durable(
            instance_id,
            world["definition"],
            worker_id="naming-fixture-worker",
            claim_token=claimed.claim_token,
            workflow_definition_identity=verified.definition_identity,
        )
        assert result.error == "transition_limit"
        checkpoint = manager.get_instance(instance_id, for_execution=True)
        assert checkpoint.current_state.endswith("_rename")
        assert checkpoint.workflow_data["rename_items"][0]["evidence_token"]
        result = DurableWorkflowExecutor(
            registry=world["registry"], instance_manager=manager, max_transitions=50
        ).run_durable(
            instance_id,
            world["definition"],
            worker_id="naming-fixture-worker",
            claim_token=claimed.claim_token,
            workflow_definition_identity=verified.definition_identity,
        )
        assert result.completed, (
            result.error,
            result.data.get("last_failed_action_outputs"),
        )
        assert result.data["coverage_complete"] is True
        receipt = result.data["rename_result"]["results"][0]
        assert receipt["canonical_read_back"]["session_name"] == "Research Seminar"
        assert world["names"]["named"] == "User chosen title"
        final = manager.get_schedule(first["schedule_id"])
        assert final.enabled and final.interval_seconds == 3600
        assert final.next_run_at == due + timedelta(hours=1)


def test_partial_inspection_preserves_ready_candidates_and_reports_non_success(world):
    world["missing"] = True
    result = world["run"]()
    assert not result.completed
    assert result.data["had_partial_failure"] is True
    assert world["renames"] == ["one", "two"]
    assert world["names"]["named"] == "User chosen title"


def test_cross_actor_cannot_reuse_naming_evidence(world):
    from src.backend.integrations.internal_mcp import catalogue

    assert world["run"]().completed
    before = list(world["renames"])
    with override_current_actor("#V#another_actor", "#V#org"):
        result = catalogue._conversation_manage_batch(
            action="rename",
            rename_items=world["proposals"],
            acting_user_concept_id="#V#another_actor",
            namespace="#V#another_actor@org",
        )
    assert result["success"] is False
    assert world["renames"] == before
