#!/usr/bin/env python3
"""Single-host Codex task worker using Von's canonical services.

Run with the project's PDM environment and an operator-owned JSON config.
The controller owns identity, polling and reporting; each coding run receives
only the selected task, authorised context, and an isolated Git worktree.
No daemon, model call for empty polls, or public authentication endpoint.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

ACTIVE = {"pending", "in_progress", "blocked"}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "needs_input", "blocked"]},
        "summary": {"type": "string"},
        "evidence": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["status", "summary", "evidence", "question"],
    "additionalProperties": False,
}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def authorised_task(task, config):
    return (
        task.get("assignee_concept_id") == config["agent_id"]
        and task.get("organisation_concept_id") == config["organisation_id"]
        and task.get("created_by_concept_id") == config["delegator_id"]
        and task.get("report_to_concept_id") in (None, config["delegator_id"])
    )


def validate_result(value):
    if not isinstance(value, dict) or set(value) != set(RESULT_SCHEMA["required"]):
        raise ValueError("Codex did not return the required result fields")
    if any(not isinstance(v, str) for v in value.values()):
        raise ValueError("Codex result fields must be text")
    if value["status"] not in RESULT_SCHEMA["properties"]["status"]["enum"]:
        raise ValueError("Unknown Codex outcome")
    if not value["summary"].strip() or (
        value["status"] == "completed" and not value["evidence"].strip()
    ):
        raise ValueError("Missing summary or completion evidence")
    return value


class Von:
    """Only the configured actor enters this adapter; payloads cannot select it."""

    def __init__(self, config):
        from src.backend.services import message_service, task_management_service

        self.config = config
        self.tasks = task_management_service
        self.messages = message_service

    def task(self, task_id):
        return self.tasks.get_task(task_id)

    def pending(self):
        offset = 0
        while True:
            page = self.tasks.search_tasks(
                assignee_concept_id=self.config["agent_id"],
                created_by_concept_id=self.config["delegator_id"],
                organisation_concept_id=self.config["organisation_id"],
                statuses=sorted(ACTIVE),
                limit=200,
                offset=offset,
            )
            yield from page["tasks"]
            offset += len(page["tasks"])
            if not page["tasks"] or offset >= page["total"]:
                break

    def inputs(self, task):
        """Task comments and direct replies retain their actual authorship."""
        task_id = task["task_concept_id"]
        comments = []
        offset = 0
        while True:
            page = self.tasks.list_task_comments(task_id, offset=offset, limit=500)
            comments.extend(
                c
                for c in page["comments"]
                if c.get("author_concept_id") == self.config["delegator_id"]
            )
            offset += len(page["comments"])
            if offset >= page["total"]:
                break
        replies = []
        offset = 0
        while True:
            rows = self.messages.get_messages_for_user(
                self.config["agent_id"],
                include_sent=False,
                thread_id=task_id,
                limit=100,
                skip=offset,
            )
            for row in rows:
                message = self.messages.project_direct_message(row)
                if (
                    message["sender_id"] == self.config["delegator_id"]
                    and message["organisation_concept_id"]
                    == self.config["organisation_id"]
                ):
                    replies.append(message)
            offset += len(rows)
            if len(rows) < 100:
                break
        return {
            "title": task["title"],
            "description": task.get("description"),
            "comments": comments,
            "replies": replies,
        }

    def conversation(self, task):
        from src.backend.integrations.internal_mcp.catalogue import (
            _conversation_transcript_page,
        )
        from src.backend.services import shared_conversation_service as shares
        from src.backend.services.conversation_concept_service import (
            _generate_conversation_concept_id,
        )

        session_id = task.get("conversation_session_id")
        invitations = shares.list_invites_for_user(
            user_concept_id=self.config["agent_id"]
        )
        if not session_id:
            from src.backend.db.repositories.concepts_repository import (
                ConceptsRepository,
            )

            # The Vontology conversation identity stays owner-private. Resolve
            # its deterministic locator only against invitations already
            # addressed to this actor; the transcript route still authorises.
            session_id = next(
                (
                    i["session_id"]
                    for i in invitations
                    if i.get("session_id")
                    and i.get("status") in {"pending", "accepted"}
                    and i.get("inviter_user_id") == self.config["delegator_id"]
                    and i.get("organisation_concept_id")
                    == self.config["organisation_id"]
                    and ConceptsRepository.find_one(
                        {
                            "concept_id": task["task_concept_id"],
                            "relationships.#V#hasOriginatingConversation": _generate_conversation_concept_id(
                                i["session_id"]
                            ),
                        },
                        {"concept_id": 1},
                    )
                ),
                None,
            )
        if not session_id:
            return {
                "available": False,
                "reason": "No accessible source-conversation locator. Invite Codex to the originating conversation if its context is needed.",
            }

        # Accept only an invitation from this worker's delegator in its org.
        for invitation in invitations:
            if (
                invitation.get("session_id") == session_id
                and invitation.get("status") == "pending"
                and invitation.get("inviter_user_id") == self.config["delegator_id"]
                and invitation.get("organisation_concept_id")
                == self.config["organisation_id"]
            ):
                shares.respond_to_invite(
                    invite_id=invitation["invite_id"],
                    user_concept_id=self.config["agent_id"],
                    action="accept",
                )
        pages = []
        cursor = None
        while True:
            page = _conversation_transcript_page(
                session_id=session_id, page_size=100, cursor=cursor
            )
            if page.get("success") is not True:
                return {
                    "available": False,
                    "session_id": session_id,
                    "reason": page.get("error_code")
                    or page.get("error")
                    or "Conversation access unavailable",
                }
            pages.append(page)
            cursor = page.get("next_cursor")
            if not cursor:
                return {"available": True, "pages": pages}

    def send(self, task, key, content):
        c = self.config
        allowed, _ = self.messages.authorise_direct_message_participants(
            sender_id=c["agent_id"],
            recipient_ids=[c["delegator_id"]],
            organisation_concept_id=c["organisation_id"],
        )
        if not allowed:
            raise PermissionError(
                "Worker or delegator is no longer an organisation member"
            )
        receipt = self.messages.create_message_idempotently(
            delivery_idempotency_key=key,
            sender_id=c["agent_id"],
            recipient_ids=[c["delegator_id"]],
            organisation_concept_id=c["organisation_id"],
            thread_id=task["task_concept_id"],
            subject=f"Codex DGX: {task['title']}",
            content=content,
            metadata={"attribution": "Sent by the Codex DGX coding worker"},
        )
        message_id = receipt.message["concept_id"]
        readback = self.messages.get_message_for_user(message_id, c["agent_id"])
        if (
            not readback
            or self.messages.project_direct_message(readback)["content"] != content
        ):
            raise RuntimeError("Direct message canonical read-back failed")
        return message_id

    def finish(self, state):
        try:
            task = self.task(state["task_id"])
        except self.tasks.TaskNotFoundError:
            task = {
                "task_concept_id": state["task_id"],
                "title": state.get("title", state["task_id"]),
            }
        result = state["result"]
        scope_valid = (
            authorised_task(task, self.config) and task.get("status") in ACTIVE
        )
        content = (
            f"{result['summary']}\n\n{result['evidence']}\n\n"
            f"{result['question']}\n\nTask: {state['task_id']}\n"
            f"DGX worktree: {state.get('worktree', 'not started')}\n"
            f"Run: {state['attempt']}"
        ).strip()
        # Keep the delivery intent stable across a crash after a task transition.
        # A changed/cancelled task is left untouched; the run evidence still goes
        # to the original authorised delegator.
        state.setdefault(
            "report_task", {"task_concept_id": state["task_id"], "title": task["title"]}
        )
        state.setdefault("report_content", content)
        state_path = (
            Path(self.config["state_root"])
            / "tasks"
            / (fingerprint(state["task_id"])[:16] + ".json")
        )
        write_json(state_path, state)
        message_id = self.send(
            state["report_task"], state["attempt"] + ":result", state["report_content"]
        )
        if scope_valid:
            # Idempotent reconciliation through the existing task evidence field.
            self.tasks.update_task_fields(
                state["task_id"],
                fields={
                    "progress_signal": result["summary"],
                    "evidence": f"{result['evidence']}\nVon result: {message_id}\nDGX worktree: {state.get('worktree', 'not started')}",
                    "next_checkpoint": result["question"]
                    or "Result available in Von messages.",
                },
                actor_concept_id=self.config["agent_id"],
            )
            status = "completed" if result["status"] == "completed" else "blocked"
            self.tasks.update_task_status(
                state["task_id"], status, actor_concept_id=self.config["agent_id"]
            )
            if self.task(state["task_id"])["status"] != status:
                raise RuntimeError("Task status canonical read-back failed")
        state.update(
            phase=(
                "done"
                if result["status"] == "completed" or not scope_valid
                else "waiting"
            ),
            message_id=message_id,
        )


def command(args, **kwargs):
    return subprocess.run(
        args, check=True, text=True, capture_output=True, **kwargs
    ).stdout.strip()


def launch(config, state, inputs, conversation, state_path, lock_fd):
    root = Path(config["state_root"])
    task_key = fingerprint(state["task_id"])[:16]
    worktree = Path(state.get("worktree") or root / "worktrees" / task_key)
    attempt = uuid.uuid4().hex
    run_dir = root / "runs" / attempt
    run_dir.mkdir(parents=True)
    write_json(run_dir / "schema.json", RESULT_SCHEMA)
    context = {
        "task_id": state["task_id"],
        "assignment": inputs,
        "conversation": conversation,
        "prior_result": state.get("result"),
        "worker_identity": config["agent_id"],
    }
    context_path = run_dir / "context.json"
    write_json(context_path, context)
    prompt = Path(__file__).with_name("codex_von_worker_prompt.md").read_text()
    prompt += f"\nRead your assigned context from {context_path}.\n"
    state.update(
        phase="running",
        attempt=attempt,
        worktree=str(worktree),
        run_dir=str(run_dir),
        input_hash=fingerprint(inputs),
    )
    state.pop("result", None)
    state.pop("report_task", None)
    state.pop("report_content", None)
    write_json(state_path, state)
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        command(["git", "-C", config["source_repo"], "fetch", "origin"])
        command(
            [
                "git",
                "-C",
                config["source_repo"],
                "worktree",
                "add",
                "-b",
                f"codex/von-{task_key}",
                str(worktree),
                "origin/main",
            ]
        )
    if config.get("python_environment") and not (worktree / ".venv").exists():
        (worktree / ".venv").symlink_to(
            config["python_environment"], target_is_directory=True
        )
    # Do not pass controller Mongo credentials or unrelated API keys to Codex.
    environment = {
        k: os.environ[k] for k in ("HOME", "PATH", "LANG", "TERM") if k in os.environ
    }
    args = [
        config["codex_command"],
        "exec",
        "--strict-config",
        "--model",
        config["model"],
        "--sandbox",
        "workspace-write",
        "--cd",
        str(worktree),
        "--json",
        "-c",
        "mcp_servers.von.enabled=false",
        "--output-schema",
        str(run_dir / "schema.json"),
        "--output-last-message",
        str(run_dir / "result.json"),
        "-",
    ]
    with (
        (run_dir / "events.jsonl").open("w") as events,
        (run_dir / "stderr.log").open("w") as errors,
    ):
        # The inherited lock keeps another poll out even if this controller exits.
        completed = subprocess.run(
            args,
            check=False,
            input=prompt,
            text=True,
            env=environment,
            stdout=events,
            stderr=errors,
            pass_fds=(lock_fd,),
        )
    write_json(run_dir / "exit.json", {"returncode": completed.returncode})
    recover_result(state)
    write_json(state_path, state)


def recover_result(state):
    """Only a successful process plus valid structured output is completion."""
    run_dir = Path(state["run_dir"])
    try:
        if json.loads((run_dir / "exit.json").read_text())["returncode"] != 0:
            raise ValueError("Codex exited unsuccessfully")
        result = validate_result(json.loads((run_dir / "result.json").read_text()))
    except (OSError, ValueError, KeyError) as exc:
        result = {
            "status": "blocked",
            "summary": "The Codex run did not produce a verified terminal result.",
            "evidence": f"{type(exc).__name__}. Work and logs remain at {run_dir}.",
            "question": "Please set this task back to pending to retry after checking the retained run.",
        }
    state.update(phase="reporting", result=result)


def tick(config, api, lock_fd):
    states_dir = Path(config["state_root"]) / "tasks"
    states_dir.mkdir(parents=True, exist_ok=True)
    # Reconcile an interrupted report before selecting any new work.
    for path in sorted(states_dir.glob("*.json")):
        state = json.loads(path.read_text())
        if state["phase"] == "running":
            recover_result(state)
            write_json(path, state)
        if state["phase"] == "reporting":
            api.finish(state)
            write_json(path, state)
    for task in api.pending():
        if not authorised_task(task, config) or task.get("status") not in ACTIVE:
            continue
        task_id = task["task_concept_id"]
        path = states_dir / (fingerprint(task_id)[:16] + ".json")
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"task_id": task_id, "phase": "new"}
        )
        state["title"] = task["title"]
        inputs = api.inputs(task)
        if (
            state["phase"] in {"waiting", "done"}
            and task["status"] != "pending"
            and state.get("input_hash") == fingerprint(inputs)
        ):
            continue
        conversation = api.conversation(task)
        # The model may proceed from adequate task text when a transcript is unavailable.
        api.send(
            task,
            fingerprint([task_id, fingerprint(inputs)]) + ":started",
            f"I am picking up this task on the DGX: {task['title']}.\nTask: {task_id}\n"
            "I will send the result or a question here. Reply in this task thread or add a task comment.",
        )
        api.tasks.update_task_status(
            task_id, "in_progress", actor_concept_id=config["agent_id"]
        )
        try:
            launch(config, state, inputs, conversation, path, lock_fd)
        except (OSError, subprocess.CalledProcessError) as exc:
            if "run_dir" not in state:
                raise
            write_json(
                Path(state["run_dir"]) / "launch-error.json",
                {"error_type": type(exc).__name__},
            )
            recover_result(state)
            write_json(path, state)
        api.finish(state)
        write_json(path, state)
        print(
            json.dumps(
                {
                    "outcome": state["phase"],
                    "task_id": task_id,
                    "message_id": state["message_id"],
                }
            ),
            flush=True,
        )
        return
    print(json.dumps({"outcome": "idle"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    options = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(options.config).read_text())
    if "sol" in config["model"].lower():
        raise ValueError("Sol-family models are disabled for this worker")
    state_root = Path(config["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"outcome":"already_running"}')
            return
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.backend.integrations.internal_mcp.gateway import (
            INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
            bind_internal_mcp_actor_context_source,
        )
        from src.backend.security.access_control import (
            force_access_control_enforcement,
            override_current_actor,
        )
        from src.backend.services.organisation_membership_service import (
            resolve_user_organisation_membership,
        )

        with ExitStack() as stack:
            stack.enter_context(
                override_current_actor(config["agent_id"], config["organisation_id"])
            )
            stack.enter_context(force_access_control_enforcement())
            stack.enter_context(
                bind_internal_mcp_actor_context_source(
                    INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
                    preexisting_actor_context=(
                        config["agent_id"],
                        config["organisation_id"],
                    ),
                )
            )
            if not resolve_user_organisation_membership(
                config["agent_id"], config["organisation_id"]
            ):
                raise PermissionError(
                    "The configured worker has no organisation membership"
                )
            tick(config, Von(config), lock.fileno())


if __name__ == "__main__":
    main()
