# Contextual Knowledge Evolution

- **Kind:** Design guide
- **Lifecycle:** Active
- **Authority:** Advisory under the contextual-degrees-of-freedom invariant in
  [`AGENTS.md`](../../AGENTS.md)
- **Authority scope:** Durable assertions, context-sensitive retrieval,
  hypotheses, knowledge publication or promotion, and actor-, organisation-,
  project-, source-, theory-, or time-relative knowledge
- **Owner:** Von maintainers
- **Last reviewed:** 29 July 2026
- **Review trigger:** Adoption of a first-class context/theory model, material
  multi-tenant scale evidence, material changes to the named implementation
  surfaces, or a change to assertion, namespace, inclusion, or publication
  semantics
- **Live implementation evidence:** Revalidate the services named in section 4;
  the map is a 29 July 2026 observation, not a permanent architecture contract

## 1. When to use this guide

Use it when a change does one or more of the following:

- creates or changes durable facts, claims, hypotheses, uncertain relations, or
  their lifecycle;
- combines knowledge sources during a read;
- introduces or changes context, theory, dossier, inclusion, inheritance,
  promotion, publication, endorsement, or conflict semantics;
- makes user, organisation, project, document, workflow, experiment, or time
  scope part of assertion meaning; or
- changes what “canonical”, “global”, “shared”, or “visible” means for
  represented knowledge.

Do not use it for an ordinary UI, transport, telemetry, cache, raw document, or
transactional-data change that leaves represented meaning unchanged.

This is a trigger for a small design check, not an architecture approval gate.

## 2. Direction without a selected context architecture

Von should preserve these distinctions when they matter to the current user
job:

| Dimension | Question |
|---|---|
| Concept identity | Which entity or predicate is being discussed? |
| Assertion context | Under which assumptions, theory, situation, or viewpoint does the assertion hold? |
| Provenance | Who or what supplied the assertion, through which evidence and process? |
| Audience and authority | Who may discover, read, change, endorse, or publish it? |
| Publication or endorsement | Has a person or group adopted it into a named shared context? |
| Lifecycle and confidence | Is it proposed, active, contradicted, retracted, expired, or uncertain? |
| Physical storage | Which collection, graph, document, or index currently holds it? |

The dimensions need not become seven fields on every record. A bounded
capability may use an implicit context, a fixed audience, or a separate store.
Keep that simplifying assumption explicit and local enough that a later
mechanism can replace it without reinterpreting domain meaning.

In particular:

- stable Vontology concept identity does not imply context-free truth;
- a shared or base publication context is a context, not “the one true theory”;
- semantic context inclusion must not grant access or reveal a private
  context's existence;
- visibility does not imply endorsement, and provenance does not imply truth;
- a collection boundary may implement a distinction but does not define its
  semantics; and
- microtheories, named graphs, assertion deltas, embedded theory slices, and
  separate stores remain candidate mechanisms, not doctrine.

Qualify “canonical” when using it. Say canonical **concept identity**, live
**authority**, base **publication**, or durable **read-back**, as applicable.

## 3. Four questions for a coding agent

For a triggered change, ask only:

1. What user outcome is being improved, and what is the generic semantic
   failure behind the immediate symptom?
2. Which current assertion, context, theory, provenance, and publication
   surfaces already carry the relevant meaning?
3. Which distinctions in section 2 matter now, and which are deliberately
   simplified?
4. What is the smallest seam that preserves today's job, and what evidence or
   capability should trigger reconsideration?

Reusing, extending, adapting, or temporarily diverging from a current mechanism
are possible answers, not required labels. When material, record the semantic
distinction being simplified, the existing mechanism or seam used, and the
evidence or capability that should trigger reconsideration. Omit anything
irrelevant.

Do not generalise merely because a general future is imaginable. Conversely,
do not let the nearest collection, namespace, or acceptance test silently
decide eventual semantics. Test assertion meaning at the boundary making the
claim.

## 4. Current starting points — verify live

As observed on 29 July 2026, related capabilities are distributed across:

- base concept and text relations in `concept_service.py`,
  `concept_relation_service.py`, and `text_value_service.py`;
