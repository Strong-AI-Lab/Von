"""Build sanitised lexical title/content projections for existing conversations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from src.backend.services.chat_history_service import (
    backfill_conversation_search_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-concept-id")
    parser.add_argument("--namespace")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist projections. Without this flag the command is a dry run.",
    )
    args = parser.parse_args()
    result = backfill_conversation_search_index(
        user_concept_id=args.user_concept_id,
        namespace=args.namespace,
        max_sessions=args.limit,
        dry_run=not args.apply,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
