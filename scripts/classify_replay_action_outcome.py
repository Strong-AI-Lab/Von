"""Classify generic action/observation outcomes for replay sampler artefacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from collections.abc import Mapping, Sequence
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_live_kb_tool_prompt_sampler import classify_replay_action_outcome


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object.")
    return dict(payload)


def _classify_path(path: Path, *, recompute: bool = False) -> dict[str, Any]:
    summary = _load_json_object(path)
    existing = summary.get("action_outcome")
    action_outcome = (
        dict(existing)
        if isinstance(existing, Mapping) and not recompute
        else classify_replay_action_outcome(summary)
    )
    return {
        "path": str(path),
        "status": summary.get("status"),
        "prompt_id": (
            summary.get("prompt", {}).get("id")
            if isinstance(summary.get("prompt"), Mapping)
            else None
        ),
        "outcome": action_outcome.get("outcome"),
        "request_id": action_outcome.get("request_id"),
        "selected_workflow_id": action_outcome.get("selected_workflow_id"),
        "observed_tools": action_outcome.get("observed_tools", []),
        "timeout_detected": action_outcome.get("timeout_detected"),
        "action_started": action_outcome.get("action_started"),
        "evidence": action_outcome.get("evidence", []),
        "limitations": action_outcome.get("limitations", []),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Classify replay sampler JSON artefacts into generic action/"
            "observation outcomes."
        )
    )
    parser.add_argument("paths", nargs="+", help="Replay JSON artefact path(s).")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON array instead of one compact JSON object per line.",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Ignore embedded action_outcome fields and classify from the artefact.",
    )
    args = parser.parse_args(argv)

    rows = [_classify_path(Path(path), recompute=bool(args.recompute)) for path in args.paths]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
