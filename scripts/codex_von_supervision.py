"""Controller-owned supervisor handoffs, using canonical tasks and retry receipts.

The config is operator-owned. A reporting relationship never grants a coding
identity permission to impersonate its supervisor. The controller creates a
bounded repair on behalf of its existing delegator, retaining the source chain.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

try:
    from . import codex_von_retry as retry
except ImportError:
    import codex_von_retry as retry


def resolution(task):
    from src.backend.services.task_reporting_service import resolve_task_reporting

    return resolve_task_reporting(task)


def report_result(config, api, task, key, content):
    """Notify the current canonical report-to without altering old audiences.

    Freeze the intent under the caller's controller lock before delivery. A
    lost acknowledgement retries that exact audience/content, not a new route.
    Membership and task scope are checked again before every attempted send.
    """
    from scripts.codex_von_worker import write_json

    path = (
        Path(config["state_root"])
        / "supervisor-reports"
        / (retry.digest(key) + ".json")
    )
    current = api.task(task["task_concept_id"])
    if (
        current.get("assignee_concept_id") != config["agent_id"]
        or current.get("organisation_concept_id") != config["organisation_id"]
    ):
        return None
    if path.exists():
        intent = json.loads(path.read_text())
        if intent.get("message_id"):
            return intent["message_id"]
    else:
        route = resolution(current)
        recipient = route.get("concept_id")
        if route.get("status") != "resolved" or recipient in {
            None,
            config["agent_id"],
            config["delegator_id"],
        }:
            return None
        intent = {
            "recipient": recipient,
            "content": content,
            "task_id": current["task_concept_id"],
            "subject": "Coding task report: " + current["title"],
        }
        write_json(path, intent)
    route = resolution(current)
    if (
        route.get("status") != "resolved"
        or route.get("concept_id") != intent["recipient"]
    ):
        # Preserve the old intent for diagnosis, but revoked reporting scope
        # must not create a new message to the former supervisor on recovery.
        return None
    allowed, _ = api.messages.authorise_direct_message_participants(
        sender_id=config["agent_id"],
        recipient_ids=[intent["recipient"]],
        organisation_concept_id=config["organisation_id"],
    )
    if not allowed:
        raise PermissionError("Report recipient is no longer an organisation member")
    receipt = api.messages.create_message_idempotently(
        delivery_idempotency_key=key + ":supervisor",
        sender_id=config["agent_id"],
        recipient_ids=[intent["recipient"]],
        organisation_concept_id=config["organisation_id"],
        thread_id=intent["task_id"],
        subject=intent["subject"],
        content=intent["content"],
        metadata={"attribution": "Sent by " + config["agent_id"]},
    )
    mid = receipt.message["concept_id"]
    row = api.messages.get_message_for_user(mid, config["agent_id"])
    rb = api.messages.project_direct_message(row) if row else {}
    if (
        rb.get("content") != intent["content"]
        or rb.get("sender_id") != config["agent_id"]
        or rb.get("recipient_ids") != [intent["recipient"]]
        or rb.get("organisation_concept_id") != config["organisation_id"]
        or rb.get("thread_id") != intent["task_id"]
    ):
        raise RuntimeError("Supervisor report canonical read-back failed")
    write_json(path, {**intent, "message_id": mid})
    return mid


def is_task_report(config, api, message):
    """A subordinate's canonical task report is evidence, never an instruction."""
    if (
        not config.get("supervision_enabled")
        or config["agent_id"] not in message.get("recipient_ids", [])
        or message.get("organisation_concept_id") != config["organisation_id"]
        or not message.get("thread_id")
    ):
        return False
    try:
        task = api.task(message["thread_id"])
    except api.tasks.TaskNotFoundError:
        return False
    route = resolution(task)
    return (
        task.get("organisation_concept_id") == config["organisation_id"]
        and task.get("assignee_concept_id") == message.get("sender_id")
        and message.get("sender_id") != config["agent_id"]
        and route.get("status") == "resolved"
        and route.get("concept_id") == config["agent_id"]
    )


