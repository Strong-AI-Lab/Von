"""Message replies retain task state, provenance and durable effect receipts."""

import copy
from types import SimpleNamespace

import pytest

from scripts import codex_von_inbox as inbox
from scripts import codex_von_worker as worker


@pytest.fixture
def fixture(tmp_path):
    config = {
        "agent_id": "#V#agent",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
        "state_root": str(tmp_path),
        "inbox_enabled": True,
        "inbox_since": "2026-09-11T15:00:00Z",
    }
    message = {
        "message_id": "#V#question",
        "sender_id": "#V#owner",
        "recipient_ids": ["#V#agent"],
        "organisation_concept_id": "#V#org",
        "sent_at": "2026-09-11T15:32:00Z",
        "content": "Was this finished?",
        "thread_id": None,
    }
    task = {
        "task_concept_id": "#V#task",
        "title": "Desktop tray",
        "assignee_concept_id": "#V#agent",
        "created_by_concept_id": "#V#owner",
        "organisation_concept_id": "#V#org",
        "status": "completed",
        "evidence": "Deployed PR #617",
    }
    rows = [message]
    comments, sent, transitions = [], {}, []

    def create(**kwargs):
        sent.setdefault(
            kwargs["delivery_idempotency_key"],
            {**kwargs, "concept_id": "#V#reply", "message_id": "#V#reply"},
        )
        return SimpleNamespace(message=sent[kwargs["delivery_idempotency_key"]])

    def status(task_id, value, **kwargs):
        transitions.append(value)
        task["status"] = value

    api = SimpleNamespace(
        task=lambda _: copy.deepcopy(task),
        native_writer=lambda _: True,
        tasks=SimpleNamespace(
            TaskNotFoundError=KeyError,
            list_task_comments=lambda *a, **k: {"comments": comments},
            add_task_comment=lambda *a, **k: comments.append(k),
            update_task_status=status,
        ),
        messages=SimpleNamespace(
            get_messages_for_user=lambda *a, **k: rows,
            get_conversation_between_users=lambda *a, **k: [
                message,
                {**message, "sender_id": "#V#agent", "thread_id": "#V#task"},
            ],
            project_direct_message=lambda row: row,
            get_message_for_user=lambda mid, _: (
                message
                if mid == message["message_id"]
                else next(iter(sent.values()), None)
            ),
            authorise_direct_message_participants=lambda **k: (True, []),
            create_message_idempotently=create,
        ),
    )
    return config, api, message, task, comments, sent, transitions, rows


@pytest.mark.parametrize(
    "action,deploy", [("reply", False), ("resume_task", False), ("resume_task", True)]
)
def test_reply_or_reopen_records_one_question_answer_and_one_message(
    fixture, monkeypatch, action, deploy
):
    config, api, message, task, comments, sent, transitions, _ = fixture
    launches = []

    def launch(config, state, fd):
        launches.append(state)
        state.update(
            phase="reporting",
            result={
                "answer": "The tray was deployed.",
                "task_id": "#V#task",
                "action": action,
                "deployment_requested": deploy,
            },
        )

    monkeypatch.setattr(inbox, "launch", launch)
    assert inbox.tick(config, api, 0)
    assert not inbox.tick(config, api, 0)
    assert len(launches) == len(comments) == len(sent) == 1
    assert (
        "Was this finished?" in comments[0]["body"]
        and "The tray was deployed." in comments[0]["body"]
    )
    assert comments[0]["source"]["message_id"] == message["message_id"]
    assert task["status"] == ("pending" if action == "resume_task" else "completed")
    assert transitions == (["pending"] if action == "resume_task" else [])
    if action == "resume_task":
        assert inbox.task_followup(config, "#V#task") == {
            "message_id": "#V#question",
            "content": message["content"],
            "deployment_requested": deploy,
        }
    else:
        assert inbox.task_followup(config, "#V#task") is None


def test_crash_after_note_and_delivery_reconciles_without_duplicate(
    fixture, monkeypatch
):
    config, api, message, _, comments, sent, _, _ = fixture
    state = {
        "phase": "reporting",
        "context": inbox.context_for(config, api, message),
        "result": {
            "answer": "Yes, deployed.",
            "task_id": "#V#task",
            "action": "reply",
            "deployment_requested": False,
        },
    }
    path = inbox.state_path(config, message["message_id"])
    original = inbox.write_json

    def crash(path, value):
        if value.get("phase") == "done":
            raise OSError("simulated loss after delivery")
        original(path, value)

    monkeypatch.setattr(inbox, "write_json", crash)
    with pytest.raises(OSError):
        inbox.finish(config, api, state, path)
    monkeypatch.setattr(inbox, "write_json", original)
    assert inbox.tick(config, api, 0)
    assert len(comments) == len(sent) == 1


