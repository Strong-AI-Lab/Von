"""Repair plans are proposable without authority and executable only on approval.

The properties pinned here are the ones the manual JVNAUTOSCI-2651 repair showed
were needed and that the single-shot mutation tools cannot express: ordering,
preconditions, drift detection, exact-plan approval, and safe partial failure.
"""

import os

import pytest

os.environ.setdefault("ATLASSIAN_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("ATLASSIAN_EMAIL", "agent@example.com")
os.environ.setdefault("ATLASSIAN_API_TOKEN", "token-for-import")

from src.backend.services.ontology_repair_plan_service import (  # noqa: E402
    OntologyRepairPlanError,
    RepairStep,
    build_repair_plan,
    execute_repair_plan,
    plan_reclassification,
    validate_repair_plan,
)


class _FakeRepo:
    """In-memory stand-in shaped like ConceptsRepository for these calls."""

    def __init__(self, concepts):
        self.concepts = {cid: dict(rels) for cid, rels in concepts.items()}

    def find_one(self, query, projection=None):
        cid = query.get("concept_id")
        if cid not in self.concepts:
            return None
        return {"concept_id": cid, "relationships": self.concepts[cid]}


def _repo_with_misclassification(n=2):
    concepts = {"#V#wrong_type": {}, "#V#right_type": {}}
    for i in range(n):
        concepts[f"#V#thing_{i}"] = {"is_an_instance_of": ["#V#wrong_type"]}
    return _FakeRepo(concepts)


# ---------------------------------------------------------
# Plan construction and identity
# ---------------------------------------------------------


def test_plan_orders_every_assertion_before_its_retraction():
    """Safe partial failure is a property of ordering, not of the executor."""
    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#a", "#V#b"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )

    ops = [(s.operation, s.source_id) for s in plan.steps]
    assert ops == [
        ("assert_structural", "#V#a"),
        ("retract_structural", "#V#a"),
        ("assert_structural", "#V#b"),
        ("retract_structural", "#V#b"),
    ]


def test_digest_is_stable_across_rebuilds():
    kwargs = dict(
        plan_id="p1",
        concept_ids=["#V#a"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )
    assert plan_reclassification(**kwargs).digest == plan_reclassification(**kwargs).digest


def test_digest_ignores_prose_so_review_survives_re_explanation():
    a = build_repair_plan(
        plan_id="p1",
        rationale="first wording",
        steps=[RepairStep("assert_structural", "#V#a", "is_an_instance_of", "#V#t", "why")],
    )
    b = build_repair_plan(
        plan_id="p1",
        rationale="different wording",
        steps=[RepairStep("assert_structural", "#V#a", "is_an_instance_of", "#V#t", "other")],
    )
    assert a.digest == b.digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ids: ids + ["#V#extra"],
        lambda ids: list(reversed(ids)),
    ],
)
def test_digest_changes_when_the_steps_change(mutate):
    base = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#a", "#V#b"],
        from_type="#V#w",
        to_type="#V#r",
    )
    other = plan_reclassification(
        plan_id="p1",
        concept_ids=mutate(["#V#a", "#V#b"]),
        from_type="#V#w",
        to_type="#V#r",
    )
    assert base.digest != other.digest


def test_malformed_plans_are_refused():
    with pytest.raises(OntologyRepairPlanError):
        build_repair_plan(plan_id="p", rationale="", steps=[])
    with pytest.raises(OntologyRepairPlanError):
        build_repair_plan(
            plan_id="p",
            rationale="",
            steps=[RepairStep("delete_everything", "#V#a", "p", "#V#b")],
        )


# ---------------------------------------------------------
# Validation is read-only and order-aware
# ---------------------------------------------------------


def test_validation_does_not_mutate_anything():
    repo = _repo_with_misclassification()
    before = {cid: dict(rels) for cid, rels in repo.concepts.items()}

    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )
    validate_repair_plan(plan, repo=repo)

    assert repo.concepts == before


