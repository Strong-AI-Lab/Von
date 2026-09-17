"""Single controller-owned Codex transport; never attaches to another process.

The controller supplies already authorised, durably reserved steering inputs.
Acknowledgement means accepted input, not observed model consumption. A lost
acknowledgement is uncertain and is never automatically retried or queued.
This transport is opt-in; it does not enable public message admission.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque


class ProtocolError(RuntimeError):
    pass


def run_turn(
    *,
    command,
    cwd,
    model,
    effort,
    prompt,
    schema,
    environment,
    lock_fd,
    events,
    errors,
    permission_profile=None,
    checkpoint=lambda: None,
    on_started=lambda: None,
    on_active=lambda thread, turn: None,
    steering=lambda: (),
    on_delivery=lambda message, status: None,
    interval=15,
):
    """Run one owned turn, retaining the inherited worker lock until exit.

    ``steering`` yields {message_id, thread_id, turn_id, input}. It must reserve
    each source before yielding, and never yield a reserved source again after a
    restart. Thread/turn checks here complement the owner's task/actor checks.
    There is no whole-turn deadline: interval only schedules observation.
    """
    if interval <= 0:
        raise ValueError("Observation interval must be positive")
    if "sol" in model.lower():
        raise ValueError("Sol-family models are disabled")
    args = [
        command,
        "app-server",
        "--strict-config",
        "--listen",
        "stdio://",
        "-c",
        "mcp_servers.von.enabled=false",
        "-c",
        'approval_policy="never"',
        "-c",
        "model_reasoning_effort=" + json.dumps(effort),
    ]
    if permission_profile:
        args += ["-c", "default_permissions=" + json.dumps(permission_profile)]
    else:
        args += ["-c", 'sandbox_mode="workspace-write"']
    incoming = queue.Queue()
    pending = {}
    deferred = deque()
    sequence = 0
    thread_id = turn_id = None
    final_text = None
    terminal = None
    next_observation = time.monotonic() + interval

    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errors,
        text=True,
        env=environment,
        cwd=cwd,
        pass_fds=(lock_fd,),
    ) as process:

        def read_output():
            try:
                for line in process.stdout:
                    incoming.put(line)
            finally:
                incoming.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def write(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def request(method, params, message=None):
            nonlocal sequence
            sequence += 1
            # Record before writing; even a partial write has an uncertain effect.
            pending[sequence] = (method, message)
            write({"id": sequence, "method": method, "params": params})

        def record(value):
            events.write(json.dumps(value) + "\n")
            events.flush()

        try:
            on_started()
            request(
                "initialize",
                {
                    "clientInfo": {"name": "von_dgx_worker", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            while terminal is None:
                # Schedule from time, not empty reads: continuous tool output
                # must not starve input delivery or archive checkpoints.
                if time.monotonic() >= next_observation and not (turn_id and deferred):
                    checkpoint()
                    if turn_id:
                        for message in steering():
                            if (message["thread_id"], message["turn_id"]) != (
                                thread_id,
                                turn_id,
                            ):
                                on_delivery(message, "not_applied")
                                continue
                            request(
                                "turn/steer",
                                {
                                    "threadId": thread_id,
                                    "expectedTurnId": turn_id,
                                    "input": message["input"],
                                },
                                message,
                            )
                    next_observation = time.monotonic() + interval
                try:
                    line = (
                        deferred.popleft()
                        if turn_id and deferred
                        else incoming.get(
                            timeout=max(0.001, next_observation - time.monotonic())
                        )
                    )
                except queue.Empty:
                    continue
                if line is None:
                    raise ProtocolError(
                        "Owned app-server exited before turn completion"
                    )
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ProtocolError("Invalid app-server envelope")
                if "id" in value and "method" not in value:
                    operation = pending.pop(value["id"], None)
                    if operation is None:
                        raise ProtocolError("Unrecognised app-server response")
                    method, message = operation
                    result = value.get("result", {})
                    if method == "turn/steer":
                        status = "uncertain"
                        if value.get("error", {}).get("code") in {-32600, -32601, -32602}:
                            status = "not_applied"
                        elif "error" not in value and result.get("turnId") == turn_id:
                            status = "accepted"
                        record(
                            {
                                "type": "steering.delivery",
                                "message_id": message["message_id"],
                                "thread_id": thread_id,
                                "turn_id": turn_id,
                                "status": status,
                            }
                        )
                        on_delivery(message, status)
                        continue
                    if "error" in value:
                        raise ProtocolError(f"App-server rejected {method}")
                    if method == "initialize":
                        write({"method": "initialized"})
                        params = {
                            "cwd": str(cwd),
                            "model": model,
                            "approvalPolicy": "never",
                        }
                        if not permission_profile:
                            params["sandbox"] = "workspace-write"
                        request("thread/start", params)
                    elif method == "thread/start":
                        thread_id = result["thread"]["id"]
                        record({"type": "thread.started", "thread_id": thread_id})
                        request(
                            "turn/start",
                            {
                                "threadId": thread_id,
                                "input": [{"type": "text", "text": prompt}],
                                "model": model,
                                "effort": effort,
                                "outputSchema": schema,
                            },
                        )
                    elif method == "turn/start":
                        turn_id = result["turn"]["id"]
                        record({"type": "turn.started", "turn_id": turn_id})
                        on_active(thread_id, turn_id)
                    continue
                if "id" in value:
                    # No approval/tool authority is granted by an RPC request.
                    write(
                        {
                            "id": value["id"],
                            "error": {
                                "code": -32601,
                                "message": "Interactive server requests are unsupported by this controller",
                            },
                        }
                    )
                    continue
                method = value.get("method")
                params = value.get("params", {})
                if (
                    turn_id is None
                    and params.get("threadId") == thread_id
                    and method in {"item/completed", "turn/completed"}
                ):
                    deferred.append(line)
                    continue
                # Retain raw notifications alongside the archive-compatible rows.
                record({"type": "app_server.notification", "notification": value})
                if params.get("threadId") != thread_id:
                    continue
                if method == "item/completed" and params.get("turnId") == turn_id:
                    item = params.get("item", {})
                    if (
                        item.get("type") == "agentMessage"
                        and item.get("phase") != "commentary"
                    ):
                        final_text = item.get("text")
                elif method == "turn/completed":
                    turn = params.get("turn", {})
                    if turn.get("id") == turn_id:
                        terminal = turn.get("status", "failed")
                        record(
                            {
                                "type": (
                                    "turn.completed"
                                    if terminal == "completed"
                                    else "turn.failed"
                                ),
                                "turn_id": turn_id,
                                "status": terminal,
                            }
                        )
            if terminal != "completed" or not final_text:
                raise ProtocolError(
                    f"Owned turn ended without a final result: {terminal}"
                )
            return final_text
        finally:
            # On controlled shutdown, close the owned process even when a local
            # receipt/checkpoint fails rather than leave unobserved work running.
            try:
                process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Only the already-terminated child is killed; this is a
                    # shutdown bound, never a deadline for authorised coding.
                    process.kill()
                    process.wait()
            reader.join(timeout=1)
            on_active(None, None)
            for method, message in pending.values():
                if method == "turn/steer":
                    on_delivery(message, "uncertain")