def reconcile_completed_report(config, api, selection):
    """Explicit operator recovery of one retained result; never rerun its task."""
    from scripts.codex_von_worker import fingerprint

    task_id, attempt = selection.rsplit("=", 1)
    path = Path(config["state_root"]) / "tasks" / (fingerprint(task_id)[:16] + ".json")
    state = json.loads(path.read_text())
    task = api.task(task_id)
    if (
        not config.get("supervision_enabled")
        or state.get("task_id") != task_id
        or state.get("attempt") != attempt
        or state.get("phase") != "done"
        or task.get("status") != "completed"
        or task.get("assignee_concept_id") != config["agent_id"]
        or task.get("organisation_concept_id") != config["organisation_id"]
    ):
        raise PermissionError("No matching retained completed report")
    row = api.messages.get_message_for_user(state["message_id"], config["agent_id"])
    rb = api.messages.project_direct_message(row) if row else {}
    if (
        rb.get("sender_id") != config["agent_id"]
        or rb.get("organisation_concept_id") != config["organisation_id"]
        or rb.get("thread_id") != task_id
        or rb.get("content") != state.get("report_content")
        or config["delegator_id"] not in rb.get("recipient_ids", [])
    ):
        raise PermissionError("Original result canonical read-back failed")
    mid = report_result(config, api, task, attempt + ":result", rb["content"])
    return {
        "task_id": task_id,
        "attempt": attempt,
        "message_id": mid,
        "coding_replayed": False,
        "original_message_id": state["message_id"],
    }


def source_authorised(config, task):
    return (
        task.get("assignee_concept_id") == config["agent_id"]
        and task.get("created_by_concept_id") == config["delegator_id"]
        and task.get("organisation_concept_id") == config["organisation_id"]
        and task.get("status") in {"pending", "in_progress", "blocked"}
    )


