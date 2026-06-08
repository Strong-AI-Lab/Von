"""Check tracked Python files against Von's minimum supported Python grammar.

This guardrail is intentionally stricter than ``py_compile`` on a newer local
interpreter. For example, Python 3.14 may accept syntax that Python 3.11 cannot
parse. Von currently declares ``requires-python = ">=3.11"``, so code must
remain parseable by the Python 3.11 grammar unless the project minimum changes.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
import tokenize
import tomllib
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURE_VERSION = (3, 11)
_PYTHON_REQUIREMENT_RE = re.compile(r">=\s*(\d+)\.(\d+)")
_FALLBACK_SCAN_DIRS = ("src", "scripts", "tests", "utilities")


@dataclass(frozen=True)
class SyntaxCompatibilityError:
    path: Path
    lineno: int
    offset: int
    message: str
    feature_version: tuple[int, int]

    def format(self, repo_root: Path) -> str:
        try:
            display_path = self.path.relative_to(repo_root).as_posix()
        except ValueError:
            display_path = self.path.as_posix()
        return (
            f"{display_path}:{self.lineno}:{self.offset}: "
            f"not parseable as Python {self.feature_version[0]}.{self.feature_version[1]}: "
            f"{self.message}"
        )


def minimum_feature_version_from_pyproject(
    repo_root: Path = REPO_ROOT,
) -> tuple[int, int]:
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return DEFAULT_FEATURE_VERSION
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    requirement = str(data.get("project", {}).get("requires-python") or "")
    match = _PYTHON_REQUIREMENT_RE.search(requirement)
    if match is None:
        return DEFAULT_FEATURE_VERSION
    return (int(match.group(1)), int(match.group(2)))


def list_tracked_python_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return [
            repo_root / line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
    return _fallback_python_file_scan(repo_root)


def check_python_files(
    paths: Iterable[Path],
    *,
    feature_version: tuple[int, int],
) -> list[SyntaxCompatibilityError]:
    errors: list[SyntaxCompatibilityError] = []
    for path in sorted({Path(candidate) for candidate in paths}):
        if not path.exists() or not path.is_file():
            continue
        error = check_python_file(path, feature_version=feature_version)
        if error is not None:
            errors.append(error)
    return errors


def check_python_file(
    path: Path,
    *,
    feature_version: tuple[int, int],
) -> SyntaxCompatibilityError | None:
    try:
        with tokenize.open(path) as handle:
            source = handle.read()
        ast.parse(source, filename=str(path), feature_version=feature_version)
    except SyntaxError as exc:
        return SyntaxCompatibilityError(
            path=path,
            lineno=int(exc.lineno or 1),
            offset=int(exc.offset or 1),
            message=str(exc.msg),
            feature_version=feature_version,
        )
    return None


def parse_feature_version(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError("expected MAJOR.MINOR, for example 3.11")
    return (int(match.group(1)), int(match.group(2)))


def resolve_cli_paths(paths: Sequence[str], repo_root: Path = REPO_ROOT) -> list[Path]:
    if not paths:
        return list_tracked_python_files(repo_root)
    resolved: list[Path] = []
    for raw_path in paths:
        text = raw_path.strip()
        if not text or not text.endswith(".py"):
            continue
        path = Path(text)
        resolved.append(path if path.is_absolute() else repo_root / path)
    return resolved


def _fallback_python_file_scan(repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    for name in _FALLBACK_SCAN_DIRS:
        directory = repo_root / name
        if not directory.exists():
            continue
        paths.extend(path for path in directory.rglob("*.py") if path.is_file())
    return sorted(paths)


def main(argv: Sequence[str] | None = None) -> int:
    default_feature_version = minimum_feature_version_from_pyproject(REPO_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feature-version",
        type=parse_feature_version,
        default=default_feature_version,
        help=(
            "Minimum Python grammar to enforce, as MAJOR.MINOR. "
            f"Default: {default_feature_version[0]}.{default_feature_version[1]}"
        ),
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=(),
        help="Optional repo-relative Python files to check. Defaults to all tracked Python files.",
    )
    args = parser.parse_args(argv)

    paths = resolve_cli_paths(args.paths, REPO_ROOT)
    errors = check_python_files(paths, feature_version=args.feature_version)
    if errors:
        print(
            "Python syntax compatibility check failed. "
            f"{len(errors)} file(s) are not parseable as Python "
            f"{args.feature_version[0]}.{args.feature_version[1]}."
        )
        for error in errors:
            print(error.format(REPO_ROOT))
        return 1

    print(
        f"Python syntax compatibility check passed for {len(paths)} file(s) "
        f"against Python {args.feature_version[0]}.{args.feature_version[1]} grammar."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
