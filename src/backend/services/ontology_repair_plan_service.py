"""Proposable, verifiable ontology repair plans.

A repair plan is a declarative, ordered list of structural ontology changes with
a per-step precondition and a stable digest. It can be built and validated
without any mutation authority, and executed only against an approval bound to
that exact digest.

Why this exists, from JVNAUTOSCI-2651. Reclassifying twelve misclassified
concepts by hand established what a repair actually needs, and none of it is
expressible in the single-shot mutation tools:

- Ordering is semantic. Each concept had to gain its correct type before losing
  the wrong one so it was never momentarily untyped. That ordering earned its
  keep on the first run, where every add succeeded and every retraction was
  refused, leaving a benign both-types state instead of a broken one.
- Preconditions make a plan safe to propose now and execute later. A step that
  finds the world already changed reports drift rather than acting on a stale
  assumption.
- Partial failure must be safe by construction, which is a property of the step
  order, not of the executor.
- The weakest sufficient primitive should be the default. That run asked for a
  hard delete it did not need and was correctly refused.

The affordance this adds is a third mode between acting and describing:
**propose precisely, with evidence, verifiably, without authority.** An agent
that cannot mutate the ontology can still produce a plan a human can read,
diff, approve, and execute, and the approval names those exact changes rather
than granting standing write access.

Authority note (JVNAUTOSCI-2653). This module adds no route and no principal.
Building and validating a plan are read-only. Execution calls the same
canonical services an operator maintenance script calls today and therefore
runs at exactly the authority it already had. The plan object is deliberately
the shape that "execute server-stored canonical arguments" would need, so that
decision becomes cheaper to implement without being pre-empted here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Sequence

logger = logging.getLogger(__name__)

PLAN_FORMAT_VERSION = "ontology_repair_plan.v1"

# Operations are deliberately few. Each maps to one canonical service call and
# uses the weakest primitive that achieves it.
Operation = Literal["assert_structural", "retract_structural"]

StepStatus = Literal["ready", "already_satisfied", "drifted", "blocked"]


class OntologyRepairPlanError(Exception):
    """Raised when a plan is malformed or executed without a matching approval."""


@dataclass(frozen=True)
class RepairStep:
    """One structural change, with the condition that makes it meaningful."""

    operation: Operation
    source_id: str
    predicate: str
    target_id: str
    rationale: str = ""

    def canonical(self) -> dict[str, str]:
        """The content the digest covers. Rationale is prose and excluded."""
        return {
            "operation": self.operation,
            "source_id": self.source_id,
            "predicate": self.predicate,
            "target_id": self.target_id,
        }


@dataclass(frozen=True)
class RepairPlan:
    """An ordered set of steps proposed as one reviewable unit."""

    plan_id: str
    rationale: str
    steps: tuple[RepairStep, ...]
    ticket: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def canonical(self) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT_VERSION,
            "plan_id": self.plan_id,
            "steps": [step.canonical() for step in self.steps],
        }

    @property
    def digest(self) -> str:
        """Stable hash of plan identity and ordered step content.

        Approval binds to this. Reordering, adding, removing, or altering any
        step changes it, so an approval cannot survive an edit to what it
        approved. Prose does not affect it, so a plan can be re-explained
        without invalidating review.
        """
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [asdict(step) for step in self.steps]
        data["format"] = PLAN_FORMAT_VERSION
        data["digest"] = self.digest
        return data


def build_repair_plan(
    *,
    plan_id: str,
    rationale: str,
    steps: Iterable[Mapping[str, Any] | RepairStep],
    ticket: str | None = None,
) -> RepairPlan:
    """Assemble a plan. Pure: touches no database."""
    built: list[RepairStep] = []
    for raw in steps:
        step = raw if isinstance(raw, RepairStep) else RepairStep(**dict(raw))
        if step.operation not in ("assert_structural", "retract_structural"):
            raise OntologyRepairPlanError(f"unsupported operation: {step.operation}")
        if not (step.source_id and step.predicate and step.target_id):
            raise OntologyRepairPlanError("each step needs source, predicate and target")
        built.append(step)
    if not built:
        raise OntologyRepairPlanError("a plan needs at least one step")
    return RepairPlan(
        plan_id=plan_id,
        rationale=rationale,
        steps=tuple(built),
        ticket=ticket,
    )


def _structural_targets(source_id: str, predicate: str, repo: Any) -> list[str]:
    doc = repo.find_one({"concept_id": source_id}, {f"relationships.{predicate}": 1})
    if not isinstance(doc, dict):
        return []
    values = (doc.get("relationships") or {}).get(predicate) or []
    if isinstance(values, str):
        return [values]
    return [v for v in values if isinstance(v, str)]


def _step_status(step: RepairStep, repo: Any) -> tuple[StepStatus, str]:
    """Classify a step against live state without changing anything."""
    source = repo.find_one({"concept_id": step.source_id}, {"concept_id": 1})
    if not source:
        return "blocked", f"source concept {step.source_id} not found"

    present = step.target_id in _structural_targets(step.source_id, step.predicate, repo)

    if step.operation == "assert_structural":
        target = repo.find_one({"concept_id": step.target_id}, {"concept_id": 1})
        if not target:
            return "blocked", f"target concept {step.target_id} not found"
        if present:
            return "already_satisfied", "edge already present"
        return "ready", "edge absent and both concepts exist"

    if not present:
        return "already_satisfied", "edge already absent"
    # A retraction must not strand the concept without any value for a
    # structural predicate that carries its classification.
    remaining = [
        value
        for value in _structural_targets(step.source_id, step.predicate, repo)
        if value != step.target_id
    ]
    if not remaining:
        return (
            "drifted",
            f"retracting would leave {step.source_id} with no {step.predicate}; "
            "order an assertion before it",
        )
    return "ready", "edge present and another value remains"


def validate_repair_plan(plan: RepairPlan, *, repo: Any = None) -> dict[str, Any]:
    """Check every step against live state. Read-only.

    Steps are evaluated in order against a simulated projection, so an
    assertion earlier in the plan satisfies a retraction that depends on it.
    That is what lets add-before-remove validate as safe rather than reporting
    a false stranding.
    """
    if repo is None:
        from ..db.repositories.concepts_repository import ConceptsRepository

        repo = ConceptsRepository

    simulated: dict[tuple[str, str], list[str]] = {}

    def _targets(source_id: str, predicate: str) -> list[str]:
        key = (source_id, predicate)
        if key not in simulated:
            simulated[key] = list(_structural_targets(source_id, predicate, repo))
        return simulated[key]

    results: list[dict[str, Any]] = []
    for index, step in enumerate(plan.steps):
        current = _targets(step.source_id, step.predicate)
        source_exists = bool(repo.find_one({"concept_id": step.source_id}, {"concept_id": 1}))

        if not source_exists:
            status, detail = "blocked", f"source concept {step.source_id} not found"
        elif step.operation == "assert_structural":
            if not repo.find_one({"concept_id": step.target_id}, {"concept_id": 1}):
                status, detail = "blocked", f"target concept {step.target_id} not found"
            elif step.target_id in current:
                status, detail = "already_satisfied", "edge already present"
            else:
                status, detail = "ready", "edge absent and both concepts exist"
                current.append(step.target_id)
        else:
            if step.target_id not in current:
                status, detail = "already_satisfied", "edge already absent"
            elif len([v for v in current if v != step.target_id]) == 0:
                status, detail = (
                    "drifted",
                    f"retracting would leave {step.source_id} with no "
                    f"{step.predicate}; order an assertion before it",
                )
            else:
                status, detail = "ready", "edge present and another value remains"
                current.remove(step.target_id)

        results.append(
            {
                "index": index,
                "step": step.canonical(),
                "status": status,
                "detail": detail,
                "rationale": step.rationale,
            }
        )

    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    return {
        "plan_id": plan.plan_id,
        "digest": plan.digest,
        "format": PLAN_FORMAT_VERSION,
        "steps": results,
        "counts": counts,
        "executable": counts.get("blocked", 0) == 0 and counts.get("drifted", 0) == 0,
        "changes_pending": counts.get("ready", 0),
    }


def execute_repair_plan(
    plan: RepairPlan,
    *,
    approved_digest: str,
    repo: Any = None,
) -> dict[str, Any]:
    """Apply a plan whose digest matches an explicit approval.

    Refuses outright unless ``approved_digest`` equals the plan digest, so an
    approval can never apply to changes other than the ones reviewed.

    Every step is re-validated immediately before it runs, because a plan may be
    approved long after it was built. Steps reported ``already_satisfied`` are
    skipped, which makes execution idempotent and safe to resume.
    """
    if approved_digest != plan.digest:
        raise OntologyRepairPlanError(
            "approval does not match this plan; approved "
            f"{approved_digest[:12]}… but plan is {plan.digest[:12]}…"
        )

    if repo is None:
        from ..db.repositories.concepts_repository import ConceptsRepository

        repo = ConceptsRepository

    from .relationship_removal_service import remove_relationship
    from .relationship_write_service import add_structural_relationship

    receipts: list[dict[str, Any]] = []
    applied = failed = skipped = 0

    for index, step in enumerate(plan.steps):
        status, detail = _step_status(step, repo)
        receipt: dict[str, Any] = {
            "index": index,
            "step": step.canonical(),
            "precondition": status,
        }

        if status in ("blocked", "drifted"):
            receipt["outcome"] = "refused"
            receipt["detail"] = detail
            failed += 1
            receipts.append(receipt)
            continue

        if status == "already_satisfied":
            receipt["outcome"] = "skipped"
            receipt["detail"] = detail
            skipped += 1
            receipts.append(receipt)
            continue

        if step.operation == "assert_structural":
            result = add_structural_relationship(
                step.source_id, step.predicate, step.target_id
            )
            receipt["result"] = {k: result.get(k) for k in ("success", "error")}
            ok = bool(result.get("success"))
        else:
            # soft_delete is the weakest primitive that removes the edge, and it
            # records a tombstone and undo token. Retraction is a lifecycle
            # event, not a physical deletion.
            result = remove_relationship(
                source_id=step.source_id,
                predicate=step.predicate,
                target=step.target_id,
                mode="soft_delete",
                confirmed=True,
                reason=f"{plan.plan_id}: {step.rationale or plan.rationale}",
                request_id=f"{plan.plan_id}-{index}",
            )
            receipt["result"] = {
                k: result.get(k)
                for k in ("success", "error", "error_code", "undo_token")
            }
            ok = bool(result.get("success"))

        # Canonical read-back per step, not once at the end.
        observed = _structural_targets(step.source_id, step.predicate, repo)
        expected = (
            step.target_id in observed
            if step.operation == "assert_structural"
            else step.target_id not in observed
        )
        receipt["read_back"] = observed
        receipt["verified"] = bool(ok and expected)
        receipt["outcome"] = "applied" if receipt["verified"] else "failed"
        if receipt["verified"]:
            applied += 1
        else:
            failed += 1
        receipts.append(receipt)

    return {
        "plan_id": plan.plan_id,
        "digest": plan.digest,
        "format": PLAN_FORMAT_VERSION,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "complete": failed == 0,
        "receipts": receipts,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }


def plan_reclassification(
    *,
    plan_id: str,
    concept_ids: Sequence[str],
    from_type: str,
    to_type: str,
    predicate: str = "is_an_instance_of",
    rationale: str = "",
    ticket: str | None = None,
) -> RepairPlan:
    """Build the add-before-remove reclassification shape.

    Generalised directly from JVNAUTOSCI-2651. The ordering is the point: every
    assertion precedes its matching retraction, so an interrupted run leaves
    concepts carrying both types rather than none.
    """
    steps: list[RepairStep] = []
    for concept_id in concept_ids:
        steps.append(
            RepairStep(
                operation="assert_structural",
                source_id=concept_id,
                predicate=predicate,
                target_id=to_type,
                rationale=f"adopt correct type {to_type}",
            )
        )
        steps.append(
            RepairStep(
                operation="retract_structural",
                source_id=concept_id,
                predicate=predicate,
                target_id=from_type,
                rationale=f"retract incorrect type {from_type}",
            )
        )
    return build_repair_plan(
        plan_id=plan_id,
        rationale=rationale or f"reclassify {from_type} -> {to_type}",
        steps=steps,
        ticket=ticket,
    )


__all__ = [
    "PLAN_FORMAT_VERSION",
    "OntologyRepairPlanError",
    "RepairStep",
    "RepairPlan",
    "build_repair_plan",
    "validate_repair_plan",
    "execute_repair_plan",
    "plan_reclassification",
]
