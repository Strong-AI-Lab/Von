# JVNAUTOSCI-1962 Enduring Memory Architecture - 23 April 2026

> **Document status: Long-horizon design with a dated implementation snapshot.**
> The design may remain useful. Claims using “current”, “already”, “still”,
> source anchors, and task priority describe the April 2026 evidence base and
> require live revalidation. This is not release-status authority.

## Purpose

This note records the concrete enduring-memory architecture for Von after the
main-turn memory landing in `JVNAUTOSCI-1988` and the self-improvement/profile
landings through `JVNAUTOSCI-1994` and `JVNAUTOSCI-1993`.

`JVNAUTOSCI-1962` is not a task to "add memory" in the abstract. The live code
already has multiple memory-capable substrates. The actual gap is that Von
still lacks one integrated architecture across semantic, episodic, procedural,
and policy memory with explicit:

- represented-vs-virtual boundaries;
- promotion and revision rules;
- provenance and evidence handling; and
- namespace and visibility rules.

This note therefore does two narrower things:

1. states the memory architecture that now matches the live code and the design
   intent in `docs/engineering/Von_for_AgenticAI.md`; and
2. turns that architecture into the first concrete implementation slices rather
   than leaving it as a prose umbrella.

## Current Live Substrate

The current Von code already contains the following durable or memory-adjacent
surfaces:

- Main-turn memory context resolution and rendering in
  `src/backend/services/conversation_turn_memory_context_service.py:547`.
- Context bundles, context dossiers, report revisions, reconstructed
  workspaces, and evidence receipts in
  `src/backend/services/context_bundle_service.py:622`,
  `:998`, `:1296`, and `:1541`.
- Durable critique-memory persistence and projection in
  `src/backend/services/episode_critique_memory_service.py:1723`.
- Workflow-use episode logging and aggregate recomputation in
  `src/backend/services/workflow_episode_service.py:395` and `:472`.
- Proposal-addressable workflow authoring and promotion-evaluation storage in
  `src/backend/workflows/workflow_studio_service.py:624`, `:772`, and `:2000`.
- Represented self-improvement profile loading in
  `src/backend/services/episode_self_improvement_profile_vontology_service.py:286`.

The problem is not absence. The problem is fragmentation. These surfaces still
behave more like adjacent subsystems than one governed enduring-memory
architecture.

## Architecture Decision

### 1. Memory Strata Stay Distinct

Von should keep four explicit enduring-memory strata:

| Stratum | Canonical durable authority | What should be represented | What may remain virtual or raw-backed |
| --- | --- | --- | --- |
| Semantic | Vontology concepts, relations, texts, and other KB assertions | Durable facts, stable entity links, promoted hypotheses, revision records | Joined retrieval views, transient search expansions, query-specific synthesis |
| Episodic | Turn records, workflow-use episodes, critique memories, dossiers, report revisions, evidence receipts | Critique memories, dossiers, report revisions, evidence receipts, explicit durable episode projections | High-volume raw turn logs, reconstructed workspaces, aggregate counters, query manifests |
| Procedural | Workflows, prompts, validators, launch/routing profiles, publication lifecycle, authoring proposals | Published artefacts, proposal records, promotion evaluations, procedural revision lineage | Preview graphs, diffs, local experiment scaffolds, other bounded pre-publication views |
| Policy | Profiles, guidelines, routing or context policy artefacts, evaluator-derived guidance objects | Represented profile revisions and durable guidance objects | Per-query ranking views, transient scoring outputs, local diagnostic summaries |

The engineering consequence is simple: Von should stop treating "memory" as one
flat retrieval pool. Query surfaces must preserve stratum, provenance, and
scope.

### 2. Not Everything Should Be Materialised the Same Way

Some artefacts should be durable represented individuals or relations in
Vontology. Others should remain virtual or raw-store-backed and only surface as
typed references.

Represent by default when the artefact is:

- evidence-backed;
- reusable across sessions or workflows;
- worth revising later;
- needed as an auditable promotion or publication record; or
- needed as a stable reference target for later evaluators or workflows.

Allow raw-backed or virtual views when the artefact is:

- high-volume episode telemetry whose primary role is replay or audit;
- a derived aggregation or join;
- a bounded workspace reconstruction view;
- a query-time manifest assembled from authoritative durable sources; or
- an ephemeral experiment-local intermediate that has not yet passed promotion.

This means Von should not mirror every raw episode into Vontology just to claim
"represented memory". It should instead expose raw-backed stores through typed,
governed references and promote only the durable reusable slices.

### 3. Every Queryable Memory Reference Needs a Common Contract

Across all strata, every durable artefact or queryable memory reference should
carry, directly or by support-surface projection:

- `memory_stratum`
- `artefact_kind`
- `represented` vs `virtual`
- `subject_concept_ids` and/or `workflow_ids`
- `namespace`
- `user_concept_id`
- `organisation_concept_id`
- `visibility_scope`
- `source_memory_ids` or episode anchors
- `evidence_receipt_ids`
- `revision_lineage` or predecessor/supersession pointers
- `created_at_utc` and `updated_at_utc`

This is the contract the current code lacks. Turn-memory, evaluator, and
self-improvement paths still use separate local knowledge about where memory
lives.

### 4. Promotion Must Become an Explicit Cross-Strata Workflow

