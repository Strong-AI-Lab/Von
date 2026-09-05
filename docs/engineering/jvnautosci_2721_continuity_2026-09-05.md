# Continuing responsibility: bounded integration evidence

- **Kind:** Implementation and developmental evidence record
- **Lifecycle:** Frozen
- **Authority:** Evidence only; current selection and delivery status live in [JVNAUTOSCI-2721](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2721)
- **Owner:** Michael Witbrock
- **Evidence date:** 5 September 2026 UTC
- **Review trigger:** Changed workflow, prompt, tool contract or model; prospective organisational use
- **Design direction:** [Role-learning convergence](role_learning_convergence.md)

This increment connects a saved responsibility to fresh evidence, revised
conclusions and a verified updated work product. It extends the existing
`#V#lab_project_status_digest_workflow`; it does not create a second role or
learning platform. It does not establish real lab adoption, learned benefit,
cross-domain transfer or autonomous monitoring by a deployed worker.

## Findings and changes

The inspected baseline was `00f9acb5`, including completed 2720. That experiment
rejected its tested advice candidate and verified subsequent absence. This
increment preserves that result: it neither activates the candidate nor
introduces an ordinary advice consumer.

Four concrete integration problems impeded continuing responsibility:

1. The digest always queried the latest 12 JVNAUTOSCI issues and used one fixed
   work-product ID. Callers could not select separate responsibilities; an older
   unresolved issue could fall outside the evidence window.
2. Prior-digest reads disabled Markdown reconstruction. With text stored in
   `hasContent` relations, the next encounter did not receive the saved text.
3. The content tool description explicitly recommended the metadata-only mode
   to avoid masking missing Markdown. A real model followed that advice and
   reported readable stored sources as empty.
4. Completion checked for nonempty read-back, rather than the exact written
   content. Connecting the existing exact-text verifier also exposed a writer/
   verifier disagreement between `hasContent` and its equivalent concept ID.

The release seed now accepts an existing authorised `work_product_id`, an
initial `jira_jql`, and `evidence_sources` describing source locators and their
purpose. Omission preserves the old work-product and Jira-query defaults.
An initial scope state uses the existing context-set primitive. Both foreground
and scheduled launches use the same inputs and capability.

The existing tool-capable model step can investigate material source references,
older obligations and missing facts. It has read capabilities; the deterministic
workflow owns the selected work-product write. Prompt output cannot redirect
that write to another product. The prompt preserves commitment identity across
date/owner changes, distinguishes internal/external deadlines and estimates/
approvals, and asks for refreshed evidence before a premise is repeated as
current. Unavailable evidence must remain qualified, not silently promoted.

Persistence uses the same actor-bound canonical singleton-text service and the
existing exact-text verifier. The only verifier algorithm change recognises
equivalent core predicate IDs; unrelated predicate mismatches remain invalid.
A new continuity prompt identity preserves locally authored old prompt text.
Seed version 8 recognises the reviewed version-7 authority digest; upgrade and
repeat-bootstrap tests read back the resulting authority and preserve existing
digest text. This is a release input, not evidence of shared runtime activation.

## Exercised path and observations

The [development rehearsal](role_convergence_rehearsal.md) runs the actual
represented workflow, model/tool loop and canonical text services in an
in-process mock database. Jira and repository sources are fictional adapters.
Each encounter starts afresh, with source locators and the saved work product,
without a conversation transcript or future-event text.

The [evidence extract](../generated/role_continuity_2026-09-05.json) retains exact
canonical brief text, retrieved source IDs, source/asset digests, model identity
triples, per-call reported token usage and individual elapsed times. Review was
Codex inspection of those artefacts and sources, not a blind efficacy evaluator.

| Encounter | Observed outcome after the repair |
|---|---|
| A | Reads call, office and project sources; distinguishes 23 October internal and 30 October external deadlines; retains open TASK-1 and missing compute cost/statement |
| B | Recovers the previous brief; reads office-v2; revises the internal deadline to 20 October while retaining 30 October externally and the same open commitment |
| C | Retrieves the newly available 8,000-unit quote; revises the cost/readiness conclusion, retains missing budget/statement work and distinguishes the quote from approval or spending authority |

