# Continuing a responsibility from Tasks

- **Kind:** Capability guide
- **Lifecycle:** Active
- **Authority:** Describes the Tasks interface and its canonical references;
  [JVNAUTOSCI-2724](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2724)
  owns delivery decisions and acceptance evidence.
- **Review trigger:** Changes to task product selection or execution authority.

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

This is a bounded continuity interface, not evidence of learned role competence.
Use the [role convergence guide](role_learning_convergence.md) for successive
encounters, outcomes and remaining human burden.
