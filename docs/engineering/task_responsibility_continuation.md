# Continuing a responsibility from Tasks

- **Kind:** Capability guide
- **Lifecycle:** Active
- **Authority:** Describes task continuity and its canonical references;
  [JVNAUTOSCI-2724](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2724)
  owns the original interface delivery; the continuing paper-email encounter
  is tracked in [JVNAUTOSCI-2244](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2244)
  and [JVNAUTOSCI-2721](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2721).
- **Review trigger:** Changes to task product selection, recurring execution,
  contextual requirements or execution authority.

A task can explicitly link its current work product using
`#V#hascurrentworkproduct`. The relation selects a concept identity. Content,
assertion context and revisions remain on the product; linking it neither copies
its content nor grants access. Existing tasks remain valid without a product.

Select a task in All Tasks, open its details and save the intended product's
concept ID. **Open current work product** reads its current actor-effective
`hasContent` projection. A single non-empty content assertion can be displayed.
Missing, inaccessible or ambiguous references/content are reported without
choosing a neighbouring product or guessing a revision from timestamps. The
concept inspector remains available for accessible products needing revision
resolution. Product reads do not change task status or start work.

**Discuss** opens the existing task-focused conversation path with the accessible
product reference alongside the task. This also works for human review tasks;
it does not transfer their assignment to Von. For an eligible task assigned to
Von, enter a short new instruction and use **Continue with Von**. The ordinary
execution route projects the selected task, canonical product reference,
checkpoint, evidence and instruction into its originating conversation. It
retains the existing creator, organisation and conversation-owner checks.
The model can retrieve and revise the product through its ordinary authorised
tools; the UI does not embed a second product store or prescribe its judgement.

During an active attempt, **Observe Von work** opens the existing conversation.
The queue and TaskExecution remain authoritative, including across reloads or
competing clicks; browser state is only an observation and retry aid. A later
intentional attempt is distinct once the preceding attempt reaches terminal
state. Queue completion does not imply task completion or a successful revision.

The HTTP task detail response and ordinary `task_get` share the product
reference projection. `GET /api/tasks/<id>/work-product` additionally returns the
current body and its SHA-256. `PATCH /api/tasks/<id>` accepts
`current_work_product_concept_id` (null clears the link). The existing execution
endpoint accepts an optional `continuation_instruction` of at most 4,000
characters. Client-supplied product projections do not control the execution
context.

Ordinary chat can use `task_update_fields` to attach the current product, revise
the checkpoint and evidence, and assign an actor-created task to `#V#von_system`
with the actor as its report recipient. This reuses the existing task editor
and product reference. It does not start execution. The bounded ordinary-chat
projection cannot change the creator or organisation, add an audience, or
assign work to another person. The general parity editor remains a separate
explicitly delegated surface.

## Recurring encounters

`#V#task_continuation_workflow` submits the same task operation as **Continue
with Von**, through `task_execution_submission_service`. Its release input is
`src/backend/workflows/repo_seed_bundles/task_continuation_workflow_seed_bundle.json`;
publish it with the existing `bootstrap_repo_seed_workflow_bundle` operator
service and read back the live definition. Repository presence alone is not
activation. An actor-owned schedule supplies the task and an explicit enabled
model/provider; it does not copy the task's domain programme into code.

Each occurrence reads the current task, product reference and checkpoint.
The originating conversation carries its shared situation and prior work.
The workflow's durable instance gives an idempotent launch identity; the queue's
atomic active-task key prevents overlap with both scheduled and UI launches.
An already active task coalesces with the existing execution. The workflow
receipt reports submission or coalescence, with a queue and TaskExecution
locator. Its successful termination is **not** a domain-completion claim.
Inspect the linked execution and canonical work product for that outcome.

## Requirements that can evolve

For a paper responsibility, “fully represented” is relative to the applicable
user, paper class, purpose and current expectation. It is not an immutable
property of the paper, a universal list of predicates, or proof that a named
workflow ran. Bibliographic acquisition, document retrieval, scientific
interpretation, identity reconciliation and source disposition remain
separable capabilities. Choose and improve them against the current job.

Keep the initial expectation as inspectable, revisable text in the working
account. When acquisition, assessment and later audit need a shared independent
reference, give that expectation a represented profile identity and read its
current actor-effective body. Existing representation-contract profiles are
worth inspecting, but their historical tool-requirement contracts do not by
themselves express contextual adequacy. Do not silently reinterpret one as the
new completeness policy or replace its public semantics with one user's needs.

An assessment should preserve which expectation and revision it applied,
the use context, evidence actually inspected, and remaining gaps. The existing
source-processing marker's authority fingerprint can bind the effective
expectation when the marker is used for resumption. A label or an old marker
alone cannot establish currency under a revised expectation. Preserve earlier
evidence and receipts; reassess and fill the demonstrated gap instead of
discarding the representation or downloading everything again. Different uses
may have different adequate representations of the same canonical paper.
Privacy scope remains distinct from semantic applicability; see
[contextual knowledge evolution](contextual_knowledge_evolution.md).

Other guidance that may need revision includes mailbox selection and intent,
coverage and backlog priority, effort per encounter, retry/audit cadence,
model and prompt choice, Inbox disposition and notification preferences.
Start with the existing task/product and reasonable stated defaults. Split
guidance into independently governed profiles only when actual consumers or
revision needs justify it. Keep exact cursor persistence, identity, actor
authority, queue deduplication and effect read-back in reusable mechanisms.

## Evidence of low human burden

A detailed diagnostic prompt is useful engineering evidence, but not evidence
of a natural user experience. Record supplied workflow IDs, repair plans,
manual state fixes and repeated briefing as operator assistance. After repair,
exercise an ordinary request such as “Keep up with papers people email to Von,
including the older ones, and mark each email done when its papers are
represented.” A later short instruction should recover and revise the same
responsibility. Inspect whether Von reuses earlier components, requirements
and evidence, rather than requiring the human to reconstruct their interfaces.
One focussed question may be useful when a material fact is unavailable;
making the user write an execution specification is not the target experience.

This is a bounded continuity interface, not evidence of learned role competence.
Use the [role convergence guide](role_learning_convergence.md) for successive
encounters, outcomes and remaining human burden.
