from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.services import concept_service
from src.backend.services.concept_service import ConceptNotFoundError
from src.backend.vontology.code_concepts_registry import list_code_predicate_ids


def verify_predicate_concepts() -> Dict[str, object]:
    missing: List[str] = []
    virtual: List[str] = []
    present: List[str] = []
    warnings: List[str] = []

    predicate_ids = sorted(set(list_code_predicate_ids()))

    for predicate_id in predicate_ids:
        try:
            concept = concept_service.get_concept_by_concept_id(predicate_id)
        except ConceptNotFoundError:
            missing.append(predicate_id)
            continue
        except Exception as exc:
            warnings.append(f"lookup_failed:{predicate_id}:{exc}")
            continue

        if not concept:
            missing.append(predicate_id)
            continue
        if (concept.get("metadata") or {}).get("virtual"):
            virtual.append(predicate_id)
            missing.append(predicate_id)
            continue
        present.append(predicate_id)

    return {
        "total_registry": len(predicate_ids),
        "present": sorted(present),
        "missing": sorted(missing),
        "virtual": sorted(virtual),
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify predicate concepts from the code registry are persisted in Vontology."
        )
    )
    _ = parser.parse_args()
    payload = verify_predicate_concepts()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
