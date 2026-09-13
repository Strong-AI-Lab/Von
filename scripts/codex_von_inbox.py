"""Durable direct-message replies for the single-host Codex worker."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

try:
    from .codex_von_worker import (
        authorised_task,
        capability_context,
        fingerprint,
        resolve_execution_settings,
        write_json,
    )
except ImportError:
    from codex_von_worker import (
        authorised_task,
        capability_context,
        fingerprint,
        resolve_execution_settings,
        write_json,
    )

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "task_id": {"type": "string"},
        "action": {"type": "string", "enum": ["reply", "resume_task", "create_task"]},
        "deployment_requested": {"type": "boolean"},
        "new_task": {
            "type": ["object", "null"],
            "properties": {
                "title": {"type": "string"},
                "requested_model": {"type": ["string", "null"]},
                "requested_reasoning_effort": {"type": ["string", "null"]},
            },
            "required": ["title", "requested_model", "requested_reasoning_effort"],
            "additionalProperties": False,
        },
    },
    "required": ["answer", "task_id", "action", "deployment_requested", "new_task"],
    "additionalProperties": False,
}


def timestamp(value):
    return datetime.fromisoformat(str(value))


def state_path(config, message_id):
    return Path(config["state_root"]) / "inbox" / (fingerprint(message_id) + ".json")


def followup_path(config, task_id):
    return Path(config["state_root"]) / "followups" / (fingerprint(task_id) + ".json")


def task_followup(config, task_id):
    path = followup_path(config, task_id)
    return json.loads(path.read_text()) if path.exists() else None


def already_answered(config, message_id):
    path = state_path(config, message_id)
    return path.exists() and json.loads(path.read_text()).get("phase") == "done"


def allowed_message(message, config):
    return (
        message.get("sender_id") == config["delegator_id"]
        and config["agent_id"] in message.get("recipient_ids", [])
        and message.get("organisation_concept_id") == config["organisation_id"]
    )


def new_messages(config, api):
    since = timestamp(config["inbox_since"])
    offset = 0
    found = []
    while True:
        rows = api.messages.get_messages_for_user(
            config["agent_id"], include_sent=False, limit=100, skip=offset
        )
        if not rows:
            break
        older = False
        for row in rows:
            message = api.messages.project_direct_message(row)
            if not message.get("sent_at") or timestamp(message["sent_at"]) < since:
                older = True
                continue
            if allowed_message(message, config) and not already_answered(
                config, message["message_id"]
            ):
                found.append(message)
        if older or len(rows) < 100:
            break
        offset += len(rows)
    return sorted(
        found,
        key=lambda message: (timestamp(message["sent_at"]), message["message_id"]),
    )


def context_for(config, api, message):
    rows = api.messages.get_conversation_between_users(
        config["agent_id"],
        config["delegator_id"],
        limit=31,
        organisation_concept_id=config["organisation_id"],
        as_of=timestamp(message["sent_at"]),
        newest_first=True,
    )
    conversation = [api.messages.project_direct_message(row) for row in rows]
    conversation = [
        row
        for row in conversation
        if row.get("organisation_concept_id") == config["organisation_id"]
        and row.get("sent_at")
        and timestamp(row["sent_at"]) <= timestamp(message["sent_at"])
    ]
    omitted = len(conversation) > 30
    conversation = sorted(
        conversation[:30],
        key=lambda row: (timestamp(row["sent_at"]), row["message_id"]),
    )
    task_ids = {row.get("thread_id") for row in conversation}
    task_ids.add(message.get("thread_id"))
    tasks = []
    for task_id in sorted(task_ids - {None, ""}):
        try:
            task = api.task(task_id)
        except api.tasks.TaskNotFoundError:
            continue
        if authorised_task(task, config) and api.native_writer(task):
            tasks.append(task)
    return {
        "message": message,
        "recent_messages": conversation,
        "tasks": tasks,
        "history_context": {
            "source": "canonical direct-message service",
            "as_of": message["sent_at"],
            "limit": 30,
            "older_messages_omitted": omitted,
            "oldest_supplied": conversation[0]["sent_at"] if conversation else None,
            "newest_supplied": conversation[-1]["sent_at"] if conversation else None,
            "task_selection": "Accessible native assignments referenced by these messages; proximity is not a request to resume them.",
        },
    }


def validate_result(value, context):
    fields = set(SCHEMA["required"])
    # Retained pre-upgrade runs have four fields. Keep them recoverable.
    if not isinstance(value, dict) or set(value) not in (fields, fields - {"new_task"}):
        raise ValueError("Missing inbox result fields")
    if not isinstance(value["answer"], str) or not value["answer"].strip():
        raise ValueError("Empty inbox answer")
    if value["action"] not in ("reply", "resume_task", "create_task") or not isinstance(
        value["deployment_requested"], bool
    ):
        raise ValueError("Invalid inbox action")
    ids = {task["task_concept_id"] for task in context["tasks"]}
    if not isinstance(value["task_id"], str) or value["task_id"] not in ids | {""}:
        raise ValueError("Task was not in authorised context")
    if value["action"] == "resume_task" and not value["task_id"]:
        raise ValueError("Resuming requires an identified task")
    if value["action"] == "reply" and value["deployment_requested"]:
        raise ValueError("A reply cannot deploy")
    new_task = value.get("new_task")
    if value["action"] == "create_task":
        if (
            value["task_id"]
            or not isinstance(new_task, dict)
            or set(new_task)
            != {"title", "requested_model", "requested_reasoning_effort"}
        ):
            raise ValueError("Creating requires new task details and an empty task_id")
        if not isinstance(new_task["title"], str) or not new_task["title"].strip():
            raise ValueError("New task title is empty")
        settings = resolve_execution_settings({}, new_task)
        value = {
            **value,
            "new_task": {
                **new_task,
                **{
                    "requested_" + key: settings[key]
                    for key in ("model", "reasoning_effort")
                    if new_task["requested_" + key] is not None
                },
            },
        }
    elif new_task is not None:
        raise ValueError("New task details require create_task")
    return value


def recover(state):
    run = Path(state["run_dir"])
    value = None
    stage = "process_receipt"
    try:
        receipt = json.loads((run / "exit.json").read_text())
        if not isinstance(receipt, dict):
            raise ValueError("Invalid inbox process receipt")
        stage = "result_json"
        value = json.loads((run / "result.json").read_text())
        stage = "process_exit"
        if type(receipt.get("returncode")) is not int or receipt["returncode"] != 0:
            raise ValueError("Inbox process did not exit successfully")
        stage = "result_validation"
        state["result"] = validate_result(value, state["context"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if value is None:
            try:
                value = json.loads((run / "result.json").read_text())
            except (OSError, ValueError):
                pass
        detail = (
            str(exc)
            if stage in {"result_validation", "process_exit"}
            else "Missing or unreadable " + stage
        )
        failure = {"stage": stage, "error_type": type(exc).__name__, "detail": detail}
        if stage == "process_exit":
            failure["returncode"] = receipt.get("returncode")
        state.setdefault("failures", []).append(failure)
        answer = value.get("answer") if isinstance(value, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            # One fresh attempt from the durable request, with the failure in
            # context. This is bounded recovery, not a user resend requirement.
            if not state.get("retry_count"):
                state["retry_count"] = 1
                state["context"]["recovery"] = failure
                state["run_dir"] = str(run / "retry-1")
                state["phase"] = "retrying"
                return
            answer = "I could not complete this reply. The original request and run evidence are retained for recovery; you do not need to resend it."
        else:
            answer = "Retained response (the action was not verified):\n\n" + answer
        state["result"] = {
            "answer": answer
            + f"\n\nReply recovery: {detail}. No coding or deployment action was applied from this result.",
            "task_id": "",
            "action": "reply",
            "deployment_requested": False,
        }
    state["phase"] = "reporting"


def launch(config, state, lock_fd):
    run = Path(state["run_dir"])
    run.mkdir(parents=True, exist_ok=True)
    settings = resolve_execution_settings(config, {})
    state["execution_settings"] = settings
    state["context"]["execution_settings"] = settings
    state["context"]["capabilities"] = capability_context(config, read_only=True)
    write_json(run / "schema.json", SCHEMA)
    write_json(run / "context.json", state["context"])
    prompt = Path(__file__).with_name("codex_von_inbox_prompt.md").read_text()
    prompt += "\nContext for this reply:\n" + json.dumps(state["context"], default=str)
    args = [
        config["codex_command"],
        "exec",
        "--strict-config",
        "--model",
        settings["model"],
        "-c",
        "model_reasoning_effort=" + json.dumps(settings["reasoning_effort"]),
        "--sandbox",
        "read-only",
        "--cd",
        str(run),
        "--skip-git-repo-check",
        "--json",
        "-c",
        "mcp_servers.von.enabled=false",
        "--output-schema",
        str(run / "schema.json"),
        "--output-last-message",
        str(run / "result.json"),
        "-",
    ]
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "LANG", "TERM")
        if key in os.environ
    }
    with (
        (run / "events.jsonl").open("w") as events,
        (run / "stderr.log").open("w") as errors,
    ):
        completed = subprocess.run(
            args,
            input=prompt,
            text=True,
            env=env,
            stdout=events,
            stderr=errors,
            pass_fds=(lock_fd,),
            check=False,
        )
    write_json(run / "exit.json", {"returncode": completed.returncode})
    recover(state)


def authorise_source(config, api, message):
    # Re-read the actual inbound message and both participants' authority.
    current = api.messages.get_message_for_user(
        message["message_id"], config["agent_id"]
    )
    if not current or not allowed_message(
        api.messages.project_direct_message(current), config
    ):
        raise PermissionError("Inbound message no longer available to this worker")
    if api.messages.project_direct_message(current)["content"] != message["content"]:
        raise PermissionError("Inbound message changed during reply")
    allowed, _ = api.messages.authorise_direct_message_participants(
        sender_id=config["agent_id"],
        recipient_ids=[config["delegator_id"]],
        organisation_concept_id=config["organisation_id"],
    )
    if not allowed:
        raise PermissionError("Message participants are no longer authorised")


def create_assignment(config, api, state, path):
    """One source-bound native assignment; no model-selected actor or project."""
    message = state["context"]["message"]
    details = state["result"]["new_task"]
    identity = fingerprint(
        [
            "codex-inbox-assignment-v1",
            config["agent_id"],
            config["delegator_id"],
            config["organisation_id"],
            message["message_id"],
        ]
    )
    expected = {
        "title": details["title"].strip(),
        "description": message["content"],
        "assignee_concept_id": config["agent_id"],
        "created_by_concept_id": config["delegator_id"],
        "organisation_concept_id": config["organisation_id"],
        "report_to_concept_id": config["delegator_id"],
        "requested_model": details["requested_model"],
        "requested_reasoning_effort": details["requested_reasoning_effort"],
    }
    state["effect_stage"] = "assignment_lookup"
    task = api.tasks.find_task_by_agent_creation_fingerprint(
        created_by_concept_id=config["delegator_id"],
        organisation_concept_id=config["organisation_id"],
        creation_fingerprint=identity,
    )
    if task is None:
        state["effect_stage"] = "assignment_create"
        task = api.tasks.create_task(
            **expected,
            agent_creation_fingerprint=identity,
            agent_creation_request_id=message["message_id"],
            notes=f"Requested by {config['delegator_id']} in direct message {message['message_id']} at {message['sent_at']}. Created on their behalf by {config['agent_id']}.",
        )
    state["effect_stage"] = "assignment_readback"
    task_id = task["task_concept_id"]
    task = api.task(task_id)
    if not authorised_task(task, config) or not api.native_writer(task):
        raise PermissionError("Created assignment scope changed")
    if any(task.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Created assignment fields failed canonical read-back")
    # Never reset an assignment already picked up or completed after a retry.
    if not task.get("status"):
        raise RuntimeError("Created assignment status failed canonical read-back")
    followup = {
        "message_id": message["message_id"],
        "content": message["content"],
        "deployment_requested": state["result"]["deployment_requested"],
    }
    if not task_followup(config, task_id):
        write_json(followup_path(config, task_id), followup)
    state["created_task_id"] = task_id
    write_json(path, state)
    return task_id


def finish(config, api, state, path):
    message = state["context"]["message"]
    state["effect_stage"] = "source_authorisation"
    authorise_source(config, api, message)
    # Check again at the effect boundary, including recovered on-disk states.
    result = state["result"] = validate_result(state["result"], state["context"])
    task_id = result["task_id"]
    content = result["answer"]
    if result["action"] == "create_task":
        task_id = create_assignment(config, api, state, path)
        content += f"\n\nNative assignment created and verified: {task_id}."
    if task_id:
        try:
            task = api.task(task_id)
        except api.tasks.TaskNotFoundError:
            task = None
        if not task or not authorised_task(task, config) or not api.native_writer(task):
            # A changed assignment must not strand every subsequent inbox reply.
            # Freeze the narrower report so recovery repeats the same intent.
            state["result"] = result = {
                "answer": "I can no longer act on the related task because its availability or assignment changed. I have left its state unchanged.",
                "task_id": "",
                "action": "reply",
                "deployment_requested": False,
            }
            content = result["answer"]
            task_id = ""
            write_json(path, state)
    if task_id:
        if result["action"] == "resume_task":
            state["effect_stage"] = "task_resume"
            # This exact follow-up is the new coding/deployment authority carrier.
            followup = {
                "message_id": message["message_id"],
                "content": message["content"],
                "deployment_requested": result["deployment_requested"],
            }
            prior = task_followup(config, task_id)
            if not state.get("resume_applied"):
                if (
                    prior
                    and prior["message_id"] != message["message_id"]
                    and task["status"] == "in_progress"
                ):
                    raise RuntimeError("A different follow-up is already running")
                if (
                    prior
                    and task["status"] == "pending"
                    and prior["message_id"] != message["message_id"]
                ):
                    # Several messages can arrive before the next coding run.
                    # Keep every queued instruction with its source identity.
                    followup["content"] = (
                        f"Earlier queued instructions ({prior['message_id']}):\n{prior['content']}"
                        f"\n\nAdditional instructions ({message['message_id']}):\n{message['content']}"
                    )
                    followup["message_ids"] = [
                        *prior.get("message_ids", [prior["message_id"]]),
                        message["message_id"],
                    ]
                write_json(followup_path(config, task_id), followup)
                if task["status"] != "pending":
                    api.tasks.update_task_status(
                        task_id, "pending", actor_concept_id=config["agent_id"]
                    )
                if api.task(task_id)["status"] != "pending":
                    raise RuntimeError("Task reopen read-back failed")
                state["resume_applied"] = True
                write_json(path, state)
            content += "\n\nThe task is queued for the additional work."
        note = f"Question from {config['delegator_id']}:\n{message['content']}\n\nCodex DGX answer:\n{content}"
        state["effect_stage"] = "task_note"
        comments = task_comments(api, task_id)
        existing = next(
            (
                row
                for row in comments
                if row.get("source", {}).get("message_id") == message["message_id"]
                and row.get("source", {}).get("kind") == "codex_message_followup"
            ),
            None,
        )
        if not existing:
            api.tasks.add_task_comment(
                task_id,
                body=note,
                author_concept_id=config["agent_id"],
                source={
                    "kind": "codex_message_followup",
                    "message_id": message["message_id"],
                    "question_author": config["delegator_id"],
                    "action": result["action"],
                },
            )
        comments = task_comments(api, task_id)
        if not any(
            row.get("body") == note
            and row.get("source", {}).get("message_id") == message["message_id"]
            for row in comments
        ):
            raise RuntimeError("Question/answer task note read-back failed")
    state.setdefault("report_content", content)
    state["effect_stage"] = "reply_delivery"
    write_json(path, state)
    receipt = api.messages.create_message_idempotently(
        delivery_idempotency_key="codex-inbox:" + message["message_id"],
        sender_id=config["agent_id"],
        recipient_ids=[config["delegator_id"]],
        organisation_concept_id=config["organisation_id"],
        thread_id=task_id or message.get("thread_id"),
        reply_to_id=message["message_id"],
        subject="Codex DGX reply",
        content=state["report_content"],
        metadata={
            "attribution": "Sent by the Codex DGX coding worker",
            "source_message_id": message["message_id"],
        },
    )
    reply_id = receipt.message["concept_id"]
    state["effect_stage"] = "reply_readback"
    readback = api.messages.get_message_for_user(reply_id, config["agent_id"])
    if (
        not readback
        or api.messages.project_direct_message(readback)["content"]
        != state["report_content"]
    ):
        raise RuntimeError("Inbox reply canonical read-back failed")
    state.update(phase="done", reply_id=reply_id)
    write_json(path, state)


def finish_or_retain(config, api, state, path):
    try:
        finish(config, api, state, path)
    except Exception as exc:
        # Backend exceptions can contain connection credentials. Retain the
        # operation and exception class, never copy arbitrary exception text.
        state["effect_error"] = {
            "stage": state.get("effect_stage", "reporting"),
            "error_type": type(exc).__name__,
        }
        state["phase"] = "reporting"
        write_json(path, state)
        print(
            json.dumps({"outcome": "inbox_effect_pending", **state["effect_error"]}),
            flush=True,
        )
        # If authority or delivery itself failed, do not try another send.
        if state["effect_error"]["stage"] in {
            "source_authorisation",
            "reply_delivery",
            "reply_readback",
        }:
            return
        try:
            message = state["context"]["message"]
            authorise_source(config, api, message)
            content = (
                f"I could not verify the inbox action at {state['effect_error']['stage']} "
                f"({type(exc).__name__}). The original request, answer and effect receipts "
                "are retained. The controller will retry; you do not need to resend the request."
            )
            receipt = api.messages.create_message_idempotently(
                delivery_idempotency_key="codex-inbox:"
                + message["message_id"]
                + ":effect-pending",
                sender_id=config["agent_id"],
                recipient_ids=[config["delegator_id"]],
                organisation_concept_id=config["organisation_id"],
                reply_to_id=message["message_id"],
                subject="Codex DGX recovery pending",
                content=state.setdefault("failure_report_content", content),
            )
            row = api.messages.get_message_for_user(
                receipt.message["concept_id"], config["agent_id"]
            )
            if (
                not row
                or api.messages.project_direct_message(row)["content"]
                != state["failure_report_content"]
            ):
                raise RuntimeError("Failure report read-back failed")
            state["failure_reply_id"] = receipt.message["concept_id"]
        except Exception as reporting_error:
            state["failure_report_error_type"] = type(reporting_error).__name__
        write_json(path, state)


def task_comments(api, task_id):
    comments = []
    while True:
        page = api.tasks.list_task_comments(task_id, limit=500, offset=len(comments))
        rows = page["comments"]
        comments.extend(rows)
        if not rows or len(comments) >= page.get("total", len(comments)):
            return comments


def tick(config, api, lock_fd):
    if not config.get("inbox_enabled"):
        return False
    directory = Path(config["state_root"]) / "inbox"
    directory.mkdir(parents=True, exist_ok=True)
    for path in sorted(directory.glob("*.json")):
        state = json.loads(path.read_text())
        if state["phase"] == "running":
            recover(state)
            write_json(path, state)
        if state["phase"] == "retrying":
            state["phase"] = "running"
            write_json(path, state)
            try:
                authorise_source(config, api, state["context"]["message"])
                launch(config, state, lock_fd)
            except OSError:
                recover(state)
            write_json(path, state)
        if state["phase"] == "reporting":
            finish_or_retain(config, api, state, path)
            return True
    pending = new_messages(config, api)
    if not pending:
        return False
    message = pending[0]
    path = state_path(config, message["message_id"])
    state = {
        "phase": "running",
        "context": context_for(config, api, message),
        "run_dir": str(directory / fingerprint(message["message_id"])),
    }
    write_json(path, state)
    try:
        launch(config, state, lock_fd)
    except OSError:
        recover(state)
    write_json(path, state)
    if state["phase"] != "reporting":
        return True
    finish_or_retain(config, api, state, path)
    print(
        json.dumps(
            {
                "outcome": (
                    "message_answered"
                    if state["phase"] == "done"
                    else "inbox_effect_pending"
                ),
                "message_id": message["message_id"],
                "reply_id": state.get("reply_id"),
            }
        ),
        flush=True,
    )
    return True
