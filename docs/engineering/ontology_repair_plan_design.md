# Ontology repair plans

- **Kind:** Design and implemented substrate
- **Lifecycle:** Active
- **Authority:** Describes `src/backend/services/ontology_repair_plan_service.py`
  as implemented. It adds no authority route and no principal; the open decision
  about agent-reachable ontology mutation remains
  [JVNAUTOSCI-2653](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2653).
- **Authority scope:** Structural ontology repair proposals in Von engineering work
- **Owner:** Von maintainers
- **Last reviewed:** 20 August 2026
- **Review trigger:** A new operation type, a persistence or transport decision,
  or evidence that the affordance argument below no longer holds
- **State or evidence as of:** 20 August 2026
- **Open questions:** Should plans be persisted and executed by name; should
  non-structural operations be supported; does the propose-without-authority mode
  actually change what Von does in practice?

## What this is

A repair plan is an ordered list of structural ontology changes, each carrying a
precondition and a rationale, identified by a stable digest over its step
content. Building and validating a plan touch no authority and mutate nothing.
Executing one requires an approval bound to that exact digest.

The implementation is `ontology_repair_plan_service.py`. Its operations are
deliberately two: `assert_structural` and `retract_structural`.

## Where the design came from

JVNAUTOSCI-2651 reclassified twelve concepts recorded as instances of
`#V#mcp_tool` that were actually workflow action contracts. That repair was done
by hand, and doing it by hand is what produced this design rather than the
reverse. Four things emerged that the existing single-shot mutation tools cannot
express.

**Ordering is semantic.** Each concept had to gain its correct type before
losing the wrong one, so it was never momentarily untyped. This was not
theoretical: the first execution run had all twelve assertions succeed and all
twelve retractions refused, and the ordering is the only reason that left a
benign both-types state rather than twelve untyped concepts.

**Preconditions decouple proposal from execution.** A plan may be built now and
approved later. Without a precondition per step, a delayed execution acts on a
stale assumption.

**Partial failure safety is a property of order, not of the executor.** No
amount of executor cleverness recovers from a plan whose steps are sequenced
badly. Conversely a well-sequenced plan is safe under an executor that simply
stops.

**The weakest sufficient primitive should be the default.** That run asked for a
hard delete it did not need and was correctly refused for lacking
`operator_override`. `soft_delete` removes the edge identically and additionally
records a tombstone and undo token, which is also what JVNAUTOSCI-2615 requires:
retraction is a lifecycle event, not silent physical deletion. The service now
uses `soft_delete` unconditionally for retraction.

## The affordance argument

The test proposed for this design was that if it offers nothing beyond the
current tools, the design is wrong. Six affordances are genuinely new.

**Propose precisely without authority.** This is the central one. Today an agent
either holds mutation authority and acts, or lacks it and can only describe in
prose. A plan is a third mode: a precise, machine-checkable, executable proposal
produced with no authority at all. `validate_repair_plan` is read-only and
reports exactly which steps would change what, so the proposal carries its own
evidence.

**Approval binds to content, not to capability.** Granting write access
authorises every future write. Approving a digest authorises exactly twenty-four
named edges and nothing else. Substituting, reordering, adding, or removing any
step invalidates the approval, and `execute_repair_plan` refuses outright on a
mismatch. This is a materially different security object from a permission.

**Drift is detected rather than assumed away.** Every step is re-validated
immediately before it runs, so a plan approved yesterday cannot act on a world
that has since moved.

**Idempotent and resumable.** Steps already satisfied are skipped, so an
interrupted run resumes by rerunning the same plan. The hand-written repair
needed this within an hour of being written.

**One reviewable unit.** A plan is diffable, attachable to a ticket, and
reviewable as a whole. Reviewing one plan of twenty-four changes is a different
activity from approving twenty-four operations.

**A reversal record.** Retraction receipts carry undo tokens, so the reversal
path is part of the execution artefact rather than reconstructed afterwards.

## Why this matters for Von

The propose-without-authority mode is the part that changes what Von can be
asked to do. Von can run ontology consistency audits, of exactly the kind that
found the twelve misclassified concepts, and emit plans rather than either
requesting write authority or filing prose. A human reviews and approves a batch.

That scales oversight in the direction that matters: the reviewer's effort grows
with the number of *decisions*, not the number of *edits*. Twelve
misclassifications with one shared rationale is one decision.

It also gives rejection somewhere to land. A refused plan is a precise record of
a change a human did not want, which is far better learning signal than a
refused tool call.

For the federation direction in JVNAUTOSCI-2615, a plan is a proposal that can
cross an authority boundary as ordinary data. It carries its own preconditions
and needs no ambient trust to be transmitted, inspected, or rejected.

## What this deliberately does not do

It adds no route and no principal. Execution calls the same canonical services
an operator maintenance script already calls, so it runs at exactly the
authority that already existed. Nothing here pre-empts JVNAUTOSCI-2653.

It does not persist plans. A plan is currently built in process or committed to
the repository as a script. Persisting plans and executing them by name is the
shape "execute server-stored canonical arguments" would need, and this format is
intended to be that shape, but the decision and its threat model belong to 2653.

It supports structural predicates only. Text relations, concept creation, and
merges are out of scope until a case demands them.

## Honest limits

The digest binds step content, not the world. Two plans with identical steps
have identical digests, which is intended, but it means an approval is
transferable between contexts where those steps mean different things. If plans
are ever persisted or transmitted, the digest should be bound to an issuer and
scope as well.

`already_satisfied` is treated as success. A plan that has been fully applied and
one that was never applicable both report `executable` with zero pending
changes. Callers wanting to distinguish those should read `changes_pending`.

The precondition model knows only about structural predicate arrays. It does not
understand inferred types, cross-concept invariants, or workflow-level
consequences, so a plan can be locally valid and still semantically wrong. It
narrows the class of mistakes; it does not eliminate review.