- actor- and organisation-visible assertion deltas in
  `scoped_assertion_service.py`;
- local assertions, lifecycle, diff, promotion, rollback, and stored inclusion
  identifiers in `testing_theory_service.py`;
- evidence, hypotheses, branches, and theory references in
  `context_bundle_service.py`; and
- uncertainty and promotion state in `uncertain_relationship_service.py`.

These are working surfaces, not proof of five distinct domain ontologies. Before
adding a sibling service or schema, inspect whether the job can reuse or adapt
one of them.

Material current limitations include:

- the scoped-assertion audience currently doubles as an implicit context;
- Testing Theory inclusion identifiers do not yet constitute a general
  inherited query model;
- storage-specific metadata is exposed by compatibility adapters; and
- no common conflict, precedence, lifting, or context-selection semantics have
  been selected.

## 5. Coding, testing, and Jira consequences

Reusable reads should expose the semantic view and lineage a caller needs,
rather than making collection order the public contract. Preserve established
generic contracts where possible, and select any actor-relative view at a
trusted authority boundary.

Policy-neutral Python may provide bounded persistence, retrieval, lineage,
validation, access enforcement, telemetry, and adapters. Authored rules for
choosing contexts, resolving conflicts, or promoting knowledge belong in
Vontology, VWL, or another independently governed semantic surface when a
capability actually needs them.

Do not derive authority from context inclusion. Resolve the actor's maximum
visibility independently, then compute meaning only over authorised contexts.
Counts, lineage, conflicts, and error messages must not disclose inaccessible
contexts.

Select only the claims material to the capability. Useful tests may show that:

- base and actor-relative assertions remain distinguishable;
- provenance and source-context lineage survive an adapter;
- direct and inherited assertions are labelled when inheritance exists;
- conflict is not silently flattened;
- promotion creates a provenance-bearing assertion in the target context; or
- inclusion never enlarges visibility.

Storage and index tests may still name collections where they protect an
adapter, migration, or Atlas query contract. They must not be the only evidence
for the knowledge behaviour.

Add one neighbouring context case only when the claimed failure class is meant
to generalise. Require scale or query-plan evidence only for a scale claim.

In Jira, name the generic failure as well as the domain symptom when that
distinction will help future work. A bounded issue may be Done when its user
outcome works; “Done” must not imply that Von's general theory architecture has
been selected. Link related work when useful, but do not make an architecture
programme or follow-up issue a prerequisite for a bounded repair.

## 6. When more general machinery earns its cost

Consider a first-class context substrate when a current capability needs it or
evidence shows recurring:

- incompatible overlays or assertion stores;
- inability to explain direct versus inherited knowledge;
- real user, project, paper, hypothesis, or time-relative conflicts;
- migrations caused by conflating context, authority, provenance, and
  publication; or
- query, authoring, or evaluation failures that a flat model cannot adequately
  repair.

Compare the proposed mechanism with the current flat or adapter baseline on the
complete user job: answer quality, lineage, conflict behaviour, non-leakage,
latency, Atlas cost, storage growth, migration cost, and authoring burden.

## 7. Explicit exclusions

This guide does not require:

- universal context identifiers, automatic theory creation, a universal
  assertion envelope, or a reasoner;
- a mandated store, migration, or logical formalism;
- an automatic Jira, review, build, or test gate; or
- delay to a useful bounded repair while a general architecture is unsettled.

## 8. Related guidance

- [Maintaining global design constraints and authority alignment](maintaining_global_design_constraints_and_authority_alignment_with_coding_agents.md)
  covers the broader “one more local case” and test-pinning failure patterns.
- [Security considerations](security_considerations.md) governs actor,
  namespace, and private-data authority.
- [Agent memory and enduring knowledge](agent_memory_and_enduring_knowledge.md)
  governs promotion into durable memory.
- [Testing workflows and ephemeral theories](testing_workflows_ephemeral_theories_design.md)
  and [Vontology tooling from KA/KR literature](vontology_tooling_from_ka_kcap_kr_literature.md)
  are research and design inputs, not current implementation authority.
