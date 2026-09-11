"""Bounded authority and interrupted-run acceptance for the external worker."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "codex_von_worker",
    Path(__file__).resolve().parents[2] / "scripts/codex_von_worker.py",
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.fixture
def config(tmp_path):
    return {
        "agent_id": "#V#worker",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
        "state_root": str(tmp_path),
    }


def task(config, **changes):
    return {
        "task_concept_id": "#V#task",
        "title": "A coding task",
        "status": "pending",
        "assignee_concept_id": config["agent_id"],
        "created_by_concept_id": config["delegator_id"],
        "organisation_concept_id": config["organisation_id"],
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"assignee_concept_id": "#V#other"},
        {"created_by_concept_id": "#V#other"},
        {"organisation_concept_id": "#V#other"},
        {"report_to_concept_id": "#V#other"},
    ],
)
def test_wrong_task_authority_never_launches(config, changes, monkeypatch):
    monkeypatch.setattr(worker, "launch", lambda *a: pytest.fail("unauthorised launch"))
    api = SimpleNamespace(pending=lambda: [task(config, **changes)])
    worker.tick(config, api, 0)


def test_empty_poll_has_no_model_or_message(config, monkeypatch):
    monkeypatch.setattr(
        worker, "launch", lambda *a: pytest.fail("empty poll launched Codex")
    )
    worker.tick(config, SimpleNamespace(pending=list), 0)


@pytest.mark.parametrize(
    "exit_code,output",
    [
        (
            1,
            {
                "status": "completed",
                "summary": "Done",
                "evidence": "check",
                "question": "",
            },
        ),
        (0, {"status": "completed", "summary": "Done", "evidence": "", "question": ""}),
        (0, "malformed"),
    ],
)
def test_process_or_output_failure_cannot_complete(tmp_path, exit_code, output):
    worker.write_json(tmp_path / "exit.json", {"returncode": exit_code})
    worker.write_json(tmp_path / "result.json", output)
    state = {"run_dir": str(tmp_path)}
    worker.recover_result(state)
    assert state["phase"] == "reporting"
    assert state["result"]["status"] == "blocked"


def test_interruption_without_exit_receipt_blocks_and_preserves_work(tmp_path):
    (tmp_path / "patch.py").write_text("print('retained')")
    state = {"run_dir": str(tmp_path)}
    worker.recover_result(state)
    assert state["result"]["status"] == "blocked"
    assert (tmp_path / "patch.py").exists()


def test_waiting_for_unchanged_input_does_not_spend_model_calls(config, monkeypatch):
    current = task(config, status="blocked")
    inputs = {"description": "work"}
    path = Path(config["state_root"]) / "tasks"
    path.mkdir()
    worker.write_json(
        path / (worker.fingerprint("#V#task")[:16] + ".json"),
        {
            "task_id": "#V#task",
            "phase": "waiting",
            "input_hash": worker.fingerprint(inputs),
        },
    )
    api = SimpleNamespace(pending=lambda: [current], inputs=lambda t: inputs)
    monkeypatch.setattr(
        worker, "launch", lambda *a: pytest.fail("unchanged input launched")
    )
    worker.tick(config, api, 0)


def test_report_retry_has_same_message_intent_after_task_completed(config):
    api = object.__new__(worker.Von)
    api.config = config
    current = task(config, status="in_progress")
    api.task = lambda _: current
    sent = []
    api.send = lambda t, key, content: sent.append((key, content)) or "#V#message"
    api.tasks = SimpleNamespace(
        update_task_fields=lambda *a, **kw: None,
        update_task_status=lambda tid, status, **kw: current.update(status=status),
    )
    state = {
        "task_id": "#V#task",
        "attempt": "run",
        "phase": "reporting",
        "result": {
            "status": "completed",
            "summary": "Done",
            "evidence": "Validated",
            "question": "",
        },
    }
    api.finish(state)
    # Simulate a crash after the external effect and before saving local state.
    api.finish({**state, "phase": "reporting"})
    assert sent[0] == sent[1]
    assert current["status"] == "completed"


def test_reassigned_task_receives_no_status_or_field_mutation(config):
    api = object.__new__(worker.Von)
    api.config = config
    api.task = lambda _: task(config, assignee_concept_id="#V#other")
    api.send = lambda *a: "#V#message"
    api.tasks = SimpleNamespace(
        update_task_fields=lambda *a, **kw: pytest.fail("mutated reassigned task"),
        update_task_status=lambda *a, **kw: pytest.fail("mutated reassigned task"),
    )
    state = {
        "task_id": "#V#task",
        "attempt": "run",
        "result": {
            "status": "completed",
            "summary": "Done",
            "evidence": "check",
            "question": "",
        },
    }
    api.finish(state)
    assert state["phase"] == "done"


@pytest.mark.parametrize("same_org", [True, False])
def test_private_conversation_locator_uses_invite_and_accessible_task_join(
    config, monkeypatch, same_org
):
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import shared_conversation_service as shares

    api = object.__new__(worker.Von)
    api.config = config
    accepted = []
    monkeypatch.setattr(
        shares,
        "list_invites_for_user",
        lambda **kw: [
            {
                "invite_id": "invite",
                "session_id": "session",
                "status": "pending",
                "inviter_user_id": config["delegator_id"],
                "organisation_concept_id": (
                    config["organisation_id"] if same_org else "#V#other"
                ),
            }
        ],
    )
    monkeypatch.setattr(shares, "respond_to_invite", lambda **kw: accepted.append(kw))

    def linked_task(query, projection):
        assert query["concept_id"] == "#V#task"
        assert "relationships.#V#hasOriginatingConversation" in query
        assert projection == {"concept_id": 1}
        return {"concept_id": "#V#task"}

    monkeypatch.setattr(ConceptsRepository, "find_one", linked_task)
    monkeypatch.setattr(
        catalogue,
        "_conversation_transcript_page",
        lambda **kw: {
            "success": True,
            "messages": [{"role": "user", "content": "prefix SAIL"}],
            "next_cursor": None,
        },
    )
    result = api.conversation(task(config))
    assert result["available"] is same_org
    assert bool(accepted) is same_org


def test_git_preparation_failure_is_reported_without_losing_checkpoint(
    config, monkeypatch
):
    config.update(
        source_repo="/missing", codex_command="/missing", model="gpt-5.6-terra"
    )
    monkeypatch.setattr(
        worker,
        "command",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable")),
    )
    reports = []

    def finish(state):
        reports.append(state["result"])
        state.update(phase="waiting", message_id="#V#message")

    api = SimpleNamespace(
        pending=lambda: [task(config)],
        inputs=lambda t: {"title": "test"},
        conversation=lambda t: {"available": False},
        send=lambda *a: "#V#started",
        tasks=SimpleNamespace(update_task_status=lambda *a, **kw: None),
        finish=finish,
    )
    worker.tick(config, api, 0)
    assert reports[0]["status"] == "blocked"
    state_path = next((Path(config["state_root"]) / "tasks").glob("*.json"))
    assert json.loads(state_path.read_text())["phase"] == "waiting"
