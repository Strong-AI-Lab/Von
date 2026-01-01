"""Scan VS Code state databases for Copilot Chat selected tools.

This is a small debugging utility to understand why `chat/selectedTools` appears to
"snap back" after being pruned.

NZ English spelling.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def profile_root(profile: str) -> Path:
    appdata = Path(os.environ["APPDATA"])
    if profile == "insiders":
        return appdata / "Code - Insiders" / "User"
    if profile == "stable":
        return appdata / "Code" / "User"
    raise ValueError(f"Unknown profile: {profile}")


def load_selected_tools_counts(db_path: Path) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except Exception as exc:
        return {"error": f"connect_failed: {type(exc).__name__}: {exc}"}

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key='chat/selectedTools' LIMIT 1"
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return {"present": False}

        raw = row[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        data = json.loads(raw)

        if (
            isinstance(data, dict)
            and isinstance(data.get("toolSetEntries"), list)
            and isinstance(data.get("toolEntries"), list)
        ):
            toolsets_true = sum(
                1
                for e in data["toolSetEntries"]
                if isinstance(e, list) and len(e) >= 2 and e[1] is True
            )
            tools_true = sum(
                1
                for e in data["toolEntries"]
                if isinstance(e, list) and len(e) >= 2 and e[1] is True
            )
            return {
                "present": True,
                "shape": "structured_pairs",
                "version": data.get("version"),
                "toolsets_total": len(data["toolSetEntries"]),
                "tools_total": len(data["toolEntries"]),
                "toolsets_true": toolsets_true,
                "tools_true": tools_true,
                "enabled_total": toolsets_true + tools_true,
            }

        if isinstance(data, list):
            return {"present": True, "shape": "flat_list", "enabled_total": len(data)}

        return {
            "present": True,
            "shape": type(data).__name__,
            "note": "unrecognised shape",
        }

    except Exception as exc:
        return {"error": f"read_failed: {type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_simple_item_values(db_path: Path, keys: list[str]) -> dict[str, Any]:
    """Load a small set of non-sensitive ItemTable values.

    We keep this intentionally narrow to avoid dumping arbitrary global state.
    """

    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except Exception as exc:
        return {"error": f"connect_failed: {type(exc).__name__}: {exc}"}

    try:
        cur = conn.cursor()
        out: dict[str, Any] = {}
        for key in keys:
            cur.execute("SELECT value FROM ItemTable WHERE key=? LIMIT 1", (key,))
            row = cur.fetchone()
            if not row or row[0] is None:
                out[key] = {"present": False}
                continue

            raw = row[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")

            # Avoid dumping large blobs of synced state; keep output compact.
            info: dict[str, Any] = {"present": True, "length": len(raw)}
            if len(raw) <= 140 and "\n" not in raw and "\r" not in raw:
                info["value"] = raw
            else:
                info["preview"] = raw[:120]
            out[key] = info
        return out
    except Exception as exc:
        return {"error": f"read_failed: {type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def iter_workspace_dbs(ws_root: Path) -> list[Path]:
    if not ws_root.exists():
        return []
    dbs: list[Path] = []
    for d in sorted(ws_root.iterdir()):
        if not d.is_dir():
            continue
        db = d / "state.vscdb"
        if db.exists():
            dbs.append(db)
    return dbs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=["insiders", "stable"],
        default="insiders",
        help="VS Code profile to inspect",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=200,
        help="Maximum workspace DBs to print (sorted by enabled count)",
    )

    args = parser.parse_args()

    root = profile_root(args.profile)
    ws_root = root / "workspaceStorage"
    global_db = root / "globalStorage" / "state.vscdb"

    def scan_other_keys_for_selection_signals(db_path: Path) -> dict[str, Any]:
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        except Exception as exc:
            return {"error": f"connect_failed: {type(exc).__name__}: {exc}"}

        keys_to_check = [
            "GitHub.copilot-chat",
            "GitHub.copilot",
            "mcpToolCache",
            "mcpInputs",
        ]
        needles = [
            "chat/selectedTools",
            "selectedTools",
            "toolEntries",
            "toolSetEntries",
        ]

        try:
            cur = conn.cursor()
            out: dict[str, Any] = {}
            for key in keys_to_check:
                cur.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,))
                row = cur.fetchone()
                if not row or row[0] is None:
                    out[key] = {"present": False}
                    continue

                raw = row[0]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                # Do not print raw values; only report whether interesting substrings exist.
                out[key] = {
                    "present": True,
                    "length": len(raw),
                    "contains": {needle: (needle in raw) for needle in needles},
                }
            return out
        except Exception as exc:
            return {"error": f"read_failed: {type(exc).__name__}: {exc}"}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    print(f"Profile root: {root}")
    print(f"Global DB: {global_db}")
    print(f"WorkspaceStorage: {ws_root}")

    if global_db.exists():
        st = global_db.stat()
        print(
            "\nGLOBAL DB file:\n"
            + json.dumps(
                {
                    "size_bytes": st.st_size,
                    "mtime_epoch": int(st.st_mtime),
                },
                indent=2,
            )
        )

    if global_db.exists():
        g = load_selected_tools_counts(global_db)
        print("\nGLOBAL chat/selectedTools:")
        print(json.dumps(g, indent=2))

        sync_keys = [
            "sync.enable",
            "sync.lastSyncTime",
            "globalState.lastSyncUserData",
            "extensions.lastSyncUserData",
            "mcp.lastSyncUserData",
        ]
        sync_info = load_simple_item_values(global_db, sync_keys)
        print("\nGLOBAL sync indicators:")
        print(json.dumps(sync_info, indent=2))

        other = scan_other_keys_for_selection_signals(global_db)
        print("\nGLOBAL other keys (selection signals only):")
        print(json.dumps(other, indent=2))
    else:
        print("\nGLOBAL chat/selectedTools: (missing global DB)")

    ws_dbs = iter_workspace_dbs(ws_root)
    print(f"\nWorkspace DBs found: {len(ws_dbs)}")

    rows: list[tuple[int, str, dict[str, Any]]] = []
    for db in ws_dbs:
        info = load_selected_tools_counts(db)
        if info.get("present") is True:
            enabled_total = int(info.get("enabled_total") or 0)
            rows.append((enabled_total, str(db), info))

    rows.sort(key=lambda r: r[0], reverse=True)

    print("\nWORKSPACE DBs with chat/selectedTools present (highest enabled first):")
    if not rows:
        print("(none)")
        return 0

    for enabled, db, info in rows[: args.max]:
        shape = info.get("shape")
        toolsets_true = info.get("toolsets_true")
        tools_true = info.get("tools_true")
        if tools_true is not None or toolsets_true is not None:
            extra = f" enabled_total={enabled} (toolsets_true={toolsets_true}, tools_true={tools_true})"
        else:
            extra = f" enabled_total={enabled}"
        print(f"- {db}: {shape}{extra}")

    if len(rows) > args.max:
        print(f"... ({len(rows) - args.max} more)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
