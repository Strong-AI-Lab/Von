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
        fingerprint,
        resolve_execution_settings,
        write_json,
    )
except ImportError:
    from codex_von_worker import (
        authorised_task,
        fingerprint,
        resolve_execution_settings,
        write_json,
    )

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "task_id": {"type": "string"},
        "action": {"type": "string", "enum": ["reply", "resume_task"]},
        "deployment_requested": {"type": "boolean"},
    },
    "required": ["answer", "task_id", "action", "deployment_requested"],
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
        config["agent_id"], config["delegator_id"], limit=30
    )
    conversation = [api.messages.project_direct_message(row) for row in rows]
    conversation = [
        row
        for row in conversation
        if row.get("organisation_concept_id") == config["organisation_id"]
        and row.get("sent_at")
        and timestamp(row["sent_at"]) <= timestamp(message["sent_at"])
    ]
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
    return {"message": message, "recent_messages": conversation, "tasks": tasks}


def validate_result(value, context):
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        raise ValueError("Missing inbox result fields")
    if not isinstance(value["answer"], str) or not value["answer"].strip():
        raise ValueError("Empty inbox answer")
    if value["action"] not in ("reply", "resume_task") or not isinstance(
        value["deployment_requested"], bool
    ):
        raise ValueError("Invalid inbox action")
    ids = {task["task_concept_id"] for task in context["tasks"]}
    if value["task_id"] not in ids | {""}:
        raise ValueError("Task was not in authorised context")
    if value["action"] == "resume_task" and not value["task_id"]:
        raise ValueError("Resuming requires an identified task")
    if value["action"] == "reply" and value["deployment_requested"]:
        raise ValueError("A reply cannot deploy")
    return value


def recover(state):
    run = Path(state["run_dir"])
    try:
        if json.loads((run / "exit.json").read_text())["returncode"] != 0:
            raise ValueError("Inbox process failed")
        state["result"] = validate_result(
            json.loads((run / "result.json").read_text()), state["context"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        state["result"] = {
            "answer": "I could not complete this reply. The response run was interrupted or returned an invalid result; please resend your question to retry.",
            "task_id": "",
            "action": "reply",
            "deployment_requested": False,
        }
    state["phase"] = "reporting"


def prepare_attachment_inputs(config, context, run):
    """Copy recipient-authorised bytes into this response's private sandbox.

    Paths come from this receiving controller, never a sender's machine. The
    canonical message is re-read by the loader; supplied descriptors confer no
    authority. Retrying preparation replaces the same content-addressed files.
    """
    message = context.get("message", {})
    references = message.get("attachments", [])
    if not references:
        return
    from src.backend.security.access_control import override_current_actor
    from src.backend.services.message_attachment_service import (
        attachment_text,
        load_attachment_reference,
    )

    inputs = []
    for reference in references[:8]:
        item = {
            "concept_id": reference.get("concept_id"),
            "filename": reference.get("filename"),
        }
        try:
            with override_current_actor(config["agent_id"], config["organisation_id"]):
                info, data = load_attachment_reference(
                    {
                        "concept_id": reference.get("concept_id"),
                        "message_id": reference.get("message_id")
                        or message["message_id"],
                    },
                    config["agent_id"],
                )
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
            }.get(info["content_type"], ".bin")
            directory = Path(run) / "attachments"
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / (info["sha256"] + suffix)
            path.write_bytes(data)
            path.chmod(0o600)
            item.update(
                local_path=str(path.resolve()),
                content_type=info["content_type"],
                sha256=info["sha256"],
            )
            if info["content_type"].startswith("image/"):
                item["instruction"] = (
                    "Use view_image on local_path to inspect the supplied original. Filename alone is not image evidence."
                )
            else:
                item["content"] = attachment_text(info, data)
        except (ValueError, PermissionError, OSError):
            item["error"] = (
                "Attachment unavailable in this response; do not claim to have inspected it."
            )
        inputs.append(item)
    context["attachment_inputs"] = inputs
    if len(references) > 8:
        inputs.append({"error": "Only the first eight attachments were prepared in this run; remaining references have not been inspected."})


def launch(config, state, lock_fd):
    run = Path(state["run_dir"])
    run.mkdir(parents=True, exist_ok=True)
    prepare_attachment_inputs(config, state["context"], run)
    write_json(run / "schema.json", SCHEMA)
    write_json(run / "context.json", state["context"])
    prompt = Path(__file__).with_name("codex_von_inbox_prompt.md").read_text()
    prompt += "\nContext for this reply:\n" + json.dumps(state["context"], default=str)
    settings = resolve_execution_settings(config, {})
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


def finish(config, api, state, path):
    message = state["context"]["message"]
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
    result = state["result"]
    task_id = result["task_id"]
    content = result["answer"]
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
            # This exact follow-up is the new coding/deployment authority carrier.
            followup = {
                "message_id": message["message_id"],
                "content": message["content"],
                "deployment_requested": result["deployment_requested"],
                **(
                    {"attachments": message["attachments"]}
                    if message.get("attachments")
                    else {}
                ),
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
                    if prior.get("attachments") or followup.get("attachments"):
                        followup["attachments"] = [
                            *prior.get("attachments", []),
                            *followup.get("attachments", []),
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
    readback = api.messages.get_message_for_user(reply_id, config["agent_id"])
    if (
        not readback
        or api.messages.project_direct_message(readback)["content"]
        != state["report_content"]
    ):
        raise RuntimeError("Inbox reply canonical read-back failed")
    state.update(phase="done", reply_id=reply_id)
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
        if state["phase"] == "reporting":
            finish(config, api, state, path)
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
    finish(config, api, state, path)
    print(
        json.dumps(
            {
                "outcome": "message_answered",
                "message_id": message["message_id"],
                "reply_id": state["reply_id"],
            }
        ),
        flush=True,
    )
    return True
