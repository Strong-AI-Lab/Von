"""Inspect/install the durable-worker database admission fence.

Use --worker-pattern for a bounded legacy-host rollout. Existing validators
are preserved. Activation is explicit and prints a rollback receipt.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backend.db.mongo_client import get_db
from src.backend.workflows.durable.task_ownership import claim_admission_validator


def install_admission(
    db, *, collection="workflow_instances", worker_pattern="^worker_", apply=False
):
    options = db[collection].options()
    rule = claim_admission_validator(worker_pattern)
    existing = options.get("validator", {})
    validator = (
        existing
        if existing == rule
        else {"$and": [existing, rule]} if existing else rule
    )
    receipt = {
        "collection": collection,
        "previous": {
            "validator": existing,
            "validationLevel": options.get("validationLevel", "strict"),
            "validationAction": options.get("validationAction", "error"),
        },
        "proposed": {
            "validator": validator,
            "validationLevel": "strict",
            "validationAction": "error",
        },
        "applied": False,
    }
    if apply:
        db.command({"collMod": collection, **receipt["proposed"]})
        actual = db[collection].options()
        receipt["applied"] = all(
            actual.get(k) == v for k, v in receipt["proposed"].items()
        )
        if not receipt["applied"]:
            raise RuntimeError("claim_admission_readback_mismatch")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--worker-pattern", default="^worker_")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = install_admission(
        get_db(), worker_pattern=args.worker_pattern, apply=args.apply
    )
    args.receipt.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, default=str))
