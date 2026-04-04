from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
import tokenize
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

list_code_predicate_ids = importlib.import_module(
    "src.backend.vontology.code_concepts_registry"
).list_code_predicate_ids


CONCEPT_ID_RE = r"#V#[-A-Za-z0-9_]+"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iter_source_files(paths: Sequence[Path]) -> Iterable[Path]:
    skip_dirs = {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "data",
    }
    extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".jsonc"}
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_dir() and child.name in skip_dirs:
                    continue
                if child.is_file() and child.suffix in extensions:
                    yield child
        elif path.is_file() and path.suffix in extensions:
            yield path


def _docstring_ranges(source: str) -> List[Tuple[int, int]]:
    try:
        tree = ast.parse(source)
    except Exception:
        return []

    ranges: List[Tuple[int, int]] = []
    nodes: List[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes.append(node)

    for node in nodes:
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            start = getattr(value, "lineno", None)
            end = getattr(value, "end_lineno", start)
        else:
            continue
        if start is None:
            continue
        ranges.append((start, end or start))
    return ranges


def _is_in_ranges(line_no: int, ranges: List[Tuple[int, int]]) -> bool:
    for start, end in ranges:
        if start <= line_no <= end:
            return True
    return False


def extract_predicates_from_python(
    source: str, *, include_docstrings: bool = False
) -> Set[str]:
    doc_ranges = [] if include_docstrings else _docstring_ranges(source)
    predicates: Set[str] = set()
    pattern = ast.literal_eval  # type: ignore[assignment]

    try:
        tokens = tokenize.tokenize(BytesIO(source.encode("utf-8")).readline)
    except Exception:
        return predicates

    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        if doc_ranges and _is_in_ranges(tok.start[0], doc_ranges):
            continue
        raw = tok.string
        try:
            value = pattern(raw)
        except Exception:
            value = raw
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="ignore")
            except Exception:
                value = raw
        if not isinstance(value, str):
            continue
        predicates.update(_scan_concept_ids(value))
    return predicates


def _scan_concept_ids(text: str) -> Set[str]:
    return set({m.group(0) for m in __import__("re").finditer(CONCEPT_ID_RE, text)})


def _normalise_string_list(value: object) -> List[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _iter_js_string_literals(text: str) -> Iterable[str]:
    i = 0
    n = len(text)
    quote: Optional[str] = None
    buf: List[str] = []
    while i < n:
        ch = text[i]
        if quote is None:
            if ch in ("'", '"', "`"):
                quote = ch
                buf = []
            i += 1
            continue
        if ch == "\\":
            if i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
        if ch == quote:
            yield "".join(buf)
            quote = None
            i += 1
            continue
        buf.append(ch)
        i += 1


def _strip_js_comments(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_line = False
    in_block = False
    quote: Optional[str] = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote is not None:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def extract_predicates_from_js(text: str) -> Set[str]:
    stripped = _strip_js_comments(text)
    predicates: Set[str] = set()
    for literal in _iter_js_string_literals(stripped):
        predicates.update(_scan_concept_ids(literal))
    return predicates


def scan_files(
    paths: Sequence[Path],
    *,
    include_docstrings: bool = False,
) -> Dict[str, object]:
    found: Set[str] = set()
    by_file: Dict[str, List[str]] = {}
    scanned = 0

    for path in _iter_source_files(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        scanned += 1
        if path.suffix == ".py":
            predicates = extract_predicates_from_python(
                text, include_docstrings=include_docstrings
            )
        else:
            predicates = extract_predicates_from_js(text)
        if predicates:
            found.update(predicates)
            by_file[str(path)] = sorted(predicates)

    registry = set(list_code_predicate_ids())
    code_mentions, test_mentions = _classify_mentions(by_file, repo_root=_repo_root())
    return {
        "files_scanned": scanned,
        "found_predicates": sorted(found),
        "unknown_predicates": sorted(found - registry),
        "registry_only_predicates": sorted(registry - found),
        "by_file": by_file,
        "code_mentions": sorted(code_mentions),
        "test_mentions": sorted(test_mentions),
        "shared_mentions": sorted(code_mentions & test_mentions),
    }


def _classify_mentions(
    by_file: Dict[str, List[str]], *, repo_root: Path
) -> Tuple[Set[str], Set[str]]:
    code_mentions: Set[str] = set()
    test_mentions: Set[str] = set()

    for path_str, mentions in by_file.items():
        path = Path(path_str)
        try:
            rel = path.resolve().relative_to(repo_root)
        except Exception:
            rel = path
        if rel.parts and rel.parts[0] == "tests":
            test_mentions.update(mentions)
        else:
            code_mentions.update(mentions)

    return code_mentions, test_mentions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan code for #V# concept IDs (excluding comments by default)."
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=[],
        help="Paths to scan. Defaults to src/ and tests/ under repo root.",
    )
    parser.add_argument(
        "--include-docstrings",
        action="store_true",
        help="Include Python docstrings in the scan (default: excluded).",
    )
    parser.add_argument(
        "--sync-mentions",
        action="store_true",
        help="Dry-run sync of mentioned-in-code/test tags for found concept IDs.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply mention tags to Vontology (default: dry-run).",
    )
    args = parser.parse_args()

    root = _repo_root()
    scan_paths = [Path(p) for p in args.paths] if args.paths else [root / "src", root / "tests"]
    payload = scan_files(scan_paths, include_docstrings=args.include_docstrings)
    if args.sync_mentions:
        from src.backend.services.code_mention_sync_service import (
            sync_code_mention_concepts,
        )

        sync_result = sync_code_mention_concepts(
            code_mentions=set(_normalise_string_list(payload.get("code_mentions"))),
            test_mentions=set(_normalise_string_list(payload.get("test_mentions"))),
            dry_run=not args.apply,
        )
        payload["sync_dry_run"] = not args.apply
        payload["sync_result"] = {
            "updated": sync_result.updated,
            "skipped": sync_result.skipped,
            "missing": sync_result.missing,
            "virtual": sync_result.virtual,
            "warnings": sync_result.warnings,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
