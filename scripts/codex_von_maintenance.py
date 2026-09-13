"""Publish bounded public maintenance data through an operator-owned static root.

No HTTP write endpoint, task text, credentials, health payloads or inferred ETA.
The host operator must bind this root to an independent read-only status route.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def publish(config, receipt, state, *, now=None):
    root_value = config.get("public_status_root")
    if not root_value:
        return {"status": "not_configured"}
    root = Path(root_value)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Public status root must be an existing absolute directory")
    agent = config.get("maintenance_agent")
    reason = config.get("maintenance_public_reason", "Von is being updated")
    if not isinstance(agent, str) or not agent.strip() or len(agent) > 100:
        raise ValueError("An operator-bound public maintenance agent is required")
    if not isinstance(reason, str) or len(reason) > 280:
        raise ValueError("Public maintenance reason must be at most 280 characters")
    if state not in {"planned", "recovering", "failed", "ready", "cancelled"}:
        raise ValueError("Invalid maintenance state")
    now = now or datetime.now(timezone.utc)
    start = receipt.setdefault("maintenance_planned_start", now.isoformat())
    # This estimate is explicitly supplied by the operator from the release plan.
    # Never reuse the health timeout as an estimated return time.
    seconds = config.get("maintenance_estimate_seconds")
    if seconds is not None and (type(seconds) not in (int, float) or not 0 < seconds <= 86400):
        raise ValueError("Maintenance estimate must be positive and at most one day")
    ready_at = None
    if seconds is not None:
        ready_at = (datetime.fromisoformat(start) + timedelta(seconds=seconds)).isoformat()
    record = {
        "schema_version": "von_maintenance.v1",
        "release_id": receipt["requested_commit"],
        "agent": agent.strip(),
        "reason": reason,
        "state": state,
        "planned_start": start,
        "estimated_ready_at": ready_at if state in {"planned", "recovering"} else None,
        "updated_at": now.isoformat(),
        # Freshness is not an ETA. A crashed publisher leaves an explicitly stale record.
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
    }
    target = root / "maintenance.json"
    fd, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if json.loads(target.read_text()) != record:
        raise OSError("Public maintenance file read-back mismatch")
    return {"status": "published", "record": record}


def record_publication(config, receipt, state):
    """A status write failure must not roll back an otherwise healthy release."""
    try:
        receipt["maintenance_publication"] = publish(config, receipt, state)
    except (OSError, ValueError) as exc:
        receipt["maintenance_publication"] = {
            "status": "failed", "failure_type": type(exc).__name__
        }
