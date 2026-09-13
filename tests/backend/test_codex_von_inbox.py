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
        reply_id = "#V#reply" if not sent else f"#V#reply_{len(sent)}"
        sent.setdefault(
            kwargs["delivery_idempotency_key"],
            {**kwargs, "concept_id": reply_id, "message_id": reply_id},
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
                else next(
                    (row for row in sent.values() if row["message_id"] == mid), None
                )
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
    assert transitions == ["pending"]


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
        assert 'model_reasoning_effort="medium"' in args
        assert not set(kwargs["env"]) & {"OPENAI_API_KEY", "MONGO_URI"}
        assert state["context"]["execution_settings"]["model"] == "gpt-5.6-terra"
        assert state["context"]["capabilities"]["read_only_host_inspection"] is True
        assert "Use available read-only shell tools" in kwargs["input"]
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


def test_captured_hardware_result_retains_answer_and_denies_deployment(tmp_path):
    import json
    from pathlib import Path

    captured = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/codex/inbox_hardware_20260912.json"
        ).read_text()
    )
    inbox.write_json(tmp_path / "exit.json", captured["exit"])
    inbox.write_json(tmp_path / "result.json", captured["result"])
    state = {
        "run_dir": str(tmp_path),
        "context": {"message": captured["message"], "tasks": []},
    }
    inbox.recover(state)
    assert captured["result"]["answer"] in state["result"]["answer"]
    assert state["result"]["action"] == "reply"
    assert state["result"]["deployment_requested"] is False
    assert state["failures"] == [
        {
            "stage": "result_validation",
            "error_type": "ValueError",
            "detail": "A reply cannot deploy",
        }
    ]
    assert "No coding or deployment action was applied" in state["result"]["answer"]
    assert json.loads((tmp_path / "result.json").read_text()) == captured["result"]


def test_interrupted_reply_retries_once_from_durable_request(fixture, monkeypatch):
    config, api, message, _, _, sent, _, _ = fixture
    context = inbox.context_for(config, api, message)
    run = inbox.state_path(config, message["message_id"]).with_suffix("")
    state = {"phase": "running", "run_dir": str(run), "context": context}
    path = inbox.state_path(config, message["message_id"])
    inbox.write_json(path, state)
    calls = []

    def launch(config, state, fd):
        calls.append(state["context"]["message"])
        # A second interrupted run terminates visibly without a retry loop.
        inbox.recover(state)

    monkeypatch.setattr(inbox, "launch", launch)
    assert inbox.tick(config, api, 0)
    assert calls == [message]
    assert len(sent) == 1
    assert "do not need to resend" in next(iter(sent.values()))["content"]
    assert not inbox.tick(config, api, 0)


def test_failed_process_preserves_partial_text_without_effects(fixture, tmp_path):
    config, api, message, *_ = fixture
    inbox.write_json(tmp_path / "exit.json", {"returncode": 9})
    inbox.write_json(
        tmp_path / "result.json",
        {
            "answer": "Observed 20 CPU cores.",
            "task_id": "#V#task",
            "action": "resume_task",
            "deployment_requested": True,
        },
    )
    state = {
        "run_dir": str(tmp_path),
        "context": inbox.context_for(config, api, message),
    }
    inbox.recover(state)
    assert state["failures"][0]["returncode"] == 9
    assert "Observed 20 CPU cores." in state["result"]["answer"]
    assert state["result"]["action"] == "reply" and not state["result"]["task_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"sender_id": "#V#third_party"},
        {"organisation_concept_id": "#V#different_org"},
        {"recipient_ids": ["#V#different_agent"]},
        {"content": "Changed intent"},
    ],
)
def test_source_authority_is_rechecked_before_new_assignment(fixture, change):
    config, api, message, *_ = fixture
    state = {
        "context": copy.deepcopy(inbox.context_for(config, api, message)),
        "result": new_assignment_result(),
    }
    api.messages.get_message_for_user = lambda *a: {**message, **change}
    with pytest.raises(PermissionError):
        inbox.finish(
            config, api, state, inbox.state_path(config, message["message_id"])
        )


def new_assignment_result():
    return {
        "answer": "I will implement the requested change.",
        "task_id": "",
        "action": "create_task",
        "deployment_requested": False,
        "new_task": {
            "title": "New authorised work",
            "requested_model": "gpt-6-astra",
            "requested_reasoning_effort": "xhigh",
        },
    }


