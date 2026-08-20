#!/usr/bin/env python3
"""Reclassify workflow action contracts wrongly typed as MCP tools.

JVNAUTOSCI-2651. Twelve concepts are recorded as instances of #V#mcp_tool but
are workflow action contracts: empty attributes, no mcp_tool_name, and (for
seven) targets of #V#invokesAction from entity-representation workflow steps.
The established type is #V#workflow_action_contract.

Each concept gets the correct type ADDED before the wrong one is REMOVED, so no
concept is ever momentarily untyped.

Route note (JVNAUTOSCI-2653): this script calls the canonical relationship
services directly, the established maintenance-script pattern of this
repository, because no governed agent-reachable route exists for an authorised
operator ontology repair. It is deliberately shaped as a stored, verifiable
plan — explicit steps, per-step preconditions, a captured inverse, and
postconditions — because that shape is the specification for the general
governed repair capability that should replace this pattern.

Usage:
    python scripts/repair_mcp_tool_action_reclassification.py           # dry run
    python scripts/repair_mcp_tool_action_reclassification.py --execute
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.db.repositories.concepts_repository import (  # noqa: E402
    ConceptsRepository,
)
from src.backend.services.relationship_write_service import (  # noqa: E402
    add_structural_relationship,
)
from src.backend.services.relationship_removal_service import (  # noqa: E402
    remove_relationship,
)

WRONG_TYPE = "#V#mcp_tool"
CORRECT_TYPE = "#V#workflow_action_contract"
TICKET = "JVNAUTOSCI-2651"

CONCEPT_IDS = [
    "#V#test_org_exists_tool",
    "#V#test_org_type_membership_tool",
    "#V#test_org_evidence_tool",
    "#V#create_org_tool",
    "#V#add_org_type_tool",
    "#V#attach_org_evidence_tool",
    "#V#fail_workflow_tool",
    "#V#test_person_exists_tool",
    "#V#test_affiliation_exists_tool",
    "#V#add_affiliation_tool",
    "#V#attach_affiliation_evidence_tool",
    "#V#test_affiliation_evidence_tool",
]


def _instance_of(concept_id: str) -> list[str]:
    doc = ConceptsRepository.find_one(
        {"concept_id": concept_id}, {"relationships.is_an_instance_of": 1}
    )
    if not isinstance(doc, dict):
        return []
    values = (doc.get("relationships") or {}).get("is_an_instance_of") or []
    return values if isinstance(values, list) else [values]


def _check_preconditions(concept_id: str) -> str | None:
    """Return None when the concept matches the diagnosed state, else why not."""
    doc = ConceptsRepository.find_one({"concept_id": concept_id}, {})
    if not isinstance(doc, dict):
        return "concept_not_found"
    types = _instance_of(concept_id)
    if WRONG_TYPE not in types:
        return f"expected {WRONG_TYPE} in is_an_instance_of, found {types}"
    attributes = doc.get("attributes") or {}
    if attributes.get("mcp_tool_name"):
        return "has mcp_tool_name; this is a real tool row, not a misclassification"
    return None


def run(execute: bool) -> int:
    started = datetime.now(timezone.utc).isoformat()
    log: dict = {
        "ticket": TICKET,
        "started": started,
        "execute": execute,
        "wrong_type": WRONG_TYPE,
        "correct_type": CORRECT_TYPE,
        "steps": [],
    }

    extent_before = ConceptsRepository.collection().count_documents(
        {"relationships.is_an_instance_of": WRONG_TYPE}
    )
    log["extent_before"] = extent_before

    target = ConceptsRepository.find_one({"concept_id": CORRECT_TYPE}, {"concept_id": 1})
    if not target:
        print(f"ABORT: {CORRECT_TYPE} does not exist")
        return 1

    failures = 0
    for concept_id in CONCEPT_IDS:
        entry: dict = {"concept_id": concept_id, "prior_types": _instance_of(concept_id)}
        problem = _check_preconditions(concept_id)
        if problem:
            entry["skipped"] = problem
            log["steps"].append(entry)
            # Already-repaired concepts are an expected rerun state, not an error.
            if "expected" in problem and CORRECT_TYPE in entry["prior_types"]:
                print(f"  ~ {concept_id}: already reclassified")
            else:
                print(f"  ! {concept_id}: precondition failed: {problem}")
                failures += 1
            continue

        if not execute:
            entry["would"] = [
                f"add is_an_instance_of {CORRECT_TYPE}",
                f"remove is_an_instance_of {WRONG_TYPE}",
            ]
            print(f"  - {concept_id}: would reclassify")
            log["steps"].append(entry)
            continue

        added = add_structural_relationship(
            concept_id, "is_an_instance_of", CORRECT_TYPE
        )
        entry["add_result"] = {
            k: added.get(k) for k in ("success", "error", "already_exists")
        }
        if not added.get("success"):
            print(f"  ! {concept_id}: add failed: {added.get('error')}")
            failures += 1
            log["steps"].append(entry)
            continue  # wrong type is left in place; concept is never untyped

        # soft_delete, deliberately. It removes the edge exactly as hard_delete
        # does but also persists a tombstone and undo token. A misclassification
        # is a retraction, which JVNAUTOSCI-2615 requires be a semantic lifecycle
        # event rather than silent physical deletion, and the weaker primitive
        # is the one this repair actually needs.
        removed = remove_relationship(
            source_id=concept_id,
            predicate="is_an_instance_of",
            target=WRONG_TYPE,
            mode="soft_delete",
            confirmed=True,
            reason=f"{TICKET}: workflow action contract misclassified as MCP tool",
            request_id=f"{TICKET}-{concept_id}",
        )
        entry["remove_result"] = {
            k: removed.get(k)
            for k in ("success", "error", "error_code", "status", "undo_token")
        }
        if not removed.get("success"):
            print(f"  ! {concept_id}: remove failed: {removed}")
            failures += 1
            log["steps"].append(entry)
            continue

        entry["final_types"] = _instance_of(concept_id)
        ok = (
            CORRECT_TYPE in entry["final_types"]
            and WRONG_TYPE not in entry["final_types"]
        )
        entry["verified"] = ok
        if ok:
            print(f"  + {concept_id}: reclassified and read back")
        else:
            print(f"  ! {concept_id}: read-back mismatch: {entry['final_types']}")
            failures += 1
        log["steps"].append(entry)

    extent_after = ConceptsRepository.collection().count_documents(
        {"relationships.is_an_instance_of": WRONG_TYPE}
    )
    log["extent_after"] = extent_after
    log["failures"] = failures

    print(f"\n{WRONG_TYPE} extent: {extent_before} -> {extent_after}"
          f" ({'dry run' if not execute else 'executed'}), failures: {failures}")

    out = REPO_ROOT.parent / "Von-Private" / (
        f"repair_{TICKET}_{'execute' if execute else 'dryrun'}_"
        f"{started.replace(':', '').split('.')[0]}.json"
    )
    try:
        out.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
        print(f"log: {out}")
    except OSError:
        print(json.dumps(log, indent=2, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="apply the changes; default is a dry run")
    sys.exit(run(execute=parser.parse_args().execute))
