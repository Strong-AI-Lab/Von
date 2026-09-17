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
import re
import shlex
import subprocess
import sys
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

try:
    from . import codex_von_retry as retry_policy
except ImportError:
    try:
        import codex_von_retry as retry_policy
    except ImportError:
        from scripts import codex_von_retry as retry_policy

try:
    from . import codex_von_supervision as supervision
except ImportError:
    try:
        import codex_von_supervision as supervision
    except ImportError:
        from scripts import codex_von_supervision as supervision

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
        "blocker": retry_policy.BLOCKER_SCHEMA,
        "repair_receipt_json": {"type": ["string", "null"]},
    },
    "required": [
        "status",
        "summary",
        "evidence",
        "question",
        "deploy_commit",
        "blocker",
        "repair_receipt_json",
    ],
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


def input_fingerprint(inputs):
    # Observation timestamps and lifecycle transitions are context, not new work.
    return fingerprint(
        {
            key: value
            for key, value in inputs.items()
            if key not in {"context_provenance", "status"}
        }
    )


def capability_context(config, *, read_only, worktree=None):
    """Non-secret capabilities; configuration is not provider/served evidence."""
    return {
        "worker_identity": config["agent_id"],
        "delegator_id": config["delegator_id"],
        "organisation_id": config["organisation_id"],
        "source_repo": config.get("source_repo"),
        "worktree": str(worktree) if worktree else None,
        "controller_runtime": config.get("runtime_evidence", {"available": False}),
        "public_served_revision": {"available": False, "reason": "Not probed here"},
        "read_only_host_inspection": True,
        "run_can_edit_checkout": not read_only,
        "von_mcp": "disabled; canonical reads/effects belong to the controller",
        "controller_actions": ["reply", "resume_task", "create_task"],
        "deployment_enabled": bool(config.get("deployment_command")),
        "retry_probe_keys": sorted((config.get("retry_probes") or {}).keys()),
        "supervision_enabled": bool(config.get("supervision_enabled")),
        "limitations": [
            "Use available shell tools for non-secret host/repository facts; missing supplied facts do not mean inspection is unavailable.",
            "Do not access credentials, private databases or live-service mutation routes.",
            "Sandbox/device visibility may differ from the host; report observations and unavailable facts separately.",
            "Browser, image viewing and image generation depend on tools actually exposed in the run; subscription access does not establish them.",
            "Execution settings are configured launch arguments, not provider-observed model identity.",
        ],
    }


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
    if (
        task.get("assignee_concept_id") != config["agent_id"]
        or task.get("organisation_concept_id") not in (None, config["organisation_id"])
    ):
        return False
    if not config.get("supervision_enabled") and task.get(
        "report_to_concept_id"
    ) not in (None, config["delegator_id"]):
        return False
    from src.backend.services.task_dispatch_authority_service import (
        has_assignment_record,
        is_authorised_assignment,
    )
    if (
        not task.get("federation")
        and task.get("organisation_concept_id") == config["organisation_id"]
        and task.get("created_by_concept_id") == config["delegator_id"]
        and not has_assignment_record(task["task_concept_id"])
    ):
        return True
    if not task.get("dispatch_requested"):
        return False
    return is_authorised_assignment(task, config["delegator_id"])


def referenced_tasks(config, api, supplied, task_ids=()):
    """Resolve explicit references, independently of a bounded task projection.

    This does not pick up tasks or enlarge the controller's assignment scope.
    Canonical not-found is actor-scoped and is not proof of global absence.
    """
    ids = set(task_ids) | set(
        re.findall(r"#V#task_agent_[A-Za-z0-9_]+", json.dumps(supplied, default=str))
    )
    results = []
    for task_id in sorted(ids - {None, ""})[:50]:
        item = {
            "task_id": task_id,
            "source": "canonical task service.get_task",
            "scope": "configured worker native assignments",
        }
        try:
            row = api.task(task_id)
            if not authorised_task(row, config) or not api.native_writer(row):
                item["status"] = "outside_assignment_scope"
            else:
                item.update(status="found", task=row)
        except api.tasks.TaskNotFoundError:
            item["status"] = "not_found_in_actor_scope"
        except Exception as exc:
            item.update(status="lookup_failed", error_type=type(exc).__name__)
        results.append(item)
    return {
        "results": results,
        "omitted": max(0, len(ids - {None, ""}) - 50),
        "absence_semantics": "An omitted or unprojected reference is not a canonical not-found result.",
    }


