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
import shlex
import subprocess
import sys
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

ACTIVITY_CHECKPOINT_SECONDS = 15

ACTIVE = {"pending", "in_progress", "blocked"}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "needs_input", "blocked"]},
        "summary": {"type": "string"},
        "evidence": {"type": "string"},
        "question": {"type": "string"},
        "deploy_commit": {"type": "string"},
    },
    "required": ["status", "summary", "evidence", "question", "deploy_commit"],
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


def resolve_execution_settings(config, inputs):
    """Resolve task preferences without substituting another requested model."""
    # Import after the controller binds its coherent backend release.
    from src.backend.utils.task_execution_preferences import (
        normalise_task_execution_preference,
    )

    resolved = {}
    for name, default in (("model", "gpt-6-astra"), ("reasoning_effort", "medium")):
        requested = inputs.get("requested_" + name)
        configured = config.get(
            "model" if name == "model" else "model_reasoning_effort"
        )
        value = requested if requested is not None else configured
        value = default if value is None else value
        value = normalise_task_execution_preference(
            value, field_name="requested_" + name
        )
        if value is None:
            raise ValueError(
                f"Invalid {name}: specify one exact model or reasoning level"
            )
        resolved[name] = value
        resolved[name + "_source"] = (
            "task" if requested is not None else "worker_default"
        )
    if "sol" in resolved["model"].lower():
        raise ValueError("Sol-family models are disabled for this worker")
    return resolved


def bind_backend_root(backend_root=None):
    """Select one reviewed backend before imports, including copied script bundles."""
    root = Path(backend_root or Path(__file__).resolve().parents[1]).resolve()
    if not (root / "src/backend/services/task_management_service.py").is_file():
        raise RuntimeError(
            "Worker backend is missing; pass --backend-root with the "
            "reviewed Von checkout containing canonical task services"
        )
    for name, module in tuple(sys.modules.items()):
        if name == "src" or name.startswith("src."):
            filename = getattr(module, "__file__", None)
            if filename and not Path(filename).resolve().is_relative_to(root):
                raise RuntimeError(
                    "Worker backend imports already belong to another "
                    "checkout; restart with the selected --backend-root"
                )
    sys.path.insert(0, str(root))
    return root


def require_task_execution_preferences(task):
    # Current canonical point and list reads include both keys even when unset.
    # An older backend omits them. Never turn an incompatible read into defaults.
    missing = {"requested_model", "requested_reasoning_effort"} - task.keys()
    if missing:
        raise RuntimeError(
            "Canonical task reader lacks execution-preference fields: "
            + ", ".join(sorted(missing))
            + ". Install the matching backend and worker release; "
            "verify --backend-root before resuming pickup."
        )
    return task


def authorised_task(task, config):
    return (
        task.get("assignee_concept_id") == config["agent_id"]
        and task.get("organisation_concept_id") == config["organisation_id"]
        and task.get("created_by_concept_id") == config["delegator_id"]
        and task.get("report_to_concept_id") in (None, config["delegator_id"])
    )


def runtime_evidence(backend_root, api):
    root = Path(backend_root).resolve()
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "commit": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "backend_root": str(root),
        "worker_script": str(Path(__file__).resolve()),
        "task_service_module": str(Path(api.tasks.__file__).resolve()),
    }


def check_task(config, api, task_id):
    """Read assignment/settings through the scheduled adapter without pickup."""
    task = api.task(task_id)
    if not task or not authorised_task(task, config) or not api.native_writer(task):
        raise PermissionError("Task is outside this worker's native assignment scope")
    # Do not call pending(): that path can report external-writer assignments.
    offset = 0
    while True:
        page = api.tasks.search_tasks(
            assignee_concept_id=config["agent_id"],
            created_by_concept_id=config["delegator_id"],
            organisation_concept_id=config["organisation_id"],
            statuses=[task["status"]],
            limit=200,
            offset=offset,
        )
        match = next(
            (row for row in page["tasks"] if row["task_concept_id"] == task_id), None
        )
        if match is not None:
            require_task_execution_preferences(match)
            for key in ("requested_model", "requested_reasoning_effort"):
                if match[key] != task[key]:
                    raise RuntimeError("Task point/list execution preferences disagree")
            break
        offset += len(page["tasks"])
        if not page["tasks"] or offset >= page["total"]:
            raise RuntimeError("Assigned task is missing from canonical list read")
    return {
        "task_id": task_id,
        "status": task["status"],
        "eligible": task["status"] in ACTIVE,
        "execution_settings": resolve_execution_settings(config, api.inputs(task)),
        "pickup_performed": False,
    }


