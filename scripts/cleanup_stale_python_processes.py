from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Best-effort cleanup of stale launcher-managed Python processes."
    )
    parser.add_argument("script_path")
    parser.add_argument("exclude_pid", nargs="?", default="0")
    return parser


def main(argv: list[str] | None = None) -> int:
    from src.backend.utilities.process_hygiene import terminate_processes_matching_script

    parser = _build_parser()
    args = parser.parse_args(argv)
    exclude_pid = int(args.exclude_pid or 0)
    killed = terminate_processes_matching_script(
        args.script_path,
        exclude_pid=exclude_pid,
    )
    print(json.dumps(killed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