def delegated_task_image_context(config, supplied):
    """Resolve operator-authorised source images from a fresh assigned task.

    Context from another conversation, a prior result or the model cannot add
    identifiers to this handoff. Credentials stay in the trusted controller.
    """
    policy = config.get("delegated_task_images") or {}
    if policy.get("enabled") is not True or not policy.get("authorisation_reference"):
        return {}
    task_id = supplied.get("task_id") if isinstance(supplied, dict) else None
    if not task_id:
        return {}
    from src.backend.services.file_copy_reference_service import (
        extract_file_copy_concept_ids_from_text,
    )

    api = Von(config)
    task = api.task(task_id)
    if not (
        authorised_task(task, config)
        and task.get("status") in ACTIVE
        and api.native_writer(task)
    ):
        return {}
    inputs = api.inputs(task)
    sources = {
        "description": task.get("description"),
        "evidence": task.get("evidence"),
        "notes": task.get("notes"),
        "attachments": inputs.get("attachments"),
        "comments": [
            row
            for row in inputs.get("comments", [])
            if row.get("author_concept_id") == config["delegator_id"]
        ],
        "replies": [
            row
            for row in inputs.get("replies", [])
            if row.get("sender_id") == config["delegator_id"]
            and row.get("organisation_concept_id") == config["organisation_id"]
        ],
    }
    return {
        "task_id": task_id,
        "source_actor": config["delegator_id"],
        "recipient_actor": config["agent_id"],
        "organisation_id": config["organisation_id"],
        "authorisation_reference": policy["authorisation_reference"],
        "file_copy_concept_ids": extract_file_copy_concept_ids_from_text(
            json.dumps(sources, default=str)
        ),
    }