def validate_result(value):
    fields = set(RESULT_SCHEMA["required"])
    if not isinstance(value, dict) or set(value) not in (
        fields,
        fields - {"deploy_commit"},
    ):
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
        return require_task_execution_preferences(self.tasks.get_task(task_id))

    def native_writer(self, task):
        project_id = task.get("project_concept_id")
        if project_id:
            from src.backend.services.task_project_service import get_task_project

            project = get_task_project(project_id)
            return bool(project and project.get("writer") == "von")
        return not task.get("is_imported_jira_task", False)

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
            for task in page["tasks"]:
                if not authorised_task(task, self.config):
                    continue
                if self.native_writer(task):
                    yield require_task_execution_preferences(task)
                else:
                    self.send(
                        task,
                        fingerprint(
                            [task["task_concept_id"], task["title"], "external-writer"]
                        ),
                        "This task is assigned to me, but its tracking authority is not native Von. "
                        "This worker currently processes native Von tasks. Its imported task state "
                        "has been left unchanged; please provide a native task for this worker.",
                    )
            offset += len(page["tasks"])
            if not page["tasks"] or offset >= page["total"]:
                break

    def inputs(self, task):
        """Task comments and direct replies retain their actual authorship."""
        require_task_execution_preferences(task)
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
            "requested_model": task.get("requested_model"),
            "requested_reasoning_effort": task.get("requested_reasoning_effort"),
            "comments": comments,
            "replies": [
                message for message in replies if not self.inbox_answered(message)
            ],
            **self.followup_context(task_id),
        }

    def inbox_answered(self, message):
        if not self.config.get("inbox_enabled"):
            return False
        return inbox_module().already_answered(self.config, message["message_id"])

    def followup_context(self, task_id):
        if not self.config.get("inbox_enabled"):
            return {}
        followup = inbox_module().task_followup(self.config, task_id)
        return {"followup": followup} if followup else {}

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
                return {"available": True, "session_id": session_id, "pages": pages}

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
            authorised_task(task, self.config)
            and task.get("status") in ACTIVE
            and self.native_writer(task)
        )
        content = (
            f"{result['summary']}\n\n{result['evidence']}\n\n"
            f"{result['question']}\n\nTask: {state['task_id']}\n"
            f"DGX worktree: {state.get('worktree', 'not started')}\n"
            f"Run: {state['attempt']}"
        ).strip()
        if state.get("execution_settings"):
            settings = state["execution_settings"]
            content += (
                f"\nExecution model: {settings['model']} ({settings['model_source']}); "
                f"reasoning: {settings['reasoning_effort']} "
                f"({settings['reasoning_effort_source']})."
            )
        capture = state.get("capture", {})
        if capture.get("status") == "complete":
            content += f"\nVerified execution archive: {capture['archive_url']}"
            if not capture.get("source_complete", True):
                content += "\nThe manifest records incomplete source capture."
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
            evidence_addition = (
                f"{result['evidence']}\nVon result: {message_id}\n"
                f"Execution archive: {capture.get('archive_url', 'unavailable')}\n"
                f"DGX worktree: {state.get('worktree', 'not started')}"
            )
            evidence = task.get("evidence") or ""
            if evidence_addition not in evidence:
                evidence = "\n".join(filter(None, [evidence, evidence_addition]))
            # Idempotent reconciliation through the existing task evidence field.
            self.tasks.update_task_fields(
                state["task_id"],
                fields={
                    "progress_signal": result["summary"],
                    "evidence": evidence,
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


def activity_module():
    try:
        from . import codex_von_activity
    except ImportError:
        try:
            import codex_von_activity
        except ImportError:
            from scripts import codex_von_activity
    return codex_von_activity


def archive_checkpoint(config, state, *, terminal=False):
    return activity_module().try_checkpoint(config, state, terminal=terminal)


def finish_captured(config, api, state, state_path):
    if not archive_checkpoint(config, state, terminal=True):
        state["phase"] = "archive_pending"
        write_json(state_path, state)
        if not state.get("archive_pending_reported"):
            api.send(
                {
                    "task_concept_id": state["task_id"],
                    "title": state.get("title", state["task_id"]),
                },
                state["attempt"] + ":archive-pending",
                f"Coding outcome: {state['result']['status']}. {state['result']['summary']}\n"
                "Execution archive finalisation is pending; the existing controller will retry. "
                "The task is not being marked completed. Local originals are retained.",
            )
            state["archive_pending_reported"] = True
        write_json(state_path, state)
        return
    state["phase"] = "reporting"
    api.finish(state)
    write_json(state_path, state)


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
        started_at=datetime.now(UTC).isoformat(),
        worker_revision=config.get("runtime_evidence", {}).get("commit"),
    )
    for key in (
        "capture",
        "capture_provenance",
        "archive_pending_reported",
        "finished_at",
    ):
        state.pop(key, None)
    state.pop("result", None)
    state.pop("report_task", None)
    state.pop("report_content", None)
    state.pop("deployment_final", None)
    state.pop("execution_settings", None)
    write_json(state_path, state)
    settings = resolve_execution_settings(config, inputs)
    state["execution_settings"] = settings
    context["execution_settings"] = settings
    write_json(context_path, context)
    write_json(state_path, state)
    # Capture failure must not prevent useful bounded coding progress.
    archive_checkpoint(config, state)
    write_json(state_path, state)
    if not state.get("checkout_prepared"):
        worktree.parent.mkdir(parents=True, exist_ok=True)
        origin = command(
            ["git", "-C", config["source_repo"], "remote", "get-url", "origin"]
        )
        # Independent Git metadata permits scoped commits without granting
        # writes to the live web checkout's shared .git directory. Objects are
        # borrowed read-only from the stable source repo to avoid full copies.
        if not worktree.exists():
            command(
                [
                    "git",
                    "clone",
                    "--shared",
                    "--no-checkout",
                    config["source_repo"],
                    str(worktree),
                ]
            )
        command(["git", "-C", str(worktree), "remote", "set-url", "origin", origin])
        command(["git", "-C", str(worktree), "fetch", "origin"])
        branch = f"codex/von-{task_key}"
        exists = command(["git", "-C", str(worktree), "branch", "--list", branch])
        checkout = [branch] if exists else ["-b", branch, "origin/main"]
        command(["git", "-C", str(worktree), "checkout", *checkout])
        state["checkout_prepared"] = True
        write_json(state_path, state)
    if config.get("github_command") and (worktree / ".git").is_dir():
        helper = "!" + shlex.quote(config["github_command"]) + " auth git-credential"
        command(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--replace-all",
                "credential.https://github.com.helper",
                "",
            ]
        )
        command(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--add",
                "credential.https://github.com.helper",
                helper,
            ]
        )
    for field, setting in (
        ("git_author_name", "user.name"),
        ("git_author_email", "user.email"),
    ):
        if config.get(field) and (worktree / ".git").is_dir():
            command(["git", "-C", str(worktree), "config", setting, config[field]])
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
        settings["model"],
        "-c",
        "model_reasoning_effort=" + json.dumps(settings["reasoning_effort"]),
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
    if config.get("permission_profile"):
        args[2:2] = [
            "-c",
            "default_permissions=" + json.dumps(config["permission_profile"]),
        ]
    else:
        args[2:2] = ["--sandbox", "workspace-write"]
    with (  # noqa: SIM117 - streams must outlive the process context
        (run_dir / "events.jsonl").open("a") as events,
        (run_dir / "stderr.log").open("w") as errors,
    ):
        # The inherited lock keeps another poll out even if this controller exits.
        with subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            text=True,
            env=environment,
            stdout=events,
            stderr=errors,
            pass_fds=(lock_fd,),
        ) as process:
            process.stdin.write(prompt)
            process.stdin.close()
            while True:
                try:
                    process.wait(timeout=ACTIVITY_CHECKPOINT_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    archive_checkpoint(config, state)
                    write_json(state_path, state)
    state["finished_at"] = datetime.now(UTC).isoformat()
    write_json(
        run_dir / "exit.json",
        {"returncode": process.returncode, "finished_at": state["finished_at"]},
    )
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


def apply_deployment(config, api, state, state_path, lock_fd):
    """Execute only an operator-enabled request; retain the canonical receipt."""
    result = state["result"]
    commit = result.get("deploy_commit", "").strip()
    if not commit or state.get("deployment_final"):
        return
    task = api.task(state["task_id"])
    live_inputs = api.inputs(task) if task else {}
    eligible = (
        result["status"] == "completed"
        and bool(task)
        and authorised_task(task, config)
        and task.get("status") in ACTIVE
        and api.native_writer(task)
        and fingerprint(live_inputs) == state.get("input_hash")
        and (
            not live_inputs.get("followup")
            or live_inputs["followup"].get("deployment_requested") is True
        )
    )
    run_dir = Path(state["run_dir"])
    receipt_path = run_dir / "deployment.json"
    try:
        prior_receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        prior_receipt = {}
    recover_only = not eligible and prior_receipt.get("status") == "started"
    if (not eligible and not recover_only) or not config.get("deployment_command"):
        detail = (
            "The task authority or instructions changed before deployment."
            if not eligible
            else "Deployment is not enabled for this worker."
        )
        result.update(status="blocked", summary=result["summary"] + " " + detail)
        state["deployment_final"] = "not_authorised"
        write_json(state_path, state)
        return
    with (run_dir / "deployment.log").open("a") as log:
        completed = subprocess.run(
            [
                config["deployment_command"],
                "--commit",
                commit,
                "--receipt",
                str(receipt_path),
                "--worker-lock-fd",
                str(lock_fd),
                *(["--recover-only"] if recover_only else []),
            ],
            check=False,
            stdout=log,
            stderr=log,
            pass_fds=(lock_fd,),
        )
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        receipt = {"status": "failed_without_receipt"}
    success = (
        completed.returncode == 0
        and receipt.get("status") == "deployed"
        and receipt.get("requested_commit") == commit
        and receipt.get("verification", {}).get("commit") == commit
    )
    state["deployment_final"] = receipt.get("status", "unknown")
    result["evidence"] += f"\nDeployment receipt: {receipt_path}\n" + json.dumps(
        receipt
    )
    if success:
        result[
            "summary"
        ] += f" Deployed and verified the public server at {commit[:12]}."
    else:
        result["status"] = "blocked"
        result["summary"] += (
            " Deployment did not complete: " + state["deployment_final"] + "."
        )
        result["question"] = (
            "Inspect the retained deployment receipt/log before requeuing this task."
        )
    if recover_only:
        result["status"] = "blocked"
        result[
            "summary"
        ] += " The task changed; only the interrupted deployment was reconciled."
    write_json(state_path, state)


def inbox_module():
    try:
        from . import codex_von_inbox
    except ImportError:
        try:
            import codex_von_inbox
        except ImportError:
            from scripts import codex_von_inbox
    return codex_von_inbox


def backfill_run(config, api, selection):
    """Operator-selected pairs only; no session discovery or task rerun."""
    task_id, separator, attempt = selection.partition("=")
    if (
        not separator
        or len(attempt) != 32
        or any(c not in "0123456789abcdef" for c in attempt)
    ):
        raise ValueError("Backfill requires TASK_CONCEPT_ID=32_HEX_ATTEMPT")
    task = api.task(task_id)
    if not authorised_task(task, config) or not api.native_writer(task):
        raise PermissionError("Backfill task is outside this worker assignment")
    run_dir = Path(config["state_root"]) / "runs" / attempt
    context = json.loads((run_dir / "context.json").read_text())
    if (
        context.get("task_id") != task_id
        or context.get("worker_identity") != config["agent_id"]
    ):
        raise ValueError("Backfill source does not match selected task/worker")
    path = Path(config["state_root"]) / "archive-backfills" / (attempt + ".json")
    state = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "task_id": task_id,
            "attempt": attempt,
            "run_dir": str(run_dir),
            "backfill": True,
            "execution_settings": context.get("execution_settings", {}),
        }
    )
    recover_result(state)
    archive_checkpoint(config, state, terminal=True)
    write_json(path, state)
    return {"task_id": task_id, "attempt": attempt, "capture": state["capture"]}