def reconcile(config, api, task, state, persist):
    """Called under the existing controller lock. Persist intents before effects."""
    if not config.get("supervision_enabled"):
        return None
    task = api.task(state["task_id"])
    if not source_authorised(config, task) or not api.native_writer(task):
        return None
    blocker = retry.retain_blocker(config, state)
    if not blocker:
        return None
    # Unclassified legacy failures need retained-evidence review, not invented
    # observations. Transient failures keep their existing bounded probe first.
    route = resolution(task)
    parent = (task.get("external_references") or {}).get("coding_supervision") or {}
    chain = list(parent.get("agent_chain") or []) + [config["agent_id"]]
    supervisor = route.get("concept_id")
    if supervisor in chain:
        route = dict(route, status="supervision_cycle")
    if route["status"] != "resolved":
        content = (
            f"Supervisor handoff unavailable: {route['status']} ({route['source']}). "
            f"Configure one accessible reporting recipient for {task['task_concept_id']} "
            "or its assignee. The source remains waiting; no coding retry was launched."
        )
        error_key = retry.digest([state["task_id"], blocker["attempt"], route])
        if state.get("supervision_error", {}).get("key") != error_key:
            receipt = api.send(task, error_key + ":supervisor-error", content)
            state["supervision_error"] = {
                "key": error_key,
                "message_id": receipt,
                "route": route,
            }
            persist()
        return None
    allowed, _ = api.messages.authorise_direct_message_participants(
        sender_id=config["agent_id"],
        recipient_ids=[supervisor],
        organisation_concept_id=config["organisation_id"],
    )
    if not allowed:
        raise PermissionError("Supervisor is not an accessible organisation member")
    episode = state.get("supervision")
    signature = retry.digest(
        [
            blocker["key"],
            blocker["scope"],
            supervisor,
            task.get("title"),
            task.get("description"),
        ]
    )
    if not episode or episode.get("resolved") or episode.get("signature") != signature:
        episode = {
            "signature": signature,
            "key": retry.digest([blocker["binding"], blocker["attempt"], signature]),
            "failed_attempt": blocker["attempt"],
            "route": route,
        }
        state["supervision"] = episode
        persist()
    repair = api.tasks.find_task_by_agent_creation_fingerprint(
        created_by_concept_id=config["delegator_id"],
        organisation_concept_id=config["organisation_id"],
        creation_fingerprint=episode["key"],
    )
    if repair is None:
        # Only selected task data is copied, never the inbox or unrelated chats.
        provenance = {
            "source_task_id": state["task_id"],
            "source_agent": config["agent_id"],
            "delegator_id": config["delegator_id"],
            "organisation_id": config["organisation_id"],
            "episode_key": episode["key"],
            "agent_chain": chain,
            "blocker": blocker,
            "source_instructions": {
                k: task.get(k)
                for k in (
                    "title",
                    "description",
                    "requested_model",
                    "requested_reasoning_effort",
                )
            },
        }
        context = {
            "provenance": provenance,
            "result": state.get("result"),
            "worktree": state.get("worktree"),
            "capture": state.get("capture"),
            "execution_settings": state.get("execution_settings"),
            "task_evidence": task.get("evidence"),
            "source_comments": api.inputs(task).get("comments", []),
            "source_notes": task.get("notes"),
            "task_links": task.get("task_links"),
        }
        description = (
            "Review the retained blocked work, repair the external prerequisite within the original "
            "delegation, and verify the repaired condition. Do not repeat the failed coding run. "
            "If blocked yourself, describe the concrete missing prerequisite for your supervisor.\n\n"
            "The controller created this repair on behalf of the source delegator under their "
            "standing supervisor-routing instruction. This does not grant credentials, unrelated "
            "conversation access, or broader deployment authority.\n\n"
            "For a classified blocker, return repair_receipt_json containing the original attempt, "
            "blocker_key, binding, scope, observed_at (after repair), observations matching each "
            "required_observations value, reason and evidence_reference. Observe the actual repaired "
            "condition before attesting; completed status or passing unrelated CI is not evidence. "
            "For an unclassified legacy failure, retain the diagnosis and ask the delegator for a "
            "reviewed attempt-bound continuation; do not invent required observations.\n\n"
            + json.dumps(context, indent=2, default=str)
        )
        repair = api.tasks.create_task(
            "Unblock: " + task["title"],
            description,
            assignee_concept_id=supervisor,
            created_by_concept_id=config["delegator_id"],
            organisation_concept_id=config["organisation_id"],
            priority=task.get("priority") or "medium",
            report_to_concept_id=None,
            requested_model=task.get("requested_model")
            or config.get("model", "gpt-6-astra"),
            requested_reasoning_effort=task.get("requested_reasoning_effort") or "high",
            agent_creation_fingerprint=episode["key"],
            agent_creation_request_id=episode["key"],
            initial_external_references={"coding_supervision": provenance},
        )
    repair = api.task(repair["task_concept_id"])
    reference = (repair.get("external_references") or {}).get(
        "coding_supervision"
    ) or {}
    if (
        repair.get("created_by_concept_id") != config["delegator_id"]
        or repair.get("organisation_concept_id") != config["organisation_id"]
        or reference.get("episode_key") != episode["key"]
    ):
        raise PermissionError(
            "Repair task provenance does not match the retained episode"
        )
    if repair.get("assignee_concept_id") != supervisor or repair.get("status") not in {
        "pending",
        "in_progress",
        "blocked",
        "completed",
    }:
        raise PermissionError(
            "Supervisor repair was cancelled or reassigned; reconcile the existing task"
        )
    if reference["blocker"]["attempt"] != blocker["attempt"]:
        repair = api.tasks.refresh_coding_supervision_context(
            repair["task_concept_id"],
            episode_key=episode["key"],
            blocker=blocker,
            actor_concept_id=config["agent_id"],
        )
    episode["task_id"] = repair["task_concept_id"]
    persist()
    current = api.task(state["task_id"])
    if not source_authorised(config, current) or resolution(current) != route:
        return None
    if not episode.get("linked"):
        api.tasks.link_tasks(
            repair["task_concept_id"],
            state["task_id"],
            link_type="blocks",
            actor_concept_id=config["agent_id"],
        )
        current = api.task(state["task_id"])
        if not any(
            link.get("target_task_concept_id") == repair["task_concept_id"]
            for link in current.get("task_links", [])
        ):
            raise RuntimeError("Source/repair link read-back failed")
        episode["linked"] = True
        persist()
    content = (
        f"Blocked task: {state['task_id']}\nSupervisor: {supervisor} ({route['source']})\n"
        f"Repair task: {repair['task_concept_id']}\n{blocker['reason']}\n"
        "The source is waiting for verified recovery, without another coding launch."
    )
    if not episode.get("message_id"):
        # Notify Michael once. The assigned supervisor discovers the same canonical
        # repair via its existing task queue; human supervisors get no coding run.
        episode["message_id"] = api.send(task, episode["key"] + ":handoff", content)
        persist()
    if not episode.get("checkpoint"):
        api.tasks.update_task_fields(
            state["task_id"],
            fields={"next_checkpoint": content},
            actor_concept_id=config["agent_id"],
        )
        if api.task(state["task_id"]).get("next_checkpoint") != content:
            raise RuntimeError("Supervisor checkpoint read-back failed")
        episode["checkpoint"] = True
        persist()
    return episode