def stage_file_copy_evidence(config, supplied, run_dir):
    """Controller-only authorised byte reads; expose local copies, never blob URLs."""
    from src.backend.services.computer_file_copy_service import (
        build_file_copy_artifact_record,
        fetch_file_copy_bytes,
    )
    from src.backend.services.file_copy_reference_service import (
        extract_file_copy_concept_ids_from_text,
    )

    ids = extract_file_copy_concept_ids_from_text(json.dumps(supplied, default=str))
    try:
        delegation = delegated_task_image_context(config, supplied)
    except Exception as exc:
        delegation = {"lookup_error_type": type(exc).__name__}
    results = []
    for concept_id in ids[:20]:
        item = {
            "file_copy_concept_id": concept_id,
            "source": "referenced file copy",
            "native_attachment_asserted": False,
            "content_available": False,
            "scope": "configured worker actor and organisation",
        }
        try:
            record = None
            result = fetch_file_copy_bytes(
                file_copy_concept_id=concept_id,
                user_concept_id=config["agent_id"],
                organisation_concept_id=config["organisation_id"],
                max_bytes=5_000_000,
            )
            if (
                not result.get("success")
                and result.get("error") == "not_found"
                and concept_id in delegation.get("file_copy_concept_ids", [])
            ):
                from src.backend.security.access_control import override_current_actor

                # This task-scoped read uses an explicit operator grant. The
                # coding process receives image copies, never the owner's access.
                with override_current_actor(
                    config["delegator_id"], config["organisation_id"]
                ):
                    result = fetch_file_copy_bytes(
                        file_copy_concept_id=concept_id,
                        user_concept_id=config["delegator_id"],
                        organisation_concept_id=config["organisation_id"],
                        max_bytes=5_000_000,
                    )
                    if result.get("success"):
                        if not (result["info"].content_type or "").startswith("image/"):
                            result = {
                                "success": False,
                                "error": "delegated_image_required",
                            }
                        else:
                            record = (
                                build_file_copy_artifact_record(
                                    file_copy_concept_id=concept_id
                                )
                                or {}
                            )
                            item.update(
                                scope="explicit task-scoped source-image delegation",
                                source_actor=config["delegator_id"],
                                delegated_task_id=delegation["task_id"],
                                authorisation_reference=delegation[
                                    "authorisation_reference"
                                ],
                            )
            if not result.get("success"):
                item["error_code"] = result.get("error")
                item["status"] = (
                    "not_found_in_actor_scope"
                    if result.get("error") == "not_found"
                    else "content_unavailable"
                )
            else:
                data = result["data"]
                digest = hashlib.sha256(data).hexdigest()
                if record is None:
                    record = (
                        build_file_copy_artifact_record(file_copy_concept_id=concept_id)
                        or {}
                    )
                expected = record.get("sha256")
                item.update(
                    sha256=digest,
                    canonical_sha256=expected,
                    size_bytes=len(data),
                    original_filename=result["info"].original_filename,
                    content_type=result["info"].content_type,
                )
                if expected and expected.lower() != digest:
                    item["status"] = "checksum_mismatch"
                else:
                    suffix = {
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/webp": ".webp",
                    }.get(result["info"].content_type, ".bin")
                    destination = (
                        Path(run_dir) / "evidence" / (fingerprint(concept_id) + suffix)
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    destination.chmod(0o600)
                    item.update(
                        status="available",
                        content_available=True,
                        local_path=str(destination),
                        checksum_verified=bool(expected),
                    )
        except Exception as exc:
            item.update(status="lookup_failed", error_type=type(exc).__name__)
        results.append(item)
    return {
        "results": results,
        "omitted": max(0, len(ids) - 20),
        "source": "canonical actor-scoped file-copy service",
        "delegation_lookup_error": delegation.get("lookup_error_type"),
    }


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
            organisation_concept_id=config["organisation_id"],
            organisation_scope_mode="current_plus_unscoped",
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
    # Strict output requires every property; retained old results may omit blocker.
    optional = {"blocker", "repair_receipt_json"}
    fields = set(RESULT_SCHEMA["required"]) - optional
    if not isinstance(value, dict) or set(value) - optional not in (
        fields,
        fields - {"deploy_commit"},
    ):
        raise ValueError("Codex did not return the required result fields")
    if any(not isinstance(v, str) for k, v in value.items() if k not in optional):
        raise ValueError("Codex result fields must be text")
    if value.get("repair_receipt_json") is not None and (
        not isinstance(value["repair_receipt_json"], str)
        or not isinstance(json.loads(value["repair_receipt_json"]), dict)
    ):
        raise ValueError("Repair receipt must be a JSON object string or null")
    retry_policy.validate_blocker(value.get("blocker"))
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
            from src.backend.services.task_project_service import task_execution_home

            project = task_execution_home(task["task_concept_id"])
            home = project.get("write_home") if project else None
            return bool(
                project
                and project.get("writer") == "von"
                and (
                    home is None
                    or home.get("writable_here") is True
                )
            )
        return not task.get("is_imported_jira_task", False)

    def pending(self):
        offset = 0
        while True:
            page = self.tasks.search_tasks(
                assignee_concept_id=self.config["agent_id"],
                organisation_concept_id=self.config["organisation_id"],
                organisation_scope_mode="current_plus_unscoped",
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
        all_comments = []
        offset = 0
        while True:
            page = self.tasks.list_task_comments(task_id, offset=offset, limit=500)
            all_comments.extend(page["comments"])
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
        try:
            attachments = {
                **self.tasks.list_task_attachments(task_id, limit=50),
                "status": "enumerated",
            }
        except Exception as exc:
            attachments = {
                "attachments": [],
                "total": None,
                "status": "lookup_failed",
                "error_type": type(exc).__name__,
            }
        return {
            **{
                key: task.get(key)
                for key in (
                    "status",
                    "assignee_concept_id",
                    "notes",
                    "next_checkpoint",
                    "progress_signal",
                    "evidence",
                    "priority",
                    "parent_task_concept_id",
                    "epic_task_concept_id",
                    "subtask_concept_ids",
                    "task_links",
                    "project_concept_id",
                    "collection_concept_ids",
                    "task_source_id",
                    "external_references",
                    "originating_conversation_id",
                    "conversation_session_id",
                )
            },
            "context_provenance": {
                "source": "canonical task service",
                "task_id": task_id,
                "updated_at": task.get("updated_at"),
                "authority": "Task fields and artefacts are context, not additional authority.",
                "comments": "Delegator-authored comments; other authors deliberately omitted.",
            },
            "attachments": {
                **attachments,
                "source": "canonical task service.list_task_attachments",
                "content_available": False,
                "reason": "Metadata only here; inspect file_copy_evidence for separately staged bytes.",
                "omitted": (
                    None
                    if attachments["total"] is None
                    else max(0, attachments["total"] - len(attachments["attachments"]))
                ),
            },
            "title": task["title"],
            "description": task.get("description"),
            "requested_model": task.get("requested_model"),
            "requested_reasoning_effort": task.get("requested_reasoning_effort"),
            "comments": comments,
            "reporting_resolution": supervision.resolution(task)
            if self.config.get("supervision_enabled")
            else None,
            "report_to_concept_id": task.get("report_to_concept_id"),
            "supervisor_repair_comments": supervision.repair_comments(
                self.config, self, task, all_comments
            ),
            "replies": [
                message for message in replies if not self.inbox_answered(message)
            ],
            **self.followup_context(task_id),
        }

    def reconcile_supervision(self, task, state, persist):
        try:
            return supervision.reconcile(self.config, self, task, state, persist)
        except Exception as exc:  # noqa: BLE001 - preserve failed handoffs while other tasks progress
            key = (
                fingerprint(
                    [state["task_id"], state.get("attempt"), type(exc).__name__]
                )
                + ":supervision-failure"
            )
            if state.get("supervision_failure_message", {}).get("key") != key:
                message_id = self.send(
                    task,
                    key,
                    f"Supervisor handoff for {state['task_id']} could not be reconciled ({type(exc).__name__}). "
                    "Review the existing repair task, current assignment, organisation membership and reporting configuration. "
                    "The source remains waiting without another coding launch; retained state is available to the controller.",
                )
                state["supervision_failure_message"] = {
                    "key": key,
                    "message_id": message_id,
                }
                persist()
            return None

    def record_supervisor_repair(self, task, result):
        return supervision.record_repair(self.config, self, task, result)

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
            subject=f"{c.get('display_name', c['agent_id'])}: {task['title']}",
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

    def start_execution(self, state, *, source="codex_dgx"):
        from src.backend.services import task_execution_timing_service as timing

        state["execution_source"] = source
        task = timing.start(
            state["task_id"],
            attempt_id=state["attempt"],
            actor_concept_id=self.config["agent_id"],
            started_at=state["execution_started_at"],
            source=source,
        )
        state["execution_timing"] = task["execution_timing"]
        return task

    def finish_execution(self, state):
        from src.backend.services import task_execution_timing_service as timing

        self.start_execution(state, source=state.get("execution_source", "codex_dgx"))
        task = timing.finish(
            state["task_id"],
            attempt_id=state["attempt"],
            actor_concept_id=self.config["agent_id"],
            outcome=state["result"]["status"],
            ended_at=state.get("finished_at"),
        )
        # Optional accounting must never hold up accepted timing/completion.
        # Only controller-retained events are read, never model result fields.
        try:
            receipt = execution_usage(state)
            task = timing.record_usage(
                state["task_id"],
                attempt_id=state["attempt"],
                actor_concept_id=self.config["agent_id"],
                receipt=receipt,
            )
            state.pop("usage_recording_error_type", None)
        except Exception as exc:  # noqa: BLE001 - accounting cannot gate timing
            state["usage_recording_error_type"] = type(exc).__name__
        state["execution_timing"] = task["execution_timing"]
        return task

    def finish(self, state):
        try:
            task = self.task(state["task_id"])
        except self.tasks.TaskNotFoundError:
            task = {
                "task_concept_id": state["task_id"],
                "title": state.get("title", state["task_id"]),
            }
        result = state["result"]
        blocker = retry_policy.retain_blocker(self.config, state)
        scope_valid = (
            authorised_task(task, self.config)
            and task.get("status") in ACTIVE
            and self.native_writer(task)
        )
        timing_receipt = None
        if state.get("execution_started_at") and (
            scope_valid
            or (
                authorised_task(task, self.config)
                and self.native_writer(task)
                and task.get("status") == "completed"
                and result["status"] == "completed"
                and any(
                    a["attempt_id"] == state["attempt"] and a.get("outcome") == "completed"
                    for a in task.get("execution_timing", {}).get("attempts", [])
                )
            )
        ):
            # Supervisor receipts require the still-active repair assignment.
            if scope_valid:
                self.record_supervisor_repair(task, result)
            # Canonical acceptance precedes its completion message. Replaying
            # this attempt cannot complete a reopened task.
            timing_receipt = self.finish_execution(state)["execution_timing"]
            task = self.task(state["task_id"])
            scope_valid = task["status"] == (
                "completed" if result["status"] == "completed" else "blocked"
            )
        content = (
            f"{result['summary']}\n\n{result['evidence']}\n\n"
            f"{result['question']}\n\nTask: {state['task_id']}\n"
            f"DGX worktree: {state.get('worktree', 'not started')}\n"
            f"Run: {state['attempt']}"
        ).strip()
        if timing_receipt:
            content += "\n\n" + execution_timing_text(timing_receipt, state["attempt"])
        if state.get("usage_recording_error_type"):
            content += (
                "\nUsage receipt could not be recorded; timing acceptance succeeded."
            )
        if blocker:
            content += "\n\n" + retry_policy.waiting_text(blocker)
        if state.get("execution_settings"):
            settings = state["execution_settings"]
            content += (
                f"\nConfigured execution model: {settings['model']} ({settings['model_source']}); "
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
            if timing_receipt is None:
                self.record_supervisor_repair(task, result)
            evidence_addition = (
                f"{result['evidence']}\nVon result: {message_id}\n"
                f"Execution archive: {capture.get('archive_url', 'unavailable')}\n"
                f"DGX worktree: {state.get('worktree', 'not started')}"
            )
            evidence = task.get("evidence") or ""
            if evidence_addition not in evidence:
                evidence = "\n".join(filter(None, [evidence, evidence_addition]))
            # Idempotent reconciliation through the existing task evidence field.
            report_fields = {
                "progress_signal": result["summary"],
                "evidence": evidence,
                "next_checkpoint": (
                    retry_policy.waiting_text(blocker)
                    if blocker
                    else result["question"] or "Result available in Von messages."
                ),
            }
            self.tasks.update_task_fields(
                state["task_id"],
                fields=report_fields,
                actor_concept_id=self.config["agent_id"],
            )
            status = "completed" if result["status"] == "completed" else "blocked"
            self.tasks.update_task_status(
                state["task_id"], status, actor_concept_id=self.config["agent_id"]
            )
            if self.task(state["task_id"])["status"] != status:
                raise RuntimeError("Task status canonical read-back failed")
            if blocker:
                state["waiting_report"] = {"delivered": True, "message_id": message_id}
            # Reporting changes evidence/checkpoint fields now supplied as context.
            # Do not mistake this worker's own report for a new instruction.
            # Retain the launch snapshot so a concurrent user note/comment
            # remains new input; a fresh whole-task read would swallow it.
            if "input_snapshot" in state:
                state["input_hash"] = input_fingerprint(
                    {
                        **state["input_snapshot"],
                        **report_fields,
                    }
                )
        state.update(
            phase=(
                "done"
                if result["status"] == "completed" or not scope_valid
                else "waiting"
            ),
            message_id=message_id,
        )


def execution_usage(state):
    from src.backend.services import coding_execution_usage as usage

    model = (state.get("execution_settings") or {}).get("model")
    if state.get("execution_source") == "codex_vscode":
        return usage.unknown(
            "Interactive bridge has no trusted task-specific token receipt or turn interval",
            configured_model=model,
            thread_id=state.get("consumer_thread"),
            source="codex_vscode.consumer_thread",
        )
    if not state.get("run_dir"):
        return usage.unknown("No retained exec stream", configured_model=model)
    try:
        with (Path(state["run_dir"]) / "events.jsonl").open("rb") as stream:
            return usage.from_exec_stream(stream, configured_model=model)
    except OSError:
        return usage.unknown("Exec stream unavailable", configured_model=model)


def execution_timing_text(timing, attempt_id):
    attempt = next(a for a in timing["attempts"] if a["attempt_id"] == attempt_id)
    usage = attempt.get("usage") or {}
    counters = usage.get("tokens")
    usage_text = (
        f"Observed root tokens (partial coverage): input {counters['input_tokens']}, "
        f"cached input {counters['cached_input_tokens']} (included in input), "
        f"output {counters['output_tokens']}."
        if counters
        else "Attempt token usage: unknown."
    )
    return (
        f"Execution start (UTC): {attempt['started_at']}\n"
        f"Execution end (UTC): {attempt.get('ended_at') or 'unknown'}\n"
        f"Task completion accepted (UTC): {attempt.get('completion_accepted_at') or 'not completed'}\n"
        f"Attempt elapsed wall-clock seconds (includes waits): {attempt.get('elapsed_wall_seconds')}\n"
        f"{usage_text}\nTask cost: unknown; subscription usage is not a per-task invoice."
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
        "retry_admission": state.get("pending_admission"),
        "worker_identity": config["agent_id"],
        "capabilities": capability_context(config, read_only=False, worktree=worktree),
    }
    api = Von(config)
    context["task_lookup"] = referenced_tasks(config, api, [inputs, conversation])
    for item in context["task_lookup"]["results"]:
        if item["status"] == "found":
            try:
                item["assignment_context"] = api.inputs(item["task"])
            except Exception as exc:
                item["evidence_lookup"] = {
                    "status": "lookup_failed",
                    "error_type": type(exc).__name__,
                }
    context["file_copy_evidence"] = stage_file_copy_evidence(config, context, run_dir)
    followup = inputs.get("followup") or {}
    if followup.get("attachments"):
        attachment_context = {"message": followup}
        inbox_module().prepare_attachment_inputs(config, attachment_context, run_dir)
        context["attachment_inputs"] = attachment_context.get("attachment_inputs", [])
    context_path = run_dir / "context.json"
    write_json(context_path, context)
    prompt = Path(__file__).with_name("codex_von_worker_prompt.md").read_text()
    prompt += f"\nRead your assigned context from {context_path}.\n"
    retry_policy.consume(state, state.pop("pending_admission", {"kind": "initial"}))
    state.update(
        phase="running",
        attempt=attempt,
        worktree=str(worktree),
        run_dir=str(run_dir),
        input_hash=input_fingerprint(inputs),
        input_snapshot=inputs,
        prepared_at=datetime.now(UTC).isoformat(),
        worker_revision=config.get("runtime_evidence", {}).get("commit"),
    )
    for key in (
        "capture",
        "capture_provenance",
        "archive_pending_reported",
        "finished_at",
        "started_at",
        "execution_started_at",
        "execution_timing",
        "execution_source",
        "usage_recording_error_type",
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
            # The child is alive and waiting for its authorised input. Queue,
            # checkout preparation and reservation are not execution time.
            state["started_at"] = state["execution_started_at"] = datetime.now(
                UTC
            ).isoformat()
            write_json(state_path, state)
            api.start_execution(state)
            write_json(state_path, state)
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
        exit_receipt = json.loads((run_dir / "exit.json").read_text())
        if exit_receipt.get("finished_at"):
            state["finished_at"] = exit_receipt["finished_at"]
        if exit_receipt["returncode"] != 0:
            raise ValueError("Codex exited unsuccessfully")
        result = validate_result(json.loads((run_dir / "result.json").read_text()))
    except (OSError, ValueError, KeyError) as exc:
        result = {
            "status": "blocked",
            "summary": "The Codex run did not produce a verified terminal result.",
            "evidence": f"{type(exc).__name__}. Work and logs remain at {run_dir}.",
            "question": "Reconcile the retained work and effects, then give an explicit retry reason in the task thread.",
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
        and input_fingerprint(live_inputs) == state.get("input_hash")
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
        result["summary"] += (
            f" Deployed and verified the public server at {commit[:12]}."
        )
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
        result["summary"] += (
            " The task changed; only the interrupted deployment was reconciled."
        )
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


def probe_recovery(config, state, state_path):
    """Run only an operator-registered cheap read probe, never model output code.

    Probe bounds protect controller/queue liveness. The operator selects these
    from that probe's declared/observed duration, independently of coding time.
    Saving the next check first also backs off an interrupted/unknown probe.
    """
    blocker = state.get("blocker") or {}
    if blocker.get("kind") != "transient":
        return None
    probe = (config.get("retry_probes") or {}).get(blocker.get("probe_key"))
    if not probe:
        return None
    now = datetime.now(UTC)
    next_check = retry_policy.timestamp(blocker.get("next_check_at"))
    if next_check and now < next_check:
        return None
    from datetime import timedelta

    timeout = float(probe["timeout_seconds"])
    interval = float(probe["interval_seconds"])
    maximum = float(probe["max_interval_seconds"])
    argv = probe["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) for arg in argv)
        or not 0 < timeout <= interval <= maximum
    ):
        raise ValueError("Invalid operator retry probe bounds or argv")
    failures = min(blocker.get("probe_failures", 0), 16)
    blocker["next_check_at"] = (
        now + timedelta(seconds=min(maximum, interval * 2**failures))
    ).isoformat()
    write_json(state_path, state)
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(blocker),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        receipt = json.loads(completed.stdout)
        if retry_policy.verified_repair(blocker, receipt, datetime.now(UTC)):
            return {
                "kind": "probe_recovery",
                "token": "probe:" + retry_policy.digest(receipt),
                "reason": receipt["reason"],
                "receipt": receipt,
            }
        blocker["probe_outcome"] = "observations_not_verified"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        blocker["probe_outcome"] = type(exc).__name__
    blocker["probe_failures"] = failures + 1
    write_json(state_path, state)
    return None


def report_waiting(config, api, task, state, path):
    """One stable report per blocker, including legacy state adopted at upgrade."""
    blocker = state["blocker"]
    report = state.setdefault(
        "waiting_report",
        {
            "key": fingerprint([blocker["binding"], blocker["attempt"], blocker["key"]])
            + ":waiting",
            "content": retry_policy.waiting_text(blocker),
        },
    )
    if report.get("delivered"):
        return
    write_json(path, state)
    report["message_id"] = api.send(task, report["key"], report["content"])
    api.tasks.update_task_fields(
        state["task_id"],
        fields={"next_checkpoint": report["content"]},
        actor_concept_id=config["agent_id"],
    )
    api.tasks.update_task_status(
        state["task_id"], "blocked", actor_concept_id=config["agent_id"]
    )
    current = api.task(state["task_id"])
    if (
        current.get("next_checkpoint") != report["content"]
        or current["status"] != "blocked"
    ):
        raise RuntimeError("Waiting checkpoint canonical read-back failed")
    report["delivered"] = True
    write_json(path, state)


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
            try:
                apply_deployment(config, api, state, path, lock_fd)
                finish_captured(config, api, state, path)
            except Exception as exc:  # noqa: BLE001 - preserve any failed report without wedging other tasks
                state["reconciliation_error"] = type(exc).__name__
                write_json(path, state)
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
        if state["phase"] in {"running", "reporting", "archive_pending"}:
            continue
        state["title"] = task["title"]
        inputs = api.inputs(task)
        # A lost controller state is not proof that earlier effects never ran.
        # Adopt canonical blocked/in-progress evidence without replaying work.
        if (state["phase"] == "new" and task["status"] != "pending") or (
            state["phase"] == "waiting" and not state.get("result")
        ):
            state.update(
                phase="waiting",
                attempt="unretained-"
                + fingerprint(
                    [
                        task_id,
                        task.get("updated_at"),
                        task.get("evidence"),
                    ]
                )[:16],
                result={
                    "status": "blocked",
                    "summary": task.get("progress_signal")
                    or "Canonical task has prior work but its local attempt state is unavailable.",
                    "evidence": task.get("evidence") or "",
                    "question": task.get("next_checkpoint")
                    or "Reconcile canonical results and retained work before an explicit retry.",
                },
            )
        if state["phase"] == "waiting":
            decision = retry_policy.admission(config, state, inputs)
            write_json(path, state)
            if decision is None:
                try:
                    decision = probe_recovery(config, state, path)
                except (KeyError, TypeError, ValueError) as exc:
                    state["blocker"]["probe_outcome"] = type(exc).__name__
            if decision is None:
                # One task's unavailable reporting must not wedge the queue.
                try:
                    report_waiting(config, api, task, state, path)
                    if config.get("supervision_enabled"):
                        api.reconcile_supervision(
                            task,
                            state,
                            lambda path=path, state=state: write_json(path, state),
                        )
                except Exception as exc:  # noqa: BLE001 - canonical reporting errors stay isolated to this task
                    state["waiting_report_error"] = type(exc).__name__
                    write_json(path, state)
                continue
            state["pending_admission"] = decision
        if (
            state["phase"] == "done"
            and task["status"] != "pending"
            and state.get("input_hash") == input_fingerprint(inputs)
        ):
            continue
        if state.get("pending_admission"):
            # Probes and evidence reads can outlast a cancellation or reassignment.
            # Re-read the supported canonical route before consuming any retry.
            current = api.task(task_id)
            if not (
                authorised_task(current, config)
                and current.get("status") in ACTIVE
                and api.native_writer(current)
            ):
                continue
            current_inputs = api.inputs(current)
            decision = state["pending_admission"]
            if decision["kind"] != "probe_recovery":
                checked = retry_policy.admission(config, state, current_inputs)
                if not checked or checked.get("token") != decision.get("token"):
                    continue
            task, inputs = current, current_inputs
        conversation = api.conversation(task)
        # The model may proceed from adequate task text when a transcript is unavailable.
        api.send(
            task,
            fingerprint([task_id, input_fingerprint(inputs)]) + ":started",
            (
                f"Continuing after {state['pending_admission']['kind']}: "
                f"{state['pending_admission'].get('reason', '')}\n"
                if state.get("pending_admission")
                else ""
            )
            + f"I am picking up this task on the DGX: {task['title']}.\nTask: {task_id}\n"
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
                state["result"]["question"] = (
                    "Correct this task's model or reasoning setting, then give an explicit retry reason in the task thread."
                )
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
