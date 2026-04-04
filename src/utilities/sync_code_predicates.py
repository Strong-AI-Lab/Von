from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sync_code_predicate_concepts = importlib.import_module(
    "src.backend.services.code_predicate_sync_service"
).sync_code_predicate_concepts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ensure predicate concepts referenced in code are represented in Vontology."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to Vontology.",
    )
    parser.add_argument(
        "--retag-non-predicates",
        action="store_true",
        help="Add predicate typing to existing concepts that are not typed as predicates.",
    )
    args = parser.parse_args()

    result = sync_code_predicate_concepts(
        dry_run=args.dry_run, retag_non_predicates=args.retag_non_predicates
    )
    payload = {
        "created": result.created,
        "updated": result.updated,
        "skipped": result.skipped,
        "warnings": result.warnings,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