The corrected path passed A–C once with GPT-5.5 and once with Luna. All six
encounters passed exact canonical saved-text read-back. GPT-5.5 explicitly
identified the old project source's compute-cost unknown as stale/conflicting
with the quote. The partial empty Jira window did not resolve the obligation.
Only the selected internal brief was written; no external systems or task
statuses were changed.

A further GPT-5.5 A–C sequence joined B to the existing scheduler, worker claim,
production definition loader, actor binding and terminal-state persistence.
The worker completed the changed-deadline brief and the canonical instance was
`completed`; a second due-schedule poll left exactly one occurrence. C then
recovered that saved brief and incorporated the quote. This sequence took
55.93 seconds and reported 142,087 total tokens across calls. It establishes
the isolated scheduled path, not unattended operation in a running lab.

These are developmental observations, not an estimated success rate. Initial
Luna output retained the superseded internal deadline; initial GPT-5.5 output
followed the misleading metadata-only tool description. Both failed work
products remain in the extract. Earlier preflights encountered incompatible
model/API parameters and missing standard backfill prompt fixtures. An Astra
attempt failed at the API interface and supplies no model-quality comparison.
The successful calls used the recorded Chat Completions surface with reasoning
set to `none`; requesting another surface did not itself establish its use.

A later Luna sequence omitted office/project reads and honestly labelled the
corresponding knowledge unavailable. It therefore failed full A–C coverage,
despite exact persistence. This partial result remains in the extract. Joined
rehearsal setup also exposed harness mistakes: absent actor binding, an
executor-only invocation leaving its instance pending, and an incompatible
definition-loader signature. The final run uses the existing worker and its
production loader, rather than manually declaring completion. No production
worker patch was needed. These observations limit any reliability claim;
successful developmental runs are not a claim that every recurrence succeeds.

The foreground GPT-5.5 sequence took 54.38 seconds; Luna took 37.06 seconds.
Reported per-call total tokens sum to 158,663 and 129,628 respectively. These
are substantial context costs for small briefs, not a speed or cost advantage
claim; monetary cost is unavailable. Setup, fixture repair, prompt/tool-contract
repair and review effort are excluded from those execution times. No human
reply was required during either final three-encounter sequence. The briefs
remain repetitive and sometimes defer decisions a capable manager could make;
that limits the low-imposition claim.

## Verification and limits

The targeted execution, seed, verifier and MCP-action suites passed 34 tests.
They include separate work products, actual prior-text recovery, changed text
replacement, exact stale-readback rejection, preservation of an unrelated
product, denial of another actor's private product, model-reported target-ID
mismatch, reviewed seed upgrade, preserved old prompt and digest content, and
a due schedule claimed and completed by the existing worker, with canonical
terminal read-back and no duplicate occurrence on a second poll.
Scripted synthesis in these tests proves integration contracts, not reasoning.

A broader run had 119 passes and 11 worker-claim failures. The identical 11
failures reproduce on archived unchanged `00f9acb5` with this machine's
`VON_DURABLE_MIN_WORKER_BUILD=durable_exact_workflow_authority_snapshot.v1`.
Without that local setting the archived suite passes all 97 tests. The failures
are a baseline/configuration interaction, not a regression caused by this
candidate; they are not repaired or hidden by changing the user's configuration.

The work-product concept must already exist and the actor must have its actual
write authority. A source ID, role description or schedule input grants none.
The shared workflow, prompt, model settings, database and running services were
not activated or changed by these isolated rehearsals. A real operator/source
selection, deployed worker operation and interruption/resumption evidence remain
separate claims. Initial deterministic source-tool failures still stop this
workflow; the exercised incomplete/contradictory-source case was a returned
partial source window and conflicting content, not a connector outage.

For the next missing connection and current delivery decision, use the live
2721 task rather than turning this dated record into a second implementation
plan. Learned reuse must use a fitting source-derived candidate and the 2720
outcome/disposition protocol; this continuity result is not evidence that the
rejected candidate should be restored.
