"""Inbox → canonical native services → receipts, in an isolated in-memory store.

The model response is scripted; this proves controller effects and recovery, not
live model interpretation or delivery to the production Von organisation.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_coding_task_creation_recovery import native_store  # noqa: F401
from scripts import codex_von_inbox as inbox
from scripts import codex_von_worker as worker
from src.backend.security.access_control import override_current_actor
from src.backend.services import message_service as messages
from src.backend.services import organisation_membership_service as membership


@pytest.mark.parametrize("action", ["reply", "create_task"])
def test_canonical_delivery_assignment_and_crash_recovery(
    native_store, tmp_path, monkeypatch, action
):
    config = {
        "agent_id": "#V#worker",
        "delegator_id": "#V#alice",
        "organisation_id": "#V#lab",
        "state_root": str(tmp_path),
        "inbox_enabled": True,
        "inbox_since": "2026-01-01T00:00:00Z",
        "codex_command": "fixture-only",
        "model": "gpt-6-astra",
        "model_reasoning_effort": "xhigh",
    }
    monkeypatch.setattr(
        messages, "get_concepts_collection", lambda: native_store.concepts
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.sanitize_concept_document",
        lambda doc, **kwargs: doc,
    )
    monkeypatch.setattr(
        messages, "maybe_launch_direct_message_workflow", lambda **_: None
    )
    monkeypatch.setattr(
        membership,
        "get_organisation_members",
        lambda org: {
            "members": (
                [{"user_concept_id": cid} for cid in ("#V#alice", "#V#worker")]
                if org == "#V#lab"
                else []
            )
        },
    )
    api = worker.Von(config)
    captured = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/codex/inbox_hardware_20260912.json"
        ).read_text()
    )
    with override_current_actor("#V#worker", "#V#lab"):
        old = api.tasks.create_task(
            title="Independent completed UI assignment",
            description="Fixture only",
            created_by_concept_id="#V#alice",
            assignee_concept_id="#V#worker",
            report_to_concept_id="#V#alice",
            organisation_concept_id="#V#lab",
        )
        api.tasks.update_task_status(
            old["task_concept_id"], "completed", actor_concept_id="#V#worker"
        )
        messages.create_message(
            sender_id="#V#worker",
            recipient_ids=["#V#alice"],
            org_id="#V#lab",
            content="The independent UI work is complete.",
            thread_id=old["task_concept_id"],
        )
        source = messages.create_message(
            sender_id="#V#alice",
            recipient_ids=["#V#worker"],
            org_id="#V#lab",
            content=(
                captured["message"]["content"]
                if action == "reply"
                else "Implement the new export control and deploy it. Use gpt-6-astra with xhigh reasoning. Fixture request."
            ),
        )
        response = {
            "answer": (
                "Read-only inspection found 20 CPU cores; GPU device access is unavailable."
                if action == "reply"
                else "I will implement the new export control."
            ),
            "task_id": "",
            "action": action,
            "deployment_requested": action == "create_task",
            "new_task": (
                None
                if action == "reply"
                else {
                    "title": "New export control",
                    "requested_model": "gpt-6-astra",
                    "requested_reasoning_effort": "xhigh",
                }
            ),
        }
        calls = []

        def scripted_process(args, **kwargs):
            calls.append(args)
            assert args[args.index("--sandbox") + 1] == "read-only"
            assert args[args.index("--model") + 1] == "gpt-6-astra"
            assert 'model_reasoning_effort="xhigh"' in args
            assert source["concept_data"]["content_fallback"] in kwargs["input"]
            inbox.write_json(
                Path(args[args.index("--output-last-message") + 1]), response
            )
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(inbox.subprocess, "run", scripted_process)
        original = inbox.write_json

        def crash_after_delivery(path, value):
            if value.get("phase") == "done":
                raise OSError("Fixture crash after canonical delivery")
            original(path, value)

        monkeypatch.setattr(inbox, "write_json", crash_after_delivery)
        with (tmp_path / "lock").open("w") as lock:
            assert inbox.tick(config, api, lock.fileno())
        monkeypatch.setattr(inbox, "write_json", original)
        assert inbox.tick(config, api, 0)
        assert not inbox.tick(config, api, 0)
        assert len(calls) == 1
        checkpoint = json.loads(
            inbox.state_path(config, source["concept_id"]).read_text()
        )
        assert checkpoint["phase"] == "done"
        delivered = messages.project_direct_message(
            messages.get_message_for_user(checkpoint["reply_id"], "#V#alice")
        )
        assert response["answer"] in delivered["content"]
        assert delivered["reply_to_id"] == source["concept_id"]
        assert api.task(old["task_concept_id"])["status"] == "completed"
        tasks = api.tasks.search_tasks(
            assignee_concept_id="#V#worker",
            created_by_concept_id="#V#alice",
            organisation_concept_id="#V#lab",
        )
        assert len(tasks["tasks"]) == (2 if action == "create_task" else 1)
        if action == "create_task":
            new = api.task(checkpoint["created_task_id"])
            assert new["description"] == source["concept_data"]["content_fallback"]
            assert new["status"] == "pending"
            assert new["requested_model"] == "gpt-6-astra"
            assert (
                worker.resolve_execution_settings(config, api.inputs(new))[
                    "reasoning_effort"
                ]
                == "xhigh"
            )
            assert (
                inbox.task_followup(config, new["task_concept_id"])[
                    "deployment_requested"
                ]
                is True
            )
            assert (
                len(api.tasks.list_task_comments(new["task_concept_id"])["comments"])
                == 1
            )
        else:
            assert not list((tmp_path / "followups").glob("*.json"))
