from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
repo_root_str = str(REPO_ROOT)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)

from src.backend.services.text_value_service import get_texts_for_concept  # noqa: E402


def _resolve_path(raw_value: str) -> Path:
    return Path(raw_value).expanduser().resolve()


def _load_unique_prompt_content(prompt_id: str) -> str:
    rows = get_texts_for_concept(prompt_id, limit=100)
    candidates = {
        str(row.get("text") or "").strip()
        for row in rows
        if isinstance(row, dict)
        and str(row.get("predicate") or "").strip() in {"hasContent", "#V#hasContent"}
        and str(row.get("lang") or "en-NZ").strip() in {"en-NZ", "en"}
        and str(row.get("text") or "").strip()
    }
    if not candidates:
        raise RuntimeError(f"workflow_prompt_authority_content_missing:{prompt_id}")
    if len(candidates) != 1:
        raise RuntimeError(f"workflow_prompt_authority_content_ambiguous:{prompt_id}")
    return next(iter(candidates))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export one generated repo-seed Markdown snapshot from authoritative "
            "Vontology prompt content."
        )
    )
    parser.add_argument("--prompt-id", required=True)
    parser.add_argument("--asset-path", required=True)
    args = parser.parse_args(argv)

    prompt_id = str(args.prompt_id or "").strip()
    if not prompt_id.startswith("#V#"):
        raise ValueError("workflow_prompt_concept_id_invalid")
    target_path = _resolve_path(args.asset_path)
    content = _load_unique_prompt_content(prompt_id)
    rendered = content.rstrip() + "\n"
    target_path.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "asset_path": str(target_path),
                "bytes_written": len(rendered.encode("utf-8")),
                "prompt_id": prompt_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