def test_add_before_remove_validates_as_ready_not_stranded():
    """Order-aware simulation is what stops a false stranding report."""
    repo = _repo_with_misclassification()
    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )

    report = validate_repair_plan(plan, repo=repo)

    assert report["executable"] is True
    assert [s["status"] for s in report["steps"]] == ["ready", "ready"]


def test_a_retraction_that_would_stranded_a_concept_is_drifted():
    repo = _repo_with_misclassification()
    plan = build_repair_plan(
        plan_id="p1",
        rationale="retract without replacing",
        steps=[
            RepairStep(
                "retract_structural", "#V#thing_0", "is_an_instance_of", "#V#wrong_type"
            )
        ],
    )

    report = validate_repair_plan(plan, repo=repo)

    assert report["executable"] is False
    assert report["steps"][0]["status"] == "drifted"
    assert "no is_an_instance_of" in report["steps"][0]["detail"]


def test_missing_concepts_block_rather_than_fail_at_execution():
    repo = _repo_with_misclassification()
    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#does_not_exist"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )

    report = validate_repair_plan(plan, repo=repo)

    assert report["executable"] is False
    assert report["steps"][0]["status"] == "blocked"


def test_an_already_repaired_world_reports_no_pending_changes():
    repo = _FakeRepo(
        {
            "#V#wrong_type": {},
            "#V#right_type": {},
            "#V#thing_0": {"is_an_instance_of": ["#V#right_type"]},
        }
    )
    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )

    report = validate_repair_plan(plan, repo=repo)

    assert report["changes_pending"] == 0
    assert report["executable"] is True
    assert all(s["status"] == "already_satisfied" for s in report["steps"])


# ---------------------------------------------------------
# Approval binds to the exact plan
# ---------------------------------------------------------


def test_execution_refuses_an_approval_for_different_steps():
    approved = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#a"],
        from_type="#V#w",
        to_type="#V#r",
    )
    substituted = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#a", "#V#victim"],
        from_type="#V#w",
        to_type="#V#r",
    )

    with pytest.raises(OntologyRepairPlanError, match="does not match"):
        execute_repair_plan(substituted, approved_digest=approved.digest)


def test_execution_refuses_an_empty_or_wrong_digest():
    plan = plan_reclassification(
        plan_id="p1", concept_ids=["#V#a"], from_type="#V#w", to_type="#V#r"
    )
    for bad in ("", "0" * 64):
        with pytest.raises(OntologyRepairPlanError):
            execute_repair_plan(plan, approved_digest=bad)


# ---------------------------------------------------------
# Execution behaviour
# ---------------------------------------------------------


@pytest.fixture
def patched_mutations(monkeypatch):
    """Route the canonical services at the fake repo."""
    calls = {"asserts": [], "retracts": []}

    def _add(source_id, predicate, target_id, **_kw):
        calls["asserts"].append((source_id, predicate, target_id))
        rels = _add.repo.concepts.setdefault(source_id, {})
        rels.setdefault(predicate, [])
        if target_id not in rels[predicate]:
            rels[predicate].append(target_id)
        return {"success": True}

    def _remove(*, source_id, predicate, target, mode, **_kw):
        calls["retracts"].append((source_id, predicate, target, mode))
        rels = _remove.repo.concepts.get(source_id, {})
        if target in rels.get(predicate, []):
            rels[predicate].remove(target)
        return {"success": True, "undo_token": f"undo:{source_id}"}

    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_structural_relationship",
        _add,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.remove_relationship",
        _remove,
    )
    return calls, _add, _remove


def test_execution_applies_in_order_and_reads_back(patched_mutations):
    calls, _add, _remove = patched_mutations
    repo = _repo_with_misclassification(n=2)
    _add.repo = _remove.repo = repo

    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0", "#V#thing_1"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )
    result = execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)

    assert result["complete"] is True
    assert result["applied"] == 4
    assert all(r["verified"] for r in result["receipts"])
    for cid in ("#V#thing_0", "#V#thing_1"):
        assert repo.concepts[cid]["is_an_instance_of"] == ["#V#right_type"]


