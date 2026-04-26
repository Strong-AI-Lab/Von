"""Repo seed-bundle version-bump guard.

JVNAUTOSCI-2132 L0 deliverable. Pure helper that, given a set of changed
seed-bundle JSON paths and a callable that resolves the baseline bundle
text on the comparison ref, returns a list of human-readable issue
strings for any workflow seed bundle whose content has changed without a
strictly increasing ``seed_version``.

The helper is import-safe and has no IO side effects of its own; the
baseline resolver is supplied by the caller (the pytest test passes a
literal mapping; the CLI passes a function backed by ``git show``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

WORKFLOW_BUNDLE_SCHEMA = "repo_seed_workflow_bundle.v1"

BaselineResolver = Callable[[Path], str | None]
"""Return the baseline text of ``path`` on the comparison ref, or ``None``
if the file did not exist on that ref (i.e. a brand-new bundle)."""


def _parse_seed_version(raw: object) -> tuple[int, str] | None:
    """Return ``(int_value, raw_str)`` if the seed version parses as an int.

    Returns ``None`` when the value is missing or not parseable. The raw
    string is preserved for diagnostic messages.
    """

    if raw is None:
        return None
    raw_str = str(raw).strip()
    if not raw_str:
        return None
    try:
        return int(raw_str), raw_str
    except (TypeError, ValueError):
        return None


def _is_workflow_bundle(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == WORKFLOW_BUNDLE_SCHEMA
    )


def check_seed_version_bumps(
    changed_bundle_paths: Iterable[Path],
    *,
    repo_root: Path,
    baseline_resolver: BaselineResolver,
) -> list[str]:
    """Return a list of issue strings for changed workflow seed bundles
    whose ``seed_version`` was not strictly bumped relative to the
    baseline.

    Rules:
    - Non-existent or non-workflow bundles are silently skipped.
    - Brand-new bundles (no baseline) are accepted unconditionally.
    - If the on-disk content equals the baseline content byte-for-byte,
      no bump is required.
    - Otherwise the on-disk ``seed_version`` must parse as an integer
      strictly greater than the baseline ``seed_version``.
    - Removing or unsetting ``seed_version`` on a changed bundle is an
      issue.
    - A baseline that did not parse as an integer is treated as
      "missing"; in that case the new content must still parse as an
      integer (any value), which forces an explicit, machine-readable
      version on first guarded change.
    """

    issues: list[str] = []
    for path in changed_bundle_paths:
        if not path.exists():
            # Deleted bundles are out of scope for the bump guard.
            continue
        try:
            current_text = path.read_text(encoding="utf-8")
            current_payload = json.loads(current_text)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"{_display(path, repo_root)}: cannot parse current bundle ({exc})")
            continue
        if not _is_workflow_bundle(current_payload):
            continue

        baseline_text = baseline_resolver(path)
        if baseline_text is None:
            # New file — no bump required.
            continue
        if baseline_text == current_text:
            continue

        try:
            baseline_payload = json.loads(baseline_text)
        except json.JSONDecodeError:
            baseline_payload = None

        baseline_version = (
            _parse_seed_version(baseline_payload.get("seed_version"))
            if isinstance(baseline_payload, dict)
            else None
        )
        current_version = _parse_seed_version(current_payload.get("seed_version"))

        if current_version is None:
            issues.append(
                f"{_display(path, repo_root)}: workflow seed bundle changed but "
                "seed_version is missing or not an integer"
            )
            continue
        if baseline_version is None:
            # Baseline lacked a parseable seed_version; any integer is
            # acceptable on first guarded change.
            continue
        if current_version[0] <= baseline_version[0]:
            issues.append(
                f"{_display(path, repo_root)}: workflow seed bundle changed but "
                f"seed_version did not increase "
                f"(baseline={baseline_version[1]}, current={current_version[1]})"
            )
    return issues


def _display(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)
