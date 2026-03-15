"""Shared Jira issue-key normalisation helpers.

These helpers are reused across import, hygiene, and workflow-support code so
Jira key handling stays consistent as more workflow-native reconciliation logic
is added.
"""

from __future__ import annotations

import re
from typing import Any

_ISSUE_KEY_NUMBER_RE = re.compile(r"^[A-Z][A-Z0-9_]*-(\d+)$")


def normalise_jira_issue_key(value: Any) -> str | None:
    """Return a trimmed upper-case Jira issue key, or ``None``."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def extract_jira_issue_number(issue_key: Any) -> int | None:
    """Return the numeric suffix from a Jira issue key, if present."""

    cleaned = normalise_jira_issue_key(issue_key)
    if not cleaned:
        return None
    match = _ISSUE_KEY_NUMBER_RE.match(cleaned)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def normalise_jira_issue_key_set(value: Any) -> set[str]:
    """Return a set of normalised Jira issue keys from a list-like value."""

    if not isinstance(value, list):
        return set()
    keys: set[str] = set()
    for item in value:
        cleaned = normalise_jira_issue_key(item)
        if cleaned:
            keys.add(cleaned)
    return keys


__all__ = [
    "extract_jira_issue_number",
    "normalise_jira_issue_key",
    "normalise_jira_issue_key_set",
]