def tick(config, api, lock_fd):
    states_dir = Path(config["state_root"]) / "tasks"
    states_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(
        (Path(config["state_root"]) / "archive-backfills").glob("*.json")
    ):
        backfill = json.loads(path.read_text())
        if backfill.get("capture", {}).get("status") != "complete":
            archive_checkpoint(config, backfill, terminal=True)
            write_json(path, backfill)
    # Reconcile an interrupted report before selecting any new work.
    for path in sorted(states_dir.glob("*.json")):
        state = json.loads(path.read_text())
        if state["phase"] == "running":
            recover_result(state)
            write_json(path, state)
        if state["phase"] in {"reporting", "archive_pending"}:
            apply_deployment(config, api, state, path, lock_fd)
            finish_captured(config, api, state, path)
    if config.get("inbox_enabled") and inbox_module().tick(config, api, lock_fd):
        return
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
        if state["phase"] == "archive_pending":
            continue
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
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            if "run_dir" not in state:
                raise
            write_json(
                Path(state["run_dir"]) / "launch-error.json",
                {"error_type": type(exc).__name__},
            )
            recover_result(state)
            if isinstance(exc, ValueError):
                state["result"]["summary"] = (
                    "Coding execution settings rejected: " + str(exc)
                )
                state["result"][
                    "question"
                ] = "Correct this task's model or reasoning setting, then requeue it."
            write_json(path, state)
        apply_deployment(config, api, state, path, lock_fd)
        finish_captured(config, api, state, path)
        print(
            json.dumps(
                {
                    "outcome": state["phase"],
                    "task_id": task_id,
                    "message_id": state.get("message_id"),
                }
            ),
            flush=True,
        )
        return
    print(json.dumps({"outcome": "idle"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--backend-root",
        help="Reviewed canonical backend checkout; "
        "required when running a copied worker script bundle",
    )
    parser.add_argument(
        "--check-task", help="Read back one assigned task without running work"
    )
    parser.add_argument(
        "--backfill-run",
        action="append",
        default=[],
        help="Explicit TASK_CONCEPT_ID=ATTEMPT pair; at most 20; never executes coding",
    )
    options = parser.parse_args()
    if len(options.backfill_run) > 20:
        parser.error("Backfill is bounded to 20 selected pairs")
    os.umask(0o077)
    config = json.loads(Path(options.config).read_text())
    state_root = Path(config["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"outcome":"already_running"}')
            return
        backend_root = bind_backend_root(options.backend_root)
        resolve_execution_settings(config, {})
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
            api = Von(config)
            evidence = runtime_evidence(backend_root, api)
            config["runtime_evidence"] = evidence
            if options.check_task:
                evidence["task_check"] = check_task(config, api, options.check_task)
                print(json.dumps(evidence), flush=True)
                return
            write_json(state_root / "runtime.json", evidence)
            if options.backfill_run:
                for selection in options.backfill_run:
                    print(json.dumps(backfill_run(config, api, selection)), flush=True)
                return
            tick(config, api, lock.fileno())


if __name__ == "__main__":
    main()
