#!/usr/bin/env python3
"""Reuse completed scheduled evidence and pace backend drift notifications.

State is an Actions artefact, not the accepted-failure backlog. Missing or
unreadable state causes a fresh run. Manual runs always execute; diagnostic
slices and runs off the default branch never publish shared schedule state.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE_NAME = "backend-drift-state"
STATE_FILE = "backend-drift-state.json"


def restore_state(repo: str, branch: str) -> dict:
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/artifacts?name={STATE_NAME}&per_page=100",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        artifacts = json.loads(result.stdout)["artifacts"]
        for artifact in artifacts:
            if (
                artifact["expired"]
                or artifact.get("workflow_run", {}).get("head_branch") != branch
            ):
                continue
            archive = subprocess.run(
                ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"],
                check=True,
                capture_output=True,
                timeout=60,
            )
            with zipfile.ZipFile(io.BytesIO(archive.stdout)) as zipped:
                state = json.loads(zipped.read(STATE_FILE))
            if state.get("schema_version") == "backend_drift_schedule.v1":
                return state
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
    ):
        print("Previous schedule state unavailable; running the suite.")
    return {}


def complete(report: dict) -> bool:
    return (
        report.get("full_run") is True
        and report.get("status") in {"new_failures", "no_new_failures"}
        and report.get("pytest_exit_code") in {0, 1}
        and isinstance(report.get("observed_failures"), list)
    )


def should_run(event: str, sha: str, state: dict) -> bool:
    return not (
        event == "schedule"
        and state.get("tested_sha") == sha
        and complete(state.get("report", {}))
    )


def notification(report: dict, state: dict, now: datetime) -> str:
    if not complete(report):
        return "broken"
    previous = state.get("notified_report", {})
    # Compare with the last successfully sent report: a failed send must retry.
    if not complete(previous):
        return "changed" if report.get("new_failures") else ""
    if any(
        set(report.get(key, [])) != set(previous.get(key, []))
        for key in ("observed_failures", "new_failures")
    ):
        return "changed"
    if report.get("new_failures"):
        try:
            last = datetime.fromisoformat(state["last_notified_at"])
            if now - last < timedelta(days=7):
                return ""
        except (KeyError, ValueError, TypeError):
            pass
        return "reminder"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["plan", "finish"])
    parser.add_argument("--directory", default="/tmp")
    args = parser.parse_args()
    directory = Path(args.directory)
    state_path = directory / STATE_FILE
    if args.phase == "plan":
        state = restore_state(
            os.environ["GITHUB_REPOSITORY"], os.environ["DEFAULT_BRANCH"]
        )
        run = should_run(
            os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_SHA"], state
        )
        state_path.write_text(json.dumps(state))
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"run_suite={str(run).lower()}\n")
        print(
            "Run full suite."
            if run
            else "Unchanged commit; reusing completed full-suite evidence."
        )
        return 0

    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    report_path = directory / "backend-test-drift.json"
    ran = os.environ.get("RAN_SUITE") == "true"
    if ran:
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError):
            report = {
                "status": "run_broken",
                "output_tail": "The suite did not produce a readable report. Inspect the run logs.",
            }
    else:
        report = state.get("report", {})
    now = datetime.now(UTC)
    kind = notification(report, state, now)
    current_url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if ran and complete(report):
        state.update(
            tested_sha=os.environ["GITHUB_SHA"],
            report=report,
            report_run_url=current_url,
        )
    if ran and not complete(report):
        state.pop("tested_sha", None)
    state["schema_version"] = "backend_drift_schedule.v1"
    summary = (
        f"{'Executed' if ran else 'Reused'} full-suite evidence: {current_url if ran else state.get('report_run_url', current_url)}\n"
        f"Notification: {kind or 'unchanged; suppressed'}\n"
    )
    if kind:
        body = directory / "drift-notification.txt"
        body.write_text(
            summary
            + "\nFailures outside the accepted backlog: "
            + str(len(report.get("new_failures", [])))
            + "\n"
            + "\n".join(report.get("new_failures", []))
            + "\n\n"
            + report.get("output_tail", "")
        )
        receipt = directory / "drift-notification-sent"
        receipt.unlink(missing_ok=True)
        env = dict(os.environ, NIGHTLY_NOTIFICATION_RECEIPT=str(receipt))
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("notify_backend_test_drift.py")),
                kind,
                str(body),
            ],
            env=env,
            check=False,
        )
        if receipt.exists():
            state.update(last_notified_at=now.isoformat(), notified_report=report)
        else:
            summary += "Notification not delivered; next run will retry.\n"
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
        output.write("\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
