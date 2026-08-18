"""Navigation-only external-resource actions for Gmail-backed Von tasks.

The provider deliberately knows only the durable Gmail source evidence emitted
by the mail-ingestion workflow.  It does not interpret task prose, perform
Gmail mutations, or let a task-supplied profile bypass the normal actor-scoped
Gmail authority boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from src.backend.services.task_external_resource_action_service import (
    OPEN_RESOURCE_ACTION_KIND,
    TaskExternalResourceActionAccessError,
    TaskExternalResourceActionResolutionError,
)

GMAIL_OPEN_SOURCE_ACTION_ID = "gmail.open_source"
_GMAIL_SOURCE_SYSTEM = "gmail"
_MAX_SOURCE_FIELD_LENGTH = 512


def _clean_source_field(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_SOURCE_FIELD_LENGTH:
        return None
    return cleaned


def _parse_gmail_source_evidence(task: Mapping[str, Any]) -> dict[str, str] | None:
    """Read the exact, delimited Gmail source fields from a task's evidence.

    Evidence is a small legacy text field, not a general serialisation format.
    Requiring the exact key names avoids accidentally promoting prose such as a
    copied message subject into an external resource identifier.
    """

    evidence = task.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return None

    fields: dict[str, str] = {}
    for part in evidence.split(";"):
        key, separator, value = part.partition("=")
        key = key.strip()
        cleaned_value = _clean_source_field(value)
        if not separator or not key or cleaned_value is None or key in fields:
            return None
        fields[key] = cleaned_value

    if fields.get("source_system") != _GMAIL_SOURCE_SYSTEM:
        return None
    source_profile = fields.get("source_profile")
    source_item_id = fields.get("source_item_id")
    if source_profile is None or source_item_id is None:
        return None
    return {
        "source_system": _GMAIL_SOURCE_SYSTEM,
        "source_profile": source_profile,
        "source_item_id": source_item_id,
    }


def list_actions(task: Mapping[str, Any]) -> list[dict[str, str]]:
    """List the one safe action available for a Gmail-ingestion review task."""

    source = _parse_gmail_source_evidence(task)
    if source is None:
        return []
    return [
        {
            "action_id": GMAIL_OPEN_SOURCE_ACTION_ID,
            "kind": OPEN_RESOURCE_ACTION_KIND,
            "label": "Open source in Gmail",
            "source_system": _GMAIL_SOURCE_SYSTEM,
            "resource_type": "gmail_thread",
        }
    ]


def resolve_action(task: Mapping[str, Any], action_id: str) -> str | None:
    """Resolve the advertised Gmail action through trusted profile authority."""

    if action_id != GMAIL_OPEN_SOURCE_ACTION_ID:
        return None
    source = _parse_gmail_source_evidence(task)
    if source is None:
        return None

    from src.backend.integrations.google import gmail_service
    from src.backend.services.gmail_profile_invocation_authority_service import (
        GmailInvocationAuthorityError,
        authorise_gmail_profile_for_invocation,
    )

    try:
        authority = authorise_gmail_profile_for_invocation(source["source_profile"])
    except GmailInvocationAuthorityError as exc:
        raise TaskExternalResourceActionAccessError(
            reason_code=exc.reason_code,
            safe_message=exc.safe_message,
        ) from exc

    try:
        target = gmail_service.resolve_message_thread_navigation_target(
            profile_id=authority.profile_id,
            message_id=source["source_item_id"],
            audit_context={
                "namespace": authority.audit_namespace,
                "source": "task_external_resource_action",
                "action": GMAIL_OPEN_SOURCE_ACTION_ID,
            },
        )
    except Exception as exc:  # safe provider error boundary
        raise TaskExternalResourceActionResolutionError(
            reason_code="gmail_source_navigation_unavailable",
            safe_message="The Gmail source could not be opened right now.",
        ) from exc

    return (
        "https://mail.google.com/mail/?authuser="
        f"{quote(target['authorised_email'], safe='')}"
        f"#all/{quote(target['thread_id'], safe='')}"
    )


__all__ = [
    "GMAIL_OPEN_SOURCE_ACTION_ID",
    "list_actions",
    "resolve_action",
]
