"""Diagnostic projection helpers for durable workflow claims."""

from __future__ import annotations

from typing import Any


def _serialise_datetime(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def build_workflow_instance_claim_diagnostics(instance: Any) -> dict[str, Any]:
    """Return user-safe claim provenance fields for diagnostic payloads."""
    return {
        "workflow_instance_locked_by": getattr(instance, "locked_by", None),
        "workflow_instance_claimed_at": _serialise_datetime(
            getattr(instance, "claimed_at", None)
        ),
        "workflow_instance_claimed_by_build": getattr(
            instance,
            "claimed_by_build",
            None,
        ),
    }
