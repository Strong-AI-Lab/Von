#!/usr/bin/env python3
"""Explicit local operator entry point; no credentials are accepted as arguments."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["authorise", "collect", "daily", "work", "status", "install-schedule"],
    )
    parser.add_argument("--resource-id", default="personal_otter_archive")
    parser.add_argument("--meeting", action="append", default=[])
    parser.add_argument("--after")
    parser.add_argument("--before")
    parser.add_argument("--bindings-file", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    from src.backend.integrations.internal_mcp.otter_archive_proxy_mcp import (
        _build_otter_archive_config,
    )
    from src.backend.services import otter_collection_service as service

    if args.action == "authorise":
        from src.backend.integrations.otter_live_client import authorise

        archive = _build_otter_archive_config(resource_id=args.resource_id)
        result = asyncio.run(authorise(archive.database.parent / "credentials"))
    elif args.action in {"collect", "daily"}:
        result = service.enqueue(
            args.resource_id,
            meeting_ids=args.meeting,
            created_after=args.after,
            created_before=args.before,
            participant_bindings=json.loads(args.bindings_file.read_text())
            if args.bindings_file
            else None,
            daily=args.action == "daily",
            spawn=False,
        )
        print(json.dumps(result), flush=True)
        service.work(args.resource_id)
        result = service.status(args.resource_id, result["run_id"])
    elif args.action == "install-schedule":
        result = install_schedule(args.resource_id)
    elif args.action == "work":
        result = service.work(args.resource_id)
    else:
        result = service.status(args.resource_id, args.run_id)
    print(json.dumps(result), flush=True)
    if args.action in {"collect", "daily"} and any(
        r["status"] in {"failed", "partial"} for r in result.get("runs", [])
    ):
        return 1
    return 0


def install_schedule(resource_id):
    """Install an operator-requested per-user macOS job against this checkout."""
    import os
    import plistlib
    import subprocess

    from src.backend.integrations.otter_live_client import private_json
    from src.backend.services.otter_collection_service import config

    if sys.platform != "darwin":
        raise RuntimeError("Use the documented cron entry on non-macOS hosts")
    _, root, settings = config(resource_id)
    label = "org.von.otter-collection"
    plist_path = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = root / "schedule.log"
    log_path.touch(mode=0o600, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            str(ROOT / "scripts/collect_otter.py"),
            "daily",
            "--resource-id",
            resource_id,
        ],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": 6, "Minute": 0},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }
    domain = "gui/" + str(os.getuid())
    present = subprocess.run(
        ["launchctl", "print", domain + "/" + label], capture_output=True, check=False
    )
    if present.returncode == 0:
        subprocess.run(
            ["launchctl", "bootout", domain + "/" + label],
            check=True,
            capture_output=True,
        )
    plist_path.write_bytes(plistlib.dumps(payload))
    plist_path.chmod(0o600)
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "print", domain + "/" + label], check=True, capture_output=True
    )
    settings["schedule"] = {
        "enabled": True,
        "scheduler": "launchd",
        "label": label,
        "time": "06:00",
        "timezone": "host local timezone",
        "checkout": str(ROOT),
        "requires": "logged-in macOS host; execution resumes after sleep",
    }
    private_json(root / "settings.json", settings)
    return {"success": True, "schedule": settings["schedule"]}


if __name__ == "__main__":
    raise SystemExit(main())