def test_assignment_crash_reuses_canonical_task_without_reopening_old_task(
    fixture, monkeypatch
):
    config, api, message, old_task, comments, sent, transitions, _ = fixture
    original_task = api.task
    created = []

    def create_task(**kw):
        created.append({**kw, "task_concept_id": "#V#new", "status": "pending"})
        raise OSError("simulated crash after canonical creation")

    api.tasks.create_task = create_task
    api.tasks.find_task_by_agent_creation_fingerprint = lambda **kw: (
        created[0] if created else None
    )
    api.task = lambda tid: (
        copy.deepcopy(created[0]) if tid == "#V#new" else original_task(tid)
    )
    state = {
        "phase": "reporting",
        "context": inbox.context_for(config, api, message),
        "result": new_assignment_result(),
    }
    path = inbox.state_path(config, message["message_id"])
    inbox.finish_or_retain(config, api, state, path)
    assert (
        state["phase"] == "reporting"
        and state["effect_error"]["stage"] == "assignment_create"
    )
    assert len(created) == 1 and not transitions
    assert state["failure_reply_id"] == "#V#reply"
    # Retry from the checkpoint with an already-created assignment.
    assert inbox.tick(config, api, 0)
    assert len(created) == 1
    assert old_task["status"] == "completed" and not transitions
    assert created[0]["description"] == message["content"]
    assert created[0]["created_by_concept_id"] == config["delegator_id"]
    assert created[0]["agent_creation_request_id"] == message["message_id"]
    assert len(comments) == 1
    assert inbox.task_followup(config, "#V#new")["deployment_requested"] is False
    assert not inbox.tick(config, api, 0)


def test_new_task_schema_cannot_select_actor_or_forbidden_model(fixture):
    config, api, message, *_ = fixture
    context = inbox.context_for(config, api, message)
    for changes in (
        {"created_by_concept_id": "#V#spoof"},
        {"requested_model": "gpt-5.6-sol"},
    ):
        result = new_assignment_result()
        result["new_task"].update(changes)
        with pytest.raises(ValueError):
            inbox.validate_result(result, context)


def test_recent_history_selects_last_30_in_scope_before_limiting(fixture, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import mongomock
    from src.backend.services import message_service as messages

    config, api, message, *_ = fixture
    collection = mongomock.MongoClient().inbox_history.concepts
    start = datetime(2026, 9, 11, 15, tzinfo=timezone.utc)
    for n in range(100):
        collection.insert_one(
            {
                "concept_id": f"#V#m{n:03}",
                "created_at": start + timedelta(minutes=n),
                "relationships": {
                    "is_an_instance_of": [messages.MESSAGE_TYPE_CONCEPT_ID],
                    messages.PREDICATE_SENDER: [config["delegator_id"]],
                    messages.PREDICATE_RECIPIENT: [config["agent_id"]],
                },
                "concept_data": {
                    "organisation_concept_id": (
                        config["organisation_id"] if n % 2 == 0 else "#V#other"
                    )
                },
            }
        )
    monkeypatch.setattr(messages, "get_concepts_collection", lambda: collection)
    monkeypatch.setattr(messages, "apply_concept_query_filter", lambda query: query)
    api.messages.get_conversation_between_users = (
        messages.get_conversation_between_users
    )
    api.messages.project_direct_message = lambda row: {
        **message,
        "message_id": row["concept_id"],
        "sent_at": row["created_at"].replace(tzinfo=timezone.utc).isoformat(),
        "organisation_concept_id": row["concept_data"]["organisation_concept_id"],
    }
    message["sent_at"] = (start + timedelta(minutes=80)).isoformat()
    context = inbox.context_for(config, api, message)
    assert [row["message_id"] for row in context["recent_messages"]] == [
        f"#V#m{n:03}" for n in range(22, 81, 2)
    ]
    assert context["history_context"]["older_messages_omitted"] is True
    # Unrelated UI callers retain their original ascending pagination semantics.
    assert [
        row["concept_id"]
        for row in messages.get_conversation_between_users(
            config["agent_id"], config["delegator_id"], limit=3
        )
    ] == ["#V#m000", "#V#m001", "#V#m002"]


@pytest.mark.parametrize("receipt", [None, [], {"returncode": "invalid"}])
def test_unverified_process_receipt_retains_usable_partial_answer(tmp_path, receipt):
    if receipt is not None:
        inbox.write_json(tmp_path / "exit.json", receipt)
    inbox.write_json(tmp_path / "result.json", {"answer": "Useful observed fact."})
    state = {"run_dir": str(tmp_path), "context": {"tasks": []}}
    inbox.recover(state)
    assert state["phase"] == "reporting"
    assert "Useful observed fact." in state["result"]["answer"]
    assert state["result"]["action"] == "reply"
    assert not state["result"]["deployment_requested"]


def test_membership_revocation_denies_new_assignment(fixture):
    config, api, message, _, comments, sent, transitions, _ = fixture
    state = {
        "context": inbox.context_for(config, api, message),
        "result": new_assignment_result(),
    }
    api.messages.authorise_direct_message_participants = lambda **kw: (
        False,
        [config["agent_id"]],
    )
    with pytest.raises(PermissionError):
        inbox.finish(
            config, api, state, inbox.state_path(config, message["message_id"])
        )
    assert not comments and not sent and not transitions