def test_reassigned_task_gets_honest_reply_without_blocking_inbox(fixture):
    config, api, message, task, comments, sent, transitions, _ = fixture
    state = {
        "phase": "reporting",
        "context": inbox.context_for(config, api, message),
        "result": {
            "answer": "I will resume it.",
            "task_id": "#V#task",
            "action": "resume_task",
            "deployment_requested": False,
        },
    }
    task["assignee_concept_id"] = "#V#other"
    inbox.finish(config, api, state, inbox.state_path(config, message["message_id"]))
    assert state["phase"] == "done"
    assert "assignment changed" in next(iter(sent.values()))["content"]
    assert not comments and not transitions
    assert not inbox.tick(config, api, 0)


def test_two_queued_followups_preserve_both_coding_requests(fixture):
    config, api, message, task, comments, sent, transitions, _ = fixture

    def report():
        state = {
            "phase": "reporting",
            "context": inbox.context_for(config, api, message),
            "result": {
                "answer": "I will make this change.",
                "task_id": "#V#task",
                "action": "resume_task",
                "deployment_requested": False,
            },
        }
        inbox.finish(
            config, api, state, inbox.state_path(config, message["message_id"])
        )

    message["content"] = "Move the send controls beside the input."
    report()
    message["message_id"] = "#V#second_question"
    message["content"] = "Also use compact identity labels."
    api.messages.get_message_for_user = lambda mid, _: (
        message if mid == message["message_id"] else list(sent.values())[-1]
    )
    report()
    followup = inbox.task_followup(config, "#V#task")
    assert "Move the send controls" in followup["content"]
    assert "compact identity labels" in followup["content"]
    assert followup["message_ids"] == ["#V#question", "#V#second_question"]
    assert task["status"] == "pending" and len(comments) == 2


def test_empty_old_or_foreign_mail_does_not_invoke_model(fixture, monkeypatch):
    config, api, message, _, comments, sent, _, rows = fixture
    rows[:] = [
        {**message, "sender_id": "#V#other"},
        {**message, "organisation_concept_id": "#V#other"},
        {**message, "sent_at": "2026-09-10T01:00:00Z"},
    ]
    monkeypatch.setattr(
        inbox, "launch", lambda *a: pytest.fail("Unexpected model call")
    )
    assert not inbox.tick(config, api, 0)
    assert not comments and not sent


def test_unknown_task_and_reply_deployment_are_denied(fixture):
    config, api, message, *_ = fixture
    context = inbox.context_for(config, api, message)
    for result in [
        {
            "answer": "Done",
            "task_id": "#V#outsider",
            "action": "resume_task",
            "deployment_requested": False,
        },
        {
            "answer": "Done",
            "task_id": "#V#task",
            "action": "reply",
            "deployment_requested": True,
        },
    ]:
        with pytest.raises(ValueError):
            inbox.validate_result(result, context)


def test_resume_without_new_deployment_instruction_blocks_old_deploy_request(
    fixture, tmp_path
):
    config, api, _message, task, *_ = fixture
    task["status"] = "in_progress"
    inputs = {
        "description": "Implement and deploy the original task",
        "followup": {"content": "Adjust the icon", "deployment_requested": False},
    }
    api.inputs = lambda _: inputs
    config["deployment_command"] = "/should/not/run"
    run = tmp_path / "run"
    run.mkdir()
    state = {
        "task_id": "#V#task",
        "run_dir": str(run),
        "input_hash": worker.fingerprint(inputs),
        "result": {
            "status": "completed",
            "summary": "Changed icon",
            "deploy_commit": "a" * 40,
        },
    }
    worker.apply_deployment(config, api, state, tmp_path / "state.json", 0)
    assert state["deployment_final"] == "not_authorised"


def test_inbox_launch_is_read_only_and_inherits_the_single_worker_lock(
    fixture, monkeypatch
):
    config, api, message, *_ = fixture
    config.update(codex_command="codex", model="gpt-5.6-terra")
    state = {
        "run_dir": str(inbox.state_path(config, "#V#question").with_suffix("")),
        "context": inbox.context_for(config, api, message),
    }

    def run(args, **kwargs):
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert kwargs["pass_fds"] == (17,)
        assert not set(kwargs["env"]) & {"OPENAI_API_KEY", "MONGO_URI"}
        output = inbox.Path(args[args.index("--output-last-message") + 1])
        inbox.write_json(
            output,
            {
                "answer": "Yes, deployed.",
                "task_id": "#V#task",
                "action": "reply",
                "deployment_requested": False,
            },
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(inbox.subprocess, "run", run)
    inbox.launch(config, state, 17)
    assert state["phase"] == "reporting"