def record_repair(config, api, repair_task, result):
    """The finishing controller attests the supervisor's exact observations.

    This is the existing trusted-producer receipt boundary, not a parser for
    arbitrary comments or a completion-status inference.
    """
    raw = result.get("repair_receipt_json")
    if (
        not config.get("supervision_enabled")
        or not raw
        or result.get("status") != "completed"
    ):
        return None
    parent = (repair_task.get("external_references") or {}).get(
        "coding_supervision"
    ) or {}
    if not parent or not source_authorised(config, repair_task):
        raise PermissionError("No current delegated supervisor repair")
    source = api.task(parent["source_task_id"])
    if (
        source.get("status") not in {"blocked", "in_progress", "pending"}
        or source.get("assignee_concept_id") != parent["source_agent"]
        or source.get("created_by_concept_id") != config["delegator_id"]
        or source.get("organisation_concept_id") != config["organisation_id"]
        or resolution(source).get("concept_id") != config["agent_id"]
        or any(source.get(k) != v for k, v in parent["source_instructions"].items())
    ):
        raise PermissionError("Original task authority or instructions changed")
    receipt = json.loads(raw)
    if not retry.verified_repair(parent["blocker"], receipt, datetime.now(UTC)):
        raise ValueError("Supervisor did not supply matching fresh repair observations")
    receipt = dict(
        receipt,
        supervisor_task_id=repair_task["task_concept_id"],
        episode_key=parent["episode_key"],
    )
    # Reconcile a lost comment acknowledgement before creating another comment.
    comments = api.tasks.list_task_comments(source["task_concept_id"], limit=500)[
        "comments"
    ]
    for row in comments:
        if (
            row.get("author_concept_id") == config["agent_id"]
            and (row.get("source") or {}).get("coding_supervisor_repair") == receipt
        ):
            return row
    return api.tasks.add_task_comment(
        source["task_concept_id"],
        body="Supervisor verified recovery: " + receipt["reason"],
        author_concept_id=config["agent_id"],
        source={"coding_supervisor_repair": receipt},
    )


def repair_comments(config, api, task, rows):
    """Accept only current, linked supervisor attestations; never bare metadata."""
    if not config.get("supervision_enabled"):
        return []
    supervisor = resolution(task).get("concept_id")
    accepted = []
    for row in rows:
        receipt = (row.get("source") or {}).get("coding_supervisor_repair")
        if row.get("author_concept_id") != supervisor or not isinstance(receipt, dict):
            continue
        try:
            repair = api.task(receipt["supervisor_task_id"])
            parent = (repair.get("external_references") or {}).get(
                "coding_supervision"
            ) or {}
            if (
                repair.get("created_by_concept_id") != config["delegator_id"]
                or repair.get("assignee_concept_id") != supervisor
                or repair.get("organisation_concept_id") != config["organisation_id"]
                or parent.get("source_task_id") != task["task_concept_id"]
                or parent.get("episode_key") != receipt.get("episode_key")
                or any(
                    task.get(k) != v
                    for k, v in parent.get("source_instructions", {}).items()
                )
                or not any(
                    link.get("target_task_concept_id") == repair["task_concept_id"]
                    for link in task.get("task_links", [])
                )
            ):
                continue
            accepted.append(row)
        except (KeyError, api.tasks.TaskNotFoundError):
            continue
    return accepted
