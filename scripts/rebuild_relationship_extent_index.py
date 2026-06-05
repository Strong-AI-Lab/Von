#!/usr/bin/env python3
"""Rebuild the derived Vontology relationship extent index."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.services.relationship_extent_index_service import (  # noqa: E402
    rebuild_relationship_extent_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild relationship_extent_index from canonical concepts.relationships."
        )
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--reason", default="maintenance_script")
    args = parser.parse_args()

    result = rebuild_relationship_extent_index(
        batch_size=max(1, args.batch_size),
        reason=args.reason,
    )
    print(json.dumps(result, default=str, sort_keys=True))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
