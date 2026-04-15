from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.powershell_probe_support import (  # noqa: E402
    DEFAULT_POWERSHELL_PROBE_PREFIXES,
    DEFAULT_STALE_PROBE_MIN_AGE_SECONDS,
    cleanup_stale_powershell_probe_dirs,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Terminate stale PowerShell probe processes and remove their temp "
            "directories. Output is line-oriented so large JSON payloads are not "
            "needed for routine cleanup."
        )
    )
    parser.add_argument(
        "--temp-root",
        type=Path,
        default=None,
        help="Override the temp root to scan. Defaults to the system temp directory.",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=float,
        default=DEFAULT_STALE_PROBE_MIN_AGE_SECONDS,
        help=(
            "Only clean probe directories at least this old. "
            f"Default: {DEFAULT_STALE_PROBE_MIN_AGE_SECONDS:.0f}s."
        ),
    )
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        default=[],
        help=(
            "Probe-directory prefix to clean. Can be supplied multiple times. "
            f"Defaults to: {', '.join(DEFAULT_POWERSHELL_PROBE_PREFIXES)}."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1.0,
        help="Per-process wait timeout before force-kill. Default: 1.0.",
    )
    parser.add_argument(
        "--keep-dirs",
        action="store_true",
        help="Terminate matching processes but keep the probe directories on disk.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    prefixes = (
        tuple(args.prefixes)
        if args.prefixes
        else DEFAULT_POWERSHELL_PROBE_PREFIXES
    )
    cleaned = cleanup_stale_powershell_probe_dirs(
        temp_root=args.temp_root,
        min_age_seconds=args.min_age_seconds,
        prefixes=prefixes,
        timeout_seconds=args.timeout_seconds,
        remove_dir=not args.keep_dirs,
    )
    if not cleaned:
        print("No stale PowerShell probe directories matched the requested filters.")
        return 0

    failures = 0
    for entry in cleaned:
        probe_dir = str(entry.get("probe_dir") or "")
        killed_raw = entry.get("killed_pids")
        killed = killed_raw if isinstance(killed_raw, list) else []
        age_raw = entry.get("age_seconds")
        age_seconds = float(age_raw) if isinstance(age_raw, (int, float)) else 0.0
        removed = bool(entry.get("removed"))
        error = entry.get("error")
        print(
            f"Cleaned probe {probe_dir} "
            f"(age={age_seconds:.1f}s, killed_pids={','.join(str(pid) for pid in killed) or 'none'}, "
            f"removed={removed})"
        )
        if error:
            failures += 1
            print(f"WARN: failed to remove {probe_dir}: {error}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