def test_retraction_uses_the_weakest_primitive(patched_mutations):
    """soft_delete removes the edge and leaves a tombstone; nothing needs more."""
    calls, _add, _remove = patched_mutations
    repo = _repo_with_misclassification(n=1)
    _add.repo = _remove.repo = repo

    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )
    execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)

    assert all(mode == "soft_delete" for *_rest, mode in calls["retracts"])


def test_execution_captures_undo_tokens(patched_mutations):
    calls, _add, _remove = patched_mutations
    repo = _repo_with_misclassification(n=1)
    _add.repo = _remove.repo = repo

    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )
    result = execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)

    retraction = [r for r in result["receipts"] if r["step"]["operation"] == "retract_structural"]
    assert retraction[0]["result"]["undo_token"] == "undo:#V#thing_0"


def test_execution_is_idempotent_and_resumable(patched_mutations):
    calls, _add, _remove = patched_mutations
    repo = _repo_with_misclassification(n=1)
    _add.repo = _remove.repo = repo

    plan = plan_reclassification(
        plan_id="p1",
        concept_ids=["#V#thing_0"],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
    )
    execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)
    second = execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)

    assert second["applied"] == 0
    assert second["skipped"] == 2
    assert second["complete"] is True


def test_drift_since_approval_is_refused_not_applied(patched_mutations):
    """A plan approved earlier must not act on an assumption that has expired."""
    calls, _add, _remove = patched_mutations
    repo = _FakeRepo(
        {
            "#V#wrong_type": {},
            "#V#right_type": {},
            # Someone else already removed the wrong type and left nothing else.
            "#V#thing_0": {"is_an_instance_of": []},
        }
    )
    _add.repo = _remove.repo = repo

    plan = build_repair_plan(
        plan_id="p1",
        rationale="retract only",
        steps=[
            RepairStep(
                "retract_structural", "#V#thing_0", "is_an_instance_of", "#V#wrong_type"
            )
        ],
    )
    result = execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)

    # Already absent, so nothing to do and nothing broken.
    assert result["receipts"][0]["outcome"] == "skipped"
    assert calls["retracts"] == []


def test_a_blocked_step_does_not_stop_the_rest_from_being_reported(patched_mutations):
    calls, _add, _remove = patched_mutations
    repo = _repo_with_misclassification(n=1)
    _add.repo = _remove.repo = repo

    plan = build_repair_plan(
        plan_id="p1",
        rationale="one good, one impossible",
        steps=[
            RepairStep("assert_structural", "#V#ghost", "is_an_instance_of", "#V#right_type"),
            RepairStep("assert_structural", "#V#thing_0", "is_an_instance_of", "#V#right_type"),
        ],
    )
    result = execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)

    assert result["receipts"][0]["outcome"] == "refused"
    assert result["receipts"][1]["outcome"] == "applied"
    assert result["complete"] is False


def test_the_2651_shape_reproduces_the_manual_repair(patched_mutations):
    """The plan form must express what the hand-written repair did."""
    calls, _add, _remove = patched_mutations
    repo = _repo_with_misclassification(n=12)
    _add.repo = _remove.repo = repo

    plan = plan_reclassification(
        plan_id="JVNAUTOSCI-2651",
        concept_ids=[f"#V#thing_{i}" for i in range(12)],
        from_type="#V#wrong_type",
        to_type="#V#right_type",
        ticket="JVNAUTOSCI-2651",
    )

    report = validate_repair_plan(plan, repo=repo)
    assert report["executable"] is True
    assert report["changes_pending"] == 24

    result = execute_repair_plan(plan, approved_digest=plan.digest, repo=repo)
    assert result["applied"] == 24
    assert result["complete"] is True
    assert all(
        repo.concepts[f"#V#thing_{i}"]["is_an_instance_of"] == ["#V#right_type"]
        for i in range(12)
    )
