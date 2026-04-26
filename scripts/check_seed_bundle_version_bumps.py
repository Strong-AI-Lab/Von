"""CLI: fail if a workflow repo seed bundle changed without a seed_version bump.

JVNAUTOSCI-2132 L0 deliverable. Intended to be wired into local
pre-commit / pre-push hooks and CI. Pure Python; no MCP or DB access.

Usage::

    pdm run python scripts/check_seed_bundle_version_bumps.py
    pdm run python scripts/check_seed_bundle_version_bumps.py --base origin/main
    pdm run python scripts/check_seed_bundle_version_bumps.py path/to/bundle.json ...

Without explicit paths, the script asks ``git diff --name-only <base>``
for changed files under ``src/backend/workflows/repo_seed_bundles/`` and
checks each. Exits with a non-zero status (1) if any workflow bundle
changed without a strictly increasing ``seed_version``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR_REL = "src/backend/workflows/repo_seed_bundles"

# Allow direct execution without ``pdm run`` by ensuring the repo root is
# importable.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.workflows.repo_seed_bundle_version_guard import (  # noqa: E402
    check_seed_version_bumps,
)


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _changed_bundle_paths(base: str) -> list[Path]:
    output = _git(["diff", "--name-only", base, "--", BUNDLE_DIR_REL])
    paths: list[Path] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or not line.endswith(".json"):
            continue
        paths.append(REPO_ROOT / line)
    return paths


def _baseline_resolver(base: str):
    bundle_dir_abs = (REPO_ROOT / BUNDLE_DIR_REL).resolve()

    def _resolve(path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(REPO_ROOT.resolve())
        except ValueError:
            return None
        # Only resolve baselines for files under the seed bundle dir.
        try:
            path.resolve().relative_to(bundle_dir_abs)
        except ValueError:
            return None
        rel_posix = str(rel).replace("\\", "/")
        result = subprocess.run(
            ["git", "show", f"{base}:{rel_posix}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if (
                "exists on disk, but not in" in stderr
                or "does not exist" in stderr
                or "fatal: path" in stderr
            ):
                return None
            # Other git failures (bad ref, etc.) should surface loudly.
            raise RuntimeError(
                f"git show {base}:{rel_posix} failed: {result.stderr.strip()}"
            )
        return result.stdout

    return _resolve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Git ref to diff against (default: origin/main).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "Optional explicit bundle paths to check. If omitted, all changed "
            "*.json files under src/backend/workflows/repo_seed_bundles/ "
            "between HEAD and --base are checked."
        ),
    )
    args = parser.parse_args(argv)

    if args.paths:
        paths = [p if p.is_absolute() else (REPO_ROOT / p) for p in args.paths]
    else:
        try:
            paths = _changed_bundle_paths(args.base)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not paths:
        return 0

    issues = check_seed_version_bumps(
        paths,
        repo_root=REPO_ROOT,
        baseline_resolver=_baseline_resolver(args.base),
    )
    if not issues:
        return 0

    print(
        "Workflow repo seed bundle changes require a strictly increasing "
        "seed_version. Bump seed_version in each affected bundle:",
        file=sys.stderr,
    )
    for issue in issues:
        print(f"  - {issue}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