The durable promotion path should be:

- `episodic -> semantic`
  Promote evidence-backed facts, assertions, or corrected beliefs into
  Vontology/KB, preserving provenance and retraction pathways.
- `episodic -> procedural`
  Promote repeated successful or repeatedly failing traces into workflow,
  prompt, validator, or other procedural revision proposals with evaluation and
  publication gates.
- `episodic -> policy`
  Promote repeated critique/evaluator signals into represented profile or
  guidance revisions rather than leaving them only as local suggestions.

For all three:

- promotion candidates should be explicit durable objects or equivalent
  represented workflow state;
- acceptance and rejection should both preserve provenance;
- missing evidence, missing namespace clarity, or missing authoritative publish
  surfaces should fail closed; and
- revision should create lineage, not silent overwrite.

### 5. Namespace and Visibility Are Part of the Memory Model

The authoritative namespace contract in
`docs/engineering/effective_namespace_contract.md` applies to memory artefacts
as well, not only to RAG or request routing.

Durable memory should therefore:

- preserve both the namespace string and its user/org components;
- support both `#V#user` and `#V#user@org` scopes;
- avoid silently degrading org-scoped memory to user-only scope;
- make shared/global visibility explicit rather than implicit; and
- fail closed when a user-scoped memory operation lacks required scope data.

This matters because enduring memory is not just "what is stored". It is also
who may reuse it and under what scope.

### 6. Query Surfaces Should Become First-Class Support Surfaces

The intended support-surface stack is:

1. a canonical enduring-memory manifest/query layer across the existing live
   memory stores and represented artefacts;
2. turn-context construction that can consume that manifest instead of bespoke
   point lookups;
3. promotion workflows that reference the same stable memory IDs or manifests;
   and
4. evaluators and benchmark worlds that score memory use against those stable
   references.

This preserves represented authority while keeping Python in its correct role:
federation, validation, telemetry, persistence, and other reusable execution
support.

## Evaluation Requirements

The enduring-memory architecture should be evaluated on tasks that actually
require long-horizon memory, not just explicit recall.

Minimum acceptance dimensions:

- buried evidence retrieval
- task-state reconstruction after interruption
- preference or policy grounding across sessions
- knowledge update and retraction handling
- abstention or graceful recovery when memory is absent
- procedural reuse or proposal quality from prior episodes

Acceptance should combine:

- repeatable benchmark-style cases for comparison against simple baselines; and
- a smaller replay or nearest-real-path acceptance set on the real Von path.

## Short Literature Note

The architecture direction here was checked against recent primary sources:

- [Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers](https://arxiv.org/abs/2603.07670)
  reinforced treating memory as a write-manage-read architecture over temporal
  scope, representational substrate, and control policy. This directly supports
  a cross-strata architecture task rather than another local retrieval feature.
- [E-mem: Multi-agent based Episodic Context Reconstruction for LLM Agent Memory](https://arxiv.org/abs/2601.21714)
  reinforced that some memory value is lost when everything is flattened into
  decontextualised retrieval artefacts. This supports preserving raw or
  reconstructable episodic context instead of materialising everything into one
  flat store.
- [MemoryArena: Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks](https://arxiv.org/abs/2602.16313)
  reinforced that memory and action must be evaluated together. This is why the
  evaluation slice here includes task-state reconstruction and multi-session use
  rather than only recall.
- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813)
  reinforced the need to evaluate knowledge updates, temporal reasoning, and
  abstention, not just retrieval hit rate.
- [ProcMEM: Learning Reusable Procedural Memory from Experience via Non-Parametric PPO for LLM Agents](https://arxiv.org/abs/2602.01869)
  reinforced the direction of promoting episodic traces into reusable procedural
  artefacts with explicit executability and gating.

These papers did not overturn Von's design direction. They sharpened the
represented-vs-virtual distinction, the promotion architecture, and the need
for stronger long-horizon evaluation.

## First Implementation Slices

`JVNAUTOSCI-1962` now decomposes into the following concrete tasks:

1. `JVNAUTOSCI-1995`
   Implement a canonical enduring-memory manifest and query surface across
   dossiers, workflow-use episodes, critique memories, and durable artefacts.
2. `JVNAUTOSCI-1996`
   Implement represented memory-promotion and revision workflows from episodic
   evidence into semantic, procedural, and policy memory.
3. `JVNAUTOSCI-1997`
   Add a long-horizon enduring-memory evaluation and task-state reconstruction
   harness on the real Von path.

These tasks are intentionally not replacements for `JVNAUTOSCI-1964` or
`JVNAUTOSCI-1963`. They are the concrete continuation of the enduring-memory
line that those broader evaluator and self-improvement umbrellas should now
consume.

## Open Questions Deferred

This note does not settle everything. The following remain intentionally open
for later implementation work:

- whether some episodic raw stores should gain richer represented projections
  over time, rather than only a federated manifest layer;
- how much of semantic promotion should be handled through existing assertion
  pathways versus new represented promotion records;
- how far policy memory should unify profile, guideline, and evaluator-derived
  guidance objects; and
- whether certain benchmark or experiment-local memory artefacts should expire
  automatically rather than entering durable lineage.

Those are implementation questions for the follow-on tasks above, not reasons to
leave the architecture implicit.
