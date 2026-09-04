# Represented Advice for Adaptive Von Behaviour

- **Kind:** Design proposal
- **Lifecycle:** Draft
- **Authority:** Advisory proposal under `AGENTS.md`; it does not define live
  runtime behaviour, grant effect authority, or make advice retrieval mandatory
- **Authority scope:** Von-authored defeasible guidance for role, capability,
  workflow, tool, knowledge-acquisition, introspection, message, and task
  decisions
- **Owner:** Von maintainers
- **Last reviewed:** 4 September 2026
- **State or evidence as of:** Public Von
  `dc84fc1a850890b32b89bdc8b3e0da96cfce4264`, a read-only audit of the
  configured Vontology and workflow-instance store on 4 September 2026, and
  the repository subtraction described below; no deployment or live Vontology
  mutation is claimed
- **Supersedes / superseded by:** Nothing. This proposal owns represented-advice
  semantics, applicability, retrieval/projection, lifecycle, and retraction;
  the broader
  [automated-policy-learning design](automated_policy_learning_design.md)
  retains actor-critic, candidate-generation, evaluation, and promotion scope
  and defers its textual-guideline details here
- **Review trigger:** Evidence of a concrete recurring user-job failure that
  selects a first comparison, selection of a production consumer or autonomous
  maintenance canary, or publication of a general advice vocabulary/projection

## 1. Decision summary

Von should explore represented advice, but should not begin by building a new
universal advice subsystem.

There is nevertheless a credible unification path. Von can converge the soft,
defeasible parts of its current guidance surfaces on a **shared interchange,
projection, and lifecycle protocol**, while leaving decision semantics,
authority, storage, ranking, and specialised policy with their existing
owners. This is a federated maintenance and projection protocol for advice,
not a universal advice ontology, central ranker, authority plane, or mandatory
runtime service.

The proposed meaning is:

> **Advice is a short, provenance-bearing, defeasible text artefact authored by
> Von, retained as policy memory, and linked to the roles, concepts,
> capabilities, workflows, stages, or decision kinds for which it may improve a
> later judgement.**

“Policy memory” here uses the broad category already defined by Von's memory
architecture; it does not make advice an enforceable policy or invariant.

Advice is a revisable hypothesis about how Von might do better. It is not an
instruction, fact, workflow transition, permission, commitment, or proof that
its originating diagnosis was correct. The model making the existing decision
may use, adapt, or reject it in light of the current user request, shared
situation, available capabilities, and observed evidence.

The Phase 0 design decision is therefore:

1. treat advice as a **semantic role and lifecycle contract** over existing
   Vontology text, profiles, and policy-memory mechanisms;
2. subtract the dormant workflow-experience producer, evaluator-facing
   projection, and unpopulated selected-workflow policy-memory injector before
   publishing a general `Advice` ontology or inserting another model stage;
3. require any future advice lookup to be optional, batched, actor-scoped, and
   unable to remove candidates or enlarge authority;
4. reify a first-class advice individual only when independent identity,
   cross-target reuse, revision, evaluation, or retraction earns that extra
   representation; and
5. remove or collapse the mechanism if the same content works as well in an
   existing prompt or workflow profile.

If the first local consumer and a second independently motivated consumer both
earn reuse, they may share this lifecycle vocabulary while retaining
consumer-owned loops:

> evidence → candidate revision → scoped active selection → bounded projection
> → observed outcome → revision, graduation, expiry, or retraction

The shared vocabulary makes maintenance interoperable; it does not create one
central maintainer or decide the underlying tool, workflow, acquisition,
message, task, or role question for every consumer.

The identity decomposition, projection name, lifecycle terms, maintenance
worker, and later role application below are candidate mechanisms, not a package
to implement together. No first pilot is currently selected. If a concrete
recurring user-job failure reopens the question, the first comparison should use
source-native identities and the minimum explicit active locator; later
machinery is adopted only when the preceding evidence independently earns it.

This design is intended to keep useful conjectures revisable for longer. It
would fail its purpose if every disappointing episode simply became another
permanent paragraph in Von's context.

## 2. Advice's place in Von's cognitive architecture

Several nearby artefacts contain text, but they do different jobs:

| Artefact | Question it answers | Authority |
|---|---|---|
| Observation or episode | What happened? | Evidence only |
| Assertion or domain knowledge | What is put forward as holding in a context? | Epistemic content with provenance; not automatically true |
| Advice | What might help at this decision, and under what circumstances? | Defeasible advisory policy memory |
| Prompt or role policy | How should this model stage generally behave? | Authored behaviour within its selected scope |
| Workflow | What reusable process can be executed and recovered? | Executable procedure whose effects remain independently authorised |
| Capability or authority boundary | What may the actor and system do? | Hard maximum access and effect boundary |
| Task or commitment | What work has been undertaken? | Durable obligation or external coordination state |

Advice may influence a decision within these boundaries, but it does not alter
their meanings. In particular:

- advice about a tool does not make the tool available or authorised;
- advice about a workflow does not establish that the workflow ran;
- advice about a knowledge-acquisition goal is not acquired knowledge;
- advice to inspect something is not diagnostic evidence;
- advice to send a message or create a task is not authority for that effect;
- advice about a role does not assign the actor new responsibilities or
  permissions; and
- repeated advice does not become an invariant merely through repetition.

A stable, broadly applicable lesson may eventually be incorporated into a
prompt or workflow. A concrete unacceptable outcome may justify an exact code
or authority boundary. Those are separate promotion decisions. When advice is
replaced by a stronger artefact, the active advice should be retired rather
than left as a second route saying the same thing.

## 3. Intended user value and counter-hypotheses

The user job is not “retrieve advice”. It is for Von to recognise the useful
role it can play, choose an adequate route, produce the requested work product,
and recover intelligently with low latency and human burden.

Represented advice could help through four causal mechanisms:

| Claimed benefit | Plausible mechanism | Important counter-hypothesis |
|---|---|---|
| Faster | Avoid a known bad route, redundant discovery, unnecessary model call, or low-value question | Lookup, ranking, and extra prompt tokens cost more than the avoided work |
| More effective | Reuse local practice and prior recovery knowledge across turns without hard-coding it | Stale advice anchors the model and displaces better current judgement |
| More accurate | Prompt acquisition of missing evidence, preserve source limits, and recall failure patterns | Advice preserves a mistaken causal story and makes errors more coherent |
| Better role fit | Retrieve advice linked to the current role, responsibility, work product, organisation, and handoff | Generic role advice becomes a stereotype or fossilised process that conflicts with the actual request |

All four are hypotheses. A shorter prompt, better workflow description, better
tool metadata, stronger suitable model, or no extra mechanism may win.

Advice is particularly promising where all of the following hold:

- the decision remains semantically contingent rather than a hard invariant;
- experience has value beyond one episode;
- the lesson needs revision, provenance, or reuse independent of a large base
  prompt;
- a consumer can retrieve it cheaply at an existing decision point; and
- success and negative transfer can be observed on the end-to-end task.

It is a poor fit for an exact interface constraint, a one-off case judgement,
raw telemetry, a long procedure that should be a workflow, or a platitude a
capable model already follows reliably.

Against Von's longer-term aims, the intended contribution is specific:

- **reliable:** expose which revisable guidance affected a choice, preserve
  counterevidence, and make withdrawal and recovery cheaper than another code
  release;
- **knowledgeable:** use concept links to find relevant practice and recurring
  evidence gaps, while leaving facts and acquired knowledge in semantic memory
  rather than laundering them into advice;
- **adaptive:** bind revisions to roles, workflows, stages, actors, teams, and
  changing model/tool dependencies without freezing one global rule;
- **autonomous:** let Von perform bounded proposal, comparison, drift detection,
  revision, and retraction through independently governed workflows;
- **minimal-imposition:** reduce repeated discovery, clarification, approval,
  and correction while spending human attention only on materially different
  value, authority, publication, privacy, or coordination choices; and
- **team-effective:** retain who proposed, corrected, approved, or disputed a
  practice and support better work products and handoffs without turning team
  custom into universal policy.

## 4. This is not greenfield

The repository contains several overlapping advice-like surfaces and now
records one retired prototype:

| Existing surface | What it already provides | Design consequence |
|---|---|---|
| [`workflow_routing_profile.v1` and workflow discovery exemplars](../../src/backend/workflows/vontology_loader.py) | Vontology profiles store and carry routing role, direct-equivalence, selection guidance, examples, exclusions, and routing notes; current selector candidate rendering does not explicitly project `selection_guidance` | Audit and reuse the stored field deliberately if it proves useful; launchability remains independently established, and storage alone does not change selection |
| Retired workflow-experience guidance induction | The audited repository path attempted to author successful-run, failure-avoidance, and exploration text after every persisted episode assessment | Preserve the raw episode evidence, but do not republish this producer without an independently motivated user job and evidence; repository history retains the old mechanism |
| Retired `#V#workflow_experience_context_prelude` | The audited prelude derived a workflow/model profile and read the latest five values of three additive relations for four direct proposal/evaluation/release callers | Replace the live-capable graph with an unpublished, actionless terminal tombstone and keep it out of those callers; recency was not activation, applicability, deduplication, or independent evidence |
| Retired selected-workflow policy-memory projection | A separate raw-episode-backed builder and renderer for recent improvement suggestions had no in-repository producer, while the orchestrator could still render caller-supplied state as system text | Remove the builder, renderer, turn-stage injection, and misleading lineage path; retain critique evidence without treating it as promoted policy |
| [Tool planner hints](../../src/backend/services/tool_metadata_service.py) and [outcome-explanation maps](prompt_programs_and_model_routing_playbook.md#3b-outcome-explanation-guidance) | Specialised model-facing guidance at established tool and explanation surfaces | Keep domain-specific shapes where they are the simplest adequate representation |
| [Knowledge-acquisition profiles](../../src/backend/services/knowledge_acquisition_profile_vontology_service.py) | Typed defaults, thresholds, auto-application, and fail-closed behaviour | Treat these as stronger acquisition policy requiring its own ratchet review, not generic soft advice |
| [Publication-scope profiles](ontology_publication_authority.md#represented-publication-scope-profiles) | A concrete code-level precedent for defeasible Vontology advice separated from mutation authority | Reuse its boundary, not the domain schema or resolver unchanged: an invalid accessible profile can currently invalidate the decision, protected outcomes dominate ordinary conflicts, and add-only seed reconciliation can resurrect removed profiles |
| [Workflow selection experience and policy](../../src/backend/services/workflow_selection_policy_service.py) | Process-global outcome-derived numeric priors, lexical token affinity, and exploration signals that advise the selector while leaving semantic choice to the model | Treat this as a competing diagnostic baseline, not privacy-compatible production advice until its population and scope are isolated |
| [Operational learning release support](../../src/backend/services/operational_learning_release_vontology_service.py) | Candidate evaluation, artefact-specific active pointers, receipts, rollback, and exact namespace/user/organisation scope | Reuse proportionately for consequential promotion in that supported scope; it is not yet a general private-advice lifecycle and must not be imposed on every hint |

The retired workflow-experience implementation supplied direct ratchet
evidence:

- one episode-evaluation path renders and writes all three guidance kinds,
  including a literal “no durable lesson” fallback;
- the three additive writes are sequential, so a later failure can leave a
  partial set; the pilot must first establish whether that partial set is
  harmful or the hints are independent, in which case idempotent writes with
  receipts are preferable to an unnecessary transaction;
- its gateway test deliberately permits another relation containing the same
  guidance body but a different request identifier;
- the retrieval prelude reads the latest five relations for each of three
  predicates, rather than an active, deduplicated, applicability-aware view;
  and
- its profile key contains workflow and model but the retrieved items are not
  filtered by their recorded stage, supersession, expiry, utility, or
  retraction state.

The bounded live audit found no populated guidance to preserve or promote. In
the configured database there were zero text relations and zero scoped
assertions across all three guidance predicates, zero distinct linked guidance
profiles, and zero recorded prelude workflow instances. Subworkflow invocation
need not always create a separate instance, so the instance count alone is not
proof of non-execution; the empty guidance stores and source/runtime path are
the stronger evidence of dormancy.

The same audit found 134,215 episode-evaluation workflow instances. The latest
100 by indexed creation time contained 81 cancelled and 19 failed instances,
with no success; the latest five failures stopped at the episode-evaluation
model step before assessment persistence or guidance induction. Before this
repository change, the completion-gate binding was enabled, the terminal-event
binding was disabled, and the integration defaulted automatic triggering on.
These observations distinguish a failing automatic producer from a useful
advice capability; they do not establish the state of every historical
instance or justify deletion of raw evidence.

This satisfies the Phase 0 stop rule. The repository implementation therefore
removes the guidance authoring tail, prompt registration and seed vocabulary;
replaces the live-capable prelude with an unpublished, actionless terminal
tombstone; removes its four proposal/evaluation/release injections and the same
three retired history fields from 17 other LLM states that could still project
caller- or parent-supplied values; and defaults both
episode-evaluation bindings and the integration trigger off. On
repository bootstrap, canonical lifecycle write and read-back also mark the
tombstone retired and routing-ineligible. Explicit episode evaluation and raw
episode evidence remain. No live ontology artefacts or historical instances
were deleted, and this change is not a live deployment. A future resolver must
still never translate “latest” into “promoted”.

The durable worker currently begins polling before these non-critical workflow
families finish their startup migrations. A bounded live read found no pending,
running, or paused instances for the retired prelude, episode evaluators,
self-improvement workflows, or affected operational-learning workflows, so no
present instance was found that could exercise that window. Deployment should
nevertheless quiesce old workers, apply and read back the reviewed seed
migrations and retirement lifecycle, and only then enable new workers; this
repository change does not claim rolling-deployment safety across foreign old
workers.

The separate selected-workflow policy-memory builder read recent critique
suggestions from Mongo. A static source scan found no in-repository caller of
the builder, but the orchestrator would still render a caller- or parent-supplied
`selected_workflow_policy_memory_state` as system text in tool-planning and
synthesis contexts, and record it as lineage. Its source query also made
`namespace` optional and had no independent user/organisation access check.
Phase 0 therefore removes the builder, renderer, two turn-stage injections, and
lineage projection rather than preserving a dormant authority aperture.

The retired episode-evaluation autotrigger default and completion-gate binding
could launch a large amount of failing work. They now default off; explicit
episode evaluation remains available. If later evidence identifies a worthwhile
recurring maintenance job, compare a separate asynchronous batched process with
the simpler direct path rather than piggybacking on every episode.

Operational-learning releases provide valuable immutable-candidate, scope,
expiry, pointer, receipt, and rollback mechanics, but cannot be reused
unchanged. Registration currently requires failure-evidence packets, although
useful advice may arise from success or exploration, and rollback requires a
previous active release rather than supporting first-release retraction to the
ordinary empty/no-advice state.

These mechanisms also do not share one visibility model. Some reads use an
ambient actor-effective Vontology view, some use the base-publication view,
tool metadata uses a process-global catalogue cache, and the latent critique
path accepts an optional namespace. A general resolver cannot inherit any one
of those behaviours unchanged.

Role advice is not a shortcut around role representation. The
[programme design](Von_for_AgenticAI.md) describes a represented role with
responsibilities, authority, workflows, and work products, while the current
[task-ontology service](../../src/backend/services/task_ontology_service.py)
uses role primarily as text framing. Role-linked advice can therefore be
considered only when a concrete role-performance failure selects the work and
the target identity and relevant responsibilities are inspectable; it is not a
shortcut to selecting a pilot now.

### 4.1 The unification boundary

The useful commonality is deliberately narrower than “everything that guides a
decision”. A shared layer may own:

- stable advice/revision identity and exact target links;
- provenance, evidence lineage, visibility, lifecycle, and removal triggers;
- mechanical proposal, compare-and-set selection, supersession, retirement,
  rollback, and exact read-back operations executed only from the consumer's
  authorised release decision;
- a bounded actor-safe projection contract and failure diagnostics;
- exposure/outcome telemetry, dependency drift signals, and token budgets; and
- reversible adapters through which an existing surface exposes soft advice
  without migrating its native schema.

The consumer or specialised policy continues to own:

- domain applicability beyond exact target matching;
- relevance, conflict interpretation, and decision-specific evaluation;
- whether and where advice enters its existing model call;
- the final action choice and its independent authority checks;
- native storage and stronger prompt, workflow, score, or policy semantics; and
- activation, retirement, and rollback decisions and promotion criteria
  appropriate to the consequence and audience.

The following soft components are plausible convergence candidates:

- future workflow-local success, failure-avoidance, and exploration guidance
  that is independently motivated rather than restored from the retired path;
- the `selection_guidance` portion of workflow routing profiles, but not
  eligibility, launchability, routing role, or direct equivalence;
- evidence-conditioned or actor-specific tool-planner prose, while static
  global tool descriptions may remain metadata;
- recovery, introspection, role-practice, message-structure, task-practice, and
  explanatory advice once their target and outcome identities are represented.

The following remain specialised even if they reuse versioning, evidence, or
release primitives:

- workflow control flow, postconditions, and deterministic follow-up actions;
- tool schemas, availability, dispatch, and effect authority;
- knowledge-acquisition thresholds, automatic application, and fail-closed
  behaviour;
- publication-scope semantics and protected-carrier conflict handling;
- prompt bodies and outcome-explanation maps;
- numeric workflow/model-routing policies;
- roles, tasks, commitments, assertions, and raw episodes; and
- security, visibility, and authority boundaries.

An adapter may expose an explicitly soft projection from one of these stronger
surfaces. It must not relabel the whole source as advice or weaken its native
semantics. If a common implementation starts acquiring tool-, workflow-,
publication-, or role-specific branches, move those semantics back to the
consumer or abandon the abstraction.

## 5. Design principles

1. **The current user job leads.** Advice supplements the explicit request and
   shared conversation situation; it does not redefine them.
2. **Preserve alternatives.** Advice may change model-visible salience or
   ranking. It must not remove an otherwise authorised tool, workflow, direct
   route, recovery, or answer strategy.
3. **Advice is not authority.** Trusted actor context determines visibility and
   capability. The eventual effect independently verifies authority.
4. **No universal advice stage.** A consumer opts in at an existing decision
   point only when prior evidence suggests advice could change the outcome.
5. **Use the weakest adequate representation.** Attached text is preferable to
   a new first-class individual until reuse, lineage, or retraction needs more.
6. **Retrieve a small relevant view, not a history.** Raw episodes and retired
   advice remain auditable but do not enter the prompt by default.
7. **Absence is normal.** Missing, inaccessible, malformed, or slow advice must
   not block an otherwise viable task. Readable alternatives remain usable.
8. **Keep conflicts visible.** “Newest”, “most specific”, “most restrictive”,
   and “highest confidence” are not universal conflict-resolution rules.
9. **Do not self-govern.** Advice may propose a future resolver or evaluator
   change, but it cannot select, rewrite, or supply the success criteria for the
   resolver, evaluator, authority boundary, or activation policy governing its
   current revision. Such a proposal needs a separate release and independent
   evaluation.
10. **Deletion is a successful outcome.** Every production adoption has an
    observable condition under which the advice or mechanism will be retired.

Von authorship is not a content-safety boundary. Retrieved mail, web pages,
documents, tool output, and prior model text remain untrusted evidence even
when an evaluator summarises them into a candidate. Advice activation must not
launder source instructions into higher-priority policy.

Promoted advice remains untrusted advisory data at consumption time. It must be
rendered as a labelled, defeasible input below the current user request and
must not be injected as an undifferentiated system instruction or acquire
higher instruction priority merely through representation or promotion.

## 6. Representation

### 6.1 Two adequate forms

| Form | Use when | Avoid when |
|---|---|---|
| Text relation attached to one target or versioned profile | A simple candidate has one owner and consumer, while an enclosing profile or index owns any active revision | A raw attached relation itself needs revision, supersession, retraction, or separately accumulated evidence |
| First-class advice knowledge item linked to targets | The same advice is reused, revised, evaluated, contradicted, or retracted independently across several concepts or workflows | A new node would only wrap one existing prompt or profile field |

A future first experiment should use the source-native profile or attachment
surface already owned by its selected consumer, not resurrect the retired
workflow-experience substrate by default. Its represented arm needs an isolated
explicit manifest or pointer for the candidate and active revision; raw
additive relations do not acquire lifecycle merely because they are in
Vontology. A general advice item must still earn its ontology layer.

Whether stored natively or exposed through an adapter, the minimum external
logical contract is:

- stable advice and revision identity;
- one concise body;
- one or more target concept identifiers;
- producer, provenance, and evidence references;
- a visibility carrier resolved afresh from trusted actor context and kept
  distinct from target, applicability, and evidence context;
- lifecycle sufficient to distinguish candidate, active, superseded, and
  retired revisions;
- a removal, review, or retest condition for every active production item; and
- an inspectable reason why the item was retrieved.

When not unambiguously supplied by the exact target/carrier, the contract must
also include decision kind and applicability. Only when material to an item
should it add:

- model, prompt, workflow, tool, or ontology-version dependence;
- calibrated confidence or utility evidence;
- counterevidence;
- an explicit relationship to competing advice.

This is deliberately not one compulsory source schema or JSON envelope with
fourteen fields. Existing Vontology relations, specialised profiles,
text-relation context, revision records, and evidence locators may supply the
contract through reversible adapters. Before publishing new predicates,
implementation must search for and reuse the current lifecycle, provenance,
scope, and supersession vocabulary.

If the operational-learning release vocabulary is reused, the provisional
mapping is candidate to `proposed`, active to `active`, and superseded to
`superseded`; rejected candidates use `rejected`, while `rolled_back` records a
release transition rather than acting as a synonym for retired advice. No
generic advice active pointer exists today. This mapping must remain an adapter;
it must not import the operational-learning approval rules or full release
apparatus into every ordinary hint.

A provisional richer representation might use an `Advice` type, one content
text relation, target links such as `advises about`, evidence links, and a
supersession link. These names are design placeholders, not approved ontology
identifiers.

### 6.2 Candidate long-term identity decomposition

The first consumer should retain source-native identity plus an explicit
active-selection locator. If two independently motivated consumers later earn a
common layer, the following five-part decomposition is one candidate to test,
not a required schema:

| Identity | Meaning |
|---|---|
| Advice series | The stable revisable lesson or question |
| Advice revision | One immutable expression with provenance and dependencies |
| Advice binding | The target, decision kind, consumer, applicability, and visibility context in which a revision could be useful |
| Advice release | The scoped selection of one revision for one binding, with its evidence, lifecycle state, and release authority |
| Advice projection | A derived, bounded runtime view; never an authority or canonical knowledge object |

In that candidate, activation belongs to the binding/release, not to the
revision globally. The same revision may be active for one workflow stage,
still proposed for a second, and inapplicable or inaccessible to another actor.
Candidate lifecycle terms are `proposed`, `active`, `superseded`, `rejected`,
`expired`, and `retired`. Rollback is a transition with a receipt that restores
a prior eligible release, not another name for retirement.

An enclosing versioned profile and explicit manifest can play these roles for
a future first pilot. First-class series, binding, and release items become
justified only when shared lifecycle operations remove real duplicated work.

### 6.3 Body quality

An advice body should normally express one reusable idea:

- the situation in which it may apply;
- the action or consideration that may help;
- the intended consequence.

Evidence identifiers, telemetry dumps, complete prompts, and stack traces stay
outside the body. The retired one-sentence, 280-character experience format is
a useful possible experimental bound, not a universal semantic requirement.

Examples of suitably defeasible advice include:

- “When a scholarly source exposes a DOI, resolve it before name-only author
  matching to reduce duplicate identity candidates; retain the source
  occurrence if identifiers disagree.”
- “For a request that may continue an earlier effect, inspect the exact
  terminal receipt before launching new work; proceed directly when the new
  request clearly diverges.”
- “Before asking for an acronym expansion, inspect the conversation situation
  and linked project concepts; ask only if the remaining alternatives change
  the work product.”

The complete advice artefact—not necessarily its short projected body—must make
its applicability and reconsideration condition inspectable through the body,
typed metadata, linked evidence, or review trigger. If the artefact cannot say
when it might be wrong, it may be a prompt principle, an invariant, or an
unsupported slogan rather than useful advice.

### 6.4 Targets and context

Advice may be linked to:

- a role, responsibility, work product, or handoff;
- a task or request type;
- a capability or tool family;
- a workflow, stage, or prompt concept;
- a knowledge-acquisition goal or evidence kind;
- an introspection or recovery surface;
- a message or task kind; or
- a model/tool/workflow version when the evidence is genuinely version-bound.

Link to capability concepts rather than only transient tool names when the
lesson is about a stable capability. Link to exact versions when the lesson is
implementation-specific. Advice derived from one person's or organisation's
practice stays in that actor-visible context unless a separate governed
promotion supports wider publication.

Exact target links are strongest. Bounded ancestor or semantic matches may be
retrieved as weaker candidates but must not silently override direct advice.
Specificity affects relevance, not authority.

Semantic applicability and visibility are independent. Resolve visibility
from a trusted actor-bound carrier before relevance ranking: a target link,
natural-language namespace, provenance context, or visible target concept never
confers access to the advice. An actorless read may return globally published
advice only.

## 7. Runtime resolution and use

Advice should enter an existing judgement rather than create a controller
tower:

```mermaid
flowchart LR
    S[Current request and shared situation] --> C[Existing candidate discovery]
    C --> D[Existing model decision]
    T[Target concepts and actor scope] --> Q[Optional bounded advice query]
    Q --> B[Small labelled advice bundle]
    B --> D
    D --> E[Bounded tool, workflow, message, or task action]
    E --> R[Receipt, read-back, and outcome evidence]
    R -. optional offline input .-> V[Evaluation]
    V -. optional candidate .-> P[Candidate advice or revision]
    P -. governed comparison and activation .-> X[Held-out comparison and governed activation]
    X -. active revision .-> Q
```

### 7.1 Common projection protocol

If a second consumer demonstrates genuine reuse, one compact runtime read model
may help even while source schemas remain federated. In a provisional
`advice_projection.v1`, the resolver derives trusted actor/namespace/audience
scope from pre-existing server or workflow context and rejects disagreement
with any supplied scope fields. The request supplies consumer and decision kind,
exact target and candidate identifiers, relevant dependency versions, and an
item/token budget; payload identity is never authentication.

Its response contains only accessible active releases and reports, for each
item, the available source-native or series/revision/binding/release identities,
concise body, exact target, source kind, retrieval reason, and any explicit
conflict group. Include an evidence reference only when it is independently
visible to the actor; otherwise use a non-revealing receipt or digest. The
response also carries a projection digest, dependency versions, truncation, and
bounded error metadata without revealing inaccessible-item identities or
counts. The name, decomposition, and fields remain candidates until two
consumers demonstrate shared need.

Common support may enforce trusted visibility, active-revision integrity,
exact-target matching, bounded size, exact duplicate identity, and inspectable
read-back. The consumer retains semantic applicability, relevance, conflict
interpretation, graph expansion, prompt position, and choice. There is no
cross-domain confidence score or common ranker.

For the first consumer:

1. Build the normal candidate set and minimum-sufficient situation projection.
2. If the decision is advice-enabled, issue one actor-scoped batch read using
   trusted server context, the decision kind, and already-known
   target/candidate identifiers. Without actor context, query globally
   published advice only.
3. In the pilot, use exact active target links only. Add bounded ancestor or
   semantic expansion later only if measured misses justify its cost.
4. Deduplicate exact logical/revision identity only. Identical wording from
   distinct producers or evidence contexts remains distinct until an offline
   semantic consolidation decides otherwise. Retain material conflicts and fit
   the result to the consumer's small token budget.
5. Render the bundle below explicit user instructions and observed facts,
   clearly labelled as optional prior advice with item/revision identities and
   retrieval reasons.
6. Let the existing decision model select among the full candidate set. Where
   that model already emits a structured decision, it may name advice it
   followed or rejected without exposing chain-of-thought.
7. Execute through the normal capability and authority boundary and verify the
   result normally.
8. Record projection and outcome evidence for causal evaluation.

The advice resolver may return zero items. An inaccessible or malformed item
does not invalidate readable items and must not leak its identity or count. A
material conflict may be exposed to the deciding model; it reaches the user
only when current evidence cannot resolve a choice whose privacy, commitment,
cost, or harm differs materially.

Advice retrieval does not alter `allowed_tools`, routing eligibility, workflow
publication status, or mutation authority. A candidate may become more
salient, but none may disappear solely because advice dislikes it.

### 7.2 Consumer boundaries

| Consumer | Useful advice | Boundary that remains authoritative |
|---|---|---|
| Role identification | Responsibilities, expected work products, handoffs, and local practice linked to a role and situation | Explicit user objective, represented commitments, and delegated authority |
| Workflow selection | Known applicability, counterexamples, and useful recovery alternatives | Published workflow capability, current inputs, and model judgement |
| Tool selection | Efficient tool families, prerequisite reads, and recurring failure patterns | Available tool catalogue, schemas, and effect authority |
| Knowledge acquisition | High-value missing evidence and low-imposition ways to obtain it | Current epistemic state, source provenance, and the user's actual information need |
| Introspection and recovery | The next discriminating read-only probe or a previously effective recovery | Current telemetry, bounded task budget, and observed action order |
| Messages and tasks | Audience-appropriate structure, useful follow-up, and local coordination practice | Verified outcome, existing commitments, recipient scope, and authority to create or send |

Advice can help role performance only after role and situation evidence have
been brought together. It must not become a persona prompt that invents duties
or treats a broad role description as permission.

### 7.3 Latency and context discipline

To have a credible path to making Von faster:

- do not add a separate LLM call to select advice in the first implementation;
- batch reads instead of performing one sequential call per advice kind or
  candidate;
- reuse the existing capability/graph retrieval result where possible;
- cache only derived actor-safe projections keyed by exact trusted
  actor/namespace/audience scope, semantic view, and advice/ontology revision,
  while retaining Vontology as authority;
- never reuse the current process-global tool-metadata cache for private
  advice; private cache keys include canonical actor/organisation scope and
  semantic view, while a global cache may contain globally published advice
  only;
- skip lookup on direct, trivial, or already-decided paths;
- cap projected advice through consumer-owned relevance policy and tokens, not
  merely by “latest N”; and
- proceed without advice when the optional result is not ready in time for the
  current decision.

Begin with the direct batched read. If measured graph and assembly cost remains
material after a common protocol is earned, compile immutable scoped projection
manifests when releases change. The online path can then union only the
actor-accessible global, organisation, user, or contextual manifests and verify
their revision digests. Compilation is a derived acceleration, not a second
authority store, and must not precompute private membership into a shared
artefact.

A late optional advice result may inform a later evaluation or cache generation
but must not restart a decision that has already safely progressed. Before
caching it, revalidate the exact scope and advice/ontology revisions, and never
place an actor-private projection in a shared cache. Advice lookup latency,
prompt displacement, and end-to-end time are part of the result; they cannot be
reported as zero or excluded from a “faster” claim.

## 8. Authoring and adjustment lifecycle

### 8.1 Core lifecycle

The desired learning loop is:

1. **Observe.** Preserve the episode, receipt, outcome, user correction, and
   evaluator result separately from any explanation.
2. **Propose.** Von may author a concise candidate advice item and link it to
   the observed evidence. A single episode supports a narrow candidate, not
   automatic general policy. Advice derived from untrusted external content
   remains quarantined until independent evidence supports activation.
3. **Check redundancy and counterexamples.** Search existing advice, prompts,
   profiles, workflows, and nearby episodes. Merge equivalent candidates or
   leave the observation episodic when there is no durable lesson.
4. **Evaluate.** Compare the candidate with the best simpler baseline on
   neighbouring and held-out tasks. Test no-advice cases and materially
   different valid routes.
5. **Activate narrowly.** Publish one immutable revision for the smallest
   supported target and audience. Promotion authority is distinct from the
   advice itself.
6. **Observe use and non-use.** Retain which revisions were projected, the
   resulting actions and outcomes, and counterevidence. Do not infer that a
   model used advice merely because it was in the prompt. Advice-conditioned
   success is not independent confirmation of that advice: deduplicate evidence
   by originating event/digest, and require a no-advice comparison or other
   independent evidence before increasing confidence.
7. **Revise, supersede, or retire.** Change the explicit active selection,
   artefact-specific pointer, or lifecycle relation provided by the chosen
   representation rather than silently overwriting history. Retraction must
   remove the item from active projections and read back exactly; there is no
   generic advice pointer to assume today.
8. **Graduate or subtract.** Move a genuinely stable instruction into the
   appropriate prompt or workflow, or a genuine invariant into its independently
   justified boundary; retire the advice. Delete the runtime path if it adds no
   net value.

“No durable lesson” is an outcome, not advice text to append. Repeated identical
bodies should strengthen or challenge one candidate's evidence, not create
another active prompt fragment.

Repository seeds may bootstrap missing vocabulary or a named release input.
They must not overwrite revised live content or resurrect retired advice.
Owned seed reconciliation therefore needs the same tombstone and
duplicate-identity discipline as other governed represented policy.

Human-authored correction should remain distinguishable from Von-authored
advice. It may be higher-quality evidence or an explicit instruction, but the
system must not silently relabel it to simplify precedence.

### 8.2 Evidence independence

Maintenance consumes typed observations, not one cross-domain confidence or
reward score. Outcome and problem evidence may include canonical read-back,
repeated independently originating verified failures or recoveries, and an
independently verified downstream result. An explicit correction can establish
an actor-scoped problem, instruction, or preference, but not that proposed
advice fixes it; an unexplained artefact edit is diagnostic only. Authenticated
approval from a suitably authorised principal can establish release authority
for an exact candidate and scope, but not efficacy. Blinded evaluator
judgements, repeated unnecessary questions or
retries, latency regressions conditional on adequate completion, and dependency
drift can support investigation.

A model self-critique, aggregate score, advice projection or citation, user
silence, outer completion, or several AI summaries of the same episode is
diagnostic only. Every derived observation retains its root evidence lineage;
copies across telemetry, critics, and team agents still count as one originating
event.

Candidate generation runs over a bounded observation window and returns one of:
new candidate, revise, reinforce or challenge existing evidence, retract,
graduate, or no durable lesson. It keeps the observation separate from the
causal hypothesis and includes the exact target, intended consequence,
applicability/dependencies, valid alternatives or counterevidence, removal or
retest condition, and root evidence locators.

Before evaluation, freeze the candidate digest, induction/calibration/held-out
partition, rubric, thresholds, and relevant resolver/release versions. The
candidate author cannot alter the evaluator, promotion rule, authority path, or
held-out cases governing its own release. Proposer, evaluator, and activator may
all be Von-operated workflows, but independently versioned roles provide
auditability rather than evidential independence. Exclude the candidate and
related active advice from evaluator context except in the deliberately
advice-on arm. Different agents or models are not independent when they share
the same trace, source, prompt lineage, or evaluator.

Advice-exposed outcomes are labelled as such. They can reveal harm immediately,
but an independent read-back verifies the outcome, not that advice caused it.
Only a matched or randomised advice-off comparison supports efficacy; an
alternative-route replay tests whether advice suppressed opportunity. A small
bounded advice-off or alternative-route replay sample should continue after
activation where the corresponding claim matters; otherwise successful advice
can suppress the observations capable of disproving it.

### 8.3 Bounded autonomous maintenance

The online user path should resolve, project, act, and record outcomes only.
If a bounded pilot independently earns autonomous maintenance under the
post-pilot branch below, candidate generation, counterexample search,
experimentation, consolidation, and ordinary promotion may run asynchronously
in a low-priority actor-scoped, idempotent maintenance workflow. Trigger such a
workflow by novelty, repeated recoverable failure or burden, expected reuse,
conflict, explicit correction, dependency drift, or a due review—not after
every episode. If no recurring maintenance job earns that machinery, keep the
path manual or subtract it.

Autonomy is graduated by the consequence and scope of the maintenance effect:

| Maintenance action | Permissible autonomy |
|---|---|
| Inventory, exact-identity deduplication, conflict/drift detection, candidate authoring, and isolated evaluation | May run automatically within actor-safe resource budgets; it changes no active guidance |
| Integrity withdrawal | The resolver withholds an item from the affected projection when trusted visibility, digest, or revision checks fail; an authorised release process may retract the exact binding when a declared dependency is invalid, then verify the no-advice projection |
| Semantic activation, revision, or retirement | May be automated initially only for a narrow, reversible, low-consequence actor-private binding after predeclared A/B/C evidence, standing release authority, exact read-back, and rehearsed retract-to-empty |
| Shared, organisational, high-burden, or materially consequential guidance | Uses authorised review and the stronger release path until evidence justifies a lighter boundary |
| Resolver, evaluator, promotion policy, authority, or maintenance-budget change | Requires a separate versioned proposal and independent release; current advice cannot govern it |

Retraction is deliberately easier than activation. An explicit actor-level “do
not use this” can suppress that actor's projection immediately. Confirmed
scope, privacy, authority, wrong-effect, or stale-revision harm withdraws the
exact affected release within pre-authorised scope; weaker performance evidence
accumulates through a matched observation window. One actor's rejection remains
actor- and reason-scoped evidence. It becomes broader counterevidence only after
a separately authorised, independently verified general failure claim. The
active selection can become empty; no dummy “no advice” item should be
manufactured.

Bind version-sensitive advice to the smallest necessary dependency fingerprint:
model/provider profile, prompt or workflow revision, tool/capability schema,
relevant ontology semantics, and resolver revision. Actor/audience scope is a
trusted access and release-binding dimension, not a dependency fingerprint: a
scope change requires fresh visibility resolution or denial, not behavioural
retesting. An exact declared dependency mismatch makes the binding inapplicable
and triggers bounded retest without blocking the user task. An unrelated
version change does not invalidate stable advice, and a review date requests
evidence rather than silently erasing an otherwise valid release unless expiry
was explicit.

One capability-specific maintenance resource envelope should bound online
retrieval, observation sampling, candidate/evaluator work, experiments, human
review, and release churn. Add separate sub-budgets or oscillation controls only
when observed behaviour warrants them. Exhaustion deduplicates, defers, or drops
low-value maintenance; it never delays the current user task or disables the
cheap emergency retraction/read-back path.

### 8.4 Human and AI teams

Human instructions, corrections, approvals, and observed artefact edits remain
distinct from AI proposals and evaluations. AI-team feedback can contribute
evidence but cannot impersonate human approval, widen publication scope, grant
authority, or multiply one root event into consensus.

For team-visible advice, preserve producer identity, role, audience, dissent,
and handoff context. Ask people only when a material value, authority,
publication, privacy, or coordination choice cannot be resolved from the shared
situation and evidence. Bundle consequential candidate, supporting and contrary
evidence, intended scope, and removal condition into one review rather than
soliciting feedback after every turn. Ordinary use should remain unobtrusive;
surface advice provenance to the user only when it materially explains a choice
or uncertainty.

## 9. Reliability Ratchet test

This design applies the diagnostic from
[The Reliability Ratchet](https://michaelwitbrock.com/2026/08/01/the-reliability-ratchet/)
and the repository's
[source and case log](reliability_ratchet_articles_and_cases.md).

For a material candidate or adoption decision, the proposal/evaluation should
answer three lightweight questions:

1. What recurring failure or opportunity class does this advice address?
2. What valid behaviour, current judgement, or future route might it suppress?
3. What observation would cause us to revise, retire, or delete it?

These answers belong in an existing candidate or evaluation record. They do
not justify a new mandatory repair-theory schema for every hint.

| Ratchet risk | Required design response |
|---|---|
| Every failure appends a lesson | Candidate state, evidence threshold, deduplication, and an explicit no-durable-lesson path |
| Context grows monotonically | Only active relevant revisions enter a fixed budget; raw history remains outside the prompt |
| Advice becomes a rule by tone | Explicit defeasible labelling and preservation of all authorised candidates |
| A diagnosis fossilises | Store observation separately, retain counterevidence, and test neighbouring causes/routes |
| Old model/tool behaviour governs a new release | Version-sensitive applicability and review triggers where evidence requires them |
| One malformed or inaccessible profile disables the task | Partial readable results plus normal no-advice fallback |
| Seeded advice returns after deletion | Ownership, tombstones, duplicate-aware reconciliation, and retraction tests |
| Tests require yesterday's route | Score user outcomes, prohibited effects, and recovery while allowing competent alternative paths |
| Advice duplicates prompts or workflow metadata | Prompt-only baseline and graduation/subtraction rule |
| A central optimiser collapses incomparable values | Consumer-specific evaluation and no cross-domain scalar, ranker, or reward oracle |
| The common interface accumulates domain branches | Move semantic interpretation back to its consumer or remove the shared abstraction |
| Advice confirms itself or team agents echo one trace | Root-evidence deduplication, exposure labels, independent outcomes, and counterfactual controls |
| The resolver becomes a compulsory dependency | Maintain and continuously test the direct/no-advice bypass |

Advice can be an anti-ratchet layer because it gives a conjecture durable
identity without immediately making it executable law. It becomes another turn
of the ratchet if its accumulation is easier than its retraction, or if
“defeasible” is only a label on context that the model cannot realistically
disregard.

## 10. Distinguishing evaluation

### 10.1 First bounded claim

The first evaluation should separate two narrow claims:

1. **Advice-content claim:** at one selected decision stage in one workflow, the
   chosen advice bytes improve a predeclared user outcome or successful-path
   efficiency in Arm B relative to Arm A.
2. **Representation claim:** Arm C preserves Arm B's immediate benefit within a
   predeclared latency/context bound and materially reduces operational error,
   time, or human burden during controlled revision, scope, drift, retraction,
   or maintenance. It is not expected to change model behaviour merely because
   the same bytes came from Vontology.

Before authoring the candidate, partition eligible episodes and cases into
induction, calibration, and untouched held-out sets. Derive advice from the
induction partition only. Use blinded calibration data to validate the harness
and select repetition count, while operational value determines the minimum
meaningful effect. Predeclare one or two task-specific primary outcomes and the
small set of blocking harms. Use the smallest held-out sample whose plausible
results could change the delivery decision, including ordinary, neighbouring,
and no-advice cases in proportion to the claim. Do not revise any of these
choices after seeing held-out outcomes.

### 10.2 Comparison arms

| Arm | Purpose |
|---|---|
| A — current path, candidate off | Current production context, including pre-existing advice-like material, held constant; only the candidate under test is omitted |
| B — simple versioned sidecar | The same targeting/applicability mapping and selected bytes supplied from one preselected prompt/profile sidecar without ontology retrieval; best simpler baseline |
| C — represented advice | Independently revisioned, actor-scoped, applicability-resolved, bounded projection |

Keep actor, ontology snapshot, available tools, prompt/workflow revisions, and
model configuration fixed, and retain requested, selected, and
provider-observed model identity. Isolate or reset conversations, caches,
learning state, durable effects, task artefacts, and advice pointers between
arms, using matched isolated fixtures where a state-changing case cannot be
cleanly replayed. Randomise order only within that isolation. Evaluators should
be blind to arm and internal route. Deterministic postconditions and canonical
world-state read-back outrank stylistic judgement; use independent semantic
evaluation only where the work product genuinely requires it.

Select Arm B's surface before running the held-out comparison. Hold its
targeting/applicability result, injection point, label, ordering, selected
bytes, and context budget equal to Arm C so the experiment distinguishes
represented retrieval and lifecycle from content or presentation differences.
If that equality is impossible, add a predeclared direct-injection diagnostic
and bound the claim accordingly.

Run the same revision/retraction crossover on both B and C: revise and then
retire one item, repeat its triggering and neighbouring cases, and compare
steps, errors, elapsed effort, and human burden. Verify that each exact active
projection changes, the no-candidate projection is restored, and ordinary
alternatives remain available. Any competent route may then succeed. This tests
the distinctive operational value claimed for representation rather than only
its wording.

If a failure plausibly reflects model capability, compare a stronger suitable
model without advice before adding more advice machinery. Advice should not
become permanent compensation for a model or tool limitation that has ceased to
exist.

### 10.3 Measures

Choose one or two primary measures and the blocking harms before the held-out
run. The lists below are menus for task-specific selection and causal diagnosis,
not a conjunctive release checklist.

Candidate primary outcome measures:

- verified completion or useful partial completion;
- factual groundedness and false-empty/unsupported-claim rate;
- correct durable effects through canonical read-back;
- useful-action, false-refusal, abandonment, and recovery rates;
- fulfilment of material role obligations and work-product criteria; and
- unnecessary work, handoffs, questions, confirmations, or user corrections.

Efficiency is measured conditional on adequate completion so fast failure
cannot win:

- end-to-end time to verified outcome and time to first material progress;
- successful-turn latency distribution;
- model calls, tool calls, workflows, failed attempts, and retries;
- input/output tokens and cost, retaining unavailable values as unknown; and
- advice retrieval latency, projected tokens, cache state, and displaced
  context.

Advice- and ratchet-specific measures:

- applicability precision and missed-applicable-advice rate;
- negative transfer on no-advice and conflicting-advice cases;
- train-to-holdout generalisation gap;
- stale, duplicate, conflicting, superseded, and never-selected advice;
- revision, retraction, and rollback burden;
- authoring, review, and promotion effort amortised over realistic use;
- active advice count and prompt footprint over time; and
- whether any valid candidate or recovery route became unavailable or
  effectively hidden.

### 10.4 Telemetry and causal interpretation

Retain, where the claim needs them:

- request, turn, experiment, actor/namespace, and task-case identities;
- prompt, workflow, tool-schema, ontology-snapshot, and advice revisions;
- candidate advice considered, retrieval/applicability reasons, selected and
  omitted revisions, projection order, digest, and token count;
- advice-query latency, cache state, and truncation;
- candidate tools/workflows before projection and the path actually chosen;
- tool/workflow attempts, recovery, receipts, final answer, terminal state, and
  canonical read-back; and
- evaluator rubric/version and judgement provenance.

Telemetry can establish what was projected and what happened. It cannot by
itself prove that advice caused the model's choice. Randomised, state-isolated
ablation supports only the bounded causal claim under test; it does not by
itself establish long-term or cross-consumer architectural value.

### 10.5 Decision rules

- If B does not improve on A, the candidate content has not earned a runtime
  path; do not build C to rescue it.
- If B improves on A but C is inferior beyond the declared online bound, keep B
  or remove the candidate and repair no representation machinery.
- If C preserves B's benefit but does not reduce real lifecycle/scope/drift or
  maintenance burden, the represented layer has not earned its additional cost;
  keep the simpler sidecar.
- If C preserves B's benefit and materially improves a predeclared operational
  maintenance outcome, retain only that bounded represented use.
- A direct-injection diagnostic can isolate lookup/projection overhead when B
  and C cannot otherwise be presentation-equivalent; it does not establish
  contextual-selection or lifecycle value.
- If evidence supports only the selected workflow/stage, retain only that
  target and do not claim a general advice architecture.
- Do not broaden to graph-wide concept, tool, message, and task advice. Attempt
  reuse only when a second materially different current user job independently
  needs it and direct prompt/profile attachment appears inadequate.
- Stop gathering evidence when another run is unlikely to change the decision
  to activate, narrow, revise, roll back, or subtract.

## 11. Convergence and delivery sequence

### Phase 0 — audit before adding

- Read the live Vontology and exact runtime path.
- Count active and historical workflow-experience relations, duplicate/no-
  lesson bodies, actor scopes, and prompt tokens.
- Establish whether the Vontology guidance prelude and raw selected-workflow
  policy-memory projection are actually populated and consumed.
- Classify existing workflow-experience relations as legacy/unclassified until
  promotion evidence and lifecycle state are established; never infer active
  status from recency.
- Measure lookup and total-turn latency.
- Identify the existing canonical lifecycle and supersession primitives.
- Measure per-episode self-improvement launches, root-evidence duplication, and
  maintenance work across actor/target/time windows rather than relying on
  per-run caps.

Stop before A/B/C if the live projection is dormant, no concrete recurring
user failure or opportunity is found, or deleting/merging obsolete paths removes
the accumulation problem. In that case subtract the unused substrate and do not
create a replacement advice worker.

#### Phase 0 result — 4 September 2026

The live Vontology projection was dormant, its additive stores were empty, and
no concrete recurring user-job failure selected a candidate. Its four declared
direct callers were proposal, evaluation, and release workflows, where
injection would undermine evidence independence. Seventeen other LLM states
still declared the same history fields without a repository producer, leaving
a direct-input compatibility aperture. The separate selected-workflow memory
builder also had no caller, although its renderer could accept caller-supplied
state. Meanwhile the automatic episode producer had accumulated 134,215
instances and its latest bounded cohort had no successes. The stop condition
therefore fired before candidate authoring or an A/B/C campaign.

The repository response is subtraction and containment, not a smaller advice
platform: remove the producer tail, prompt, seed vocabulary, four direct caller
steps, 17 remaining LLM-state projection apertures, and the unused
selected-workflow builder/renderer/injection path; replace the old prelude graph
with an unpublished, actionless terminal tombstone; default automatic episode
evaluation off; retain explicit critique, evidence, and normal evaluator paths;
and add structural and lifecycle read-back checks so a successful reviewed
bootstrap cannot restore the normal routed active path.
Unknown live authority drift remains a reported migration blocker rather than
being overwritten. Reopen Phase 1 only when a concrete recurring user failure
or opportunity supplies selected advice bytes and makes an advice-off versus
simple-sidecar comparison worth running.

### Phase 1 — isolated comparison

- Curate candidate advice from existing evidence in an isolated experiment
  scope.
- Run the A/B/C and retraction comparisons above without altering ordinary
  production decisions.
- Treat failures as evidence about content, retrieval, model capability, or
  the surrounding route before proposing a mechanism.

### Phase 2 — one bounded consumer

Only if the comparison favours represented advice:

- add one optional batched active-advice projection at the winning decision
  point;
- add immutable revision, supersession, retirement, and exact read-back for
  that scope;
- migrate or retire only a demonstrably duplicate injector so the consumer has
  one advice projection, while preserving distinct episode and evidence stores;
  and
- verify absence, conflict, access denial, retraction, and rollback against the
  normal no-advice route.

After this local slice, cross-consumer reuse and autonomous maintenance are
independent branches. A second consumer does not prove safe autonomous release,
and useful local maintenance does not justify a common protocol.

### Post-pilot branch A — earn reuse

- Only when a second materially different current user job independently needs
  advice, test whether the same minimal query and lifecycle contract is useful;
  do not add a consumer merely to validate the abstraction.
- Reify advice items only if cross-target reuse or independent governance now
  provides measurable value.
- Keep specialised publication, knowledge-acquisition, tool, and outcome-
  explanation profiles specialised unless consolidation makes them simpler.
- Proceed with the shared protocol only if it removes real duplicated lifecycle,
  scope, projection, or telemetry work. If the second consumer needs shared
  domain branches, a larger universal payload, or a common ranker, retain the
  specialised implementations and stop generalising.

### Post-pilot branch B — bounded autonomous revision

Do not attach autonomous advice activation to the current per-episode
self-improvement launch. If the earlier phases reveal a worthwhile recurring
maintenance job, select one bounded campaign containing only the failure modes
that could change its release decision. Candidate probes include:

- a signal/lineage fixture with duplicate roots, weak critique, untrusted
  content, independent evidence, and an allowed no-durable-lesson result;
- a shadow A/B/C promotion, revision, and retract-to-empty crossover; and
- a drift/scope/budget sentinel covering the dependencies and actor boundary
  material to that candidate.

Scope leakage, self-confirming efficacy evidence, and failure to retract the
first release to empty remain universal stop-ship conditions. Other probes are
selected proportionately. When the claim includes autonomous generation, the
frozen generator output—including `no durable lesson`, revision, or retraction—
must feed the shadow release test; substituting a curated good candidate tests
release mechanics only.

Run a live canary for one low-consequence private binding only if the selected
campaign supports it under standing release authority. Use existing
operational-learning mechanics where their exact namespace/user/organisation
scope and evidence model fit; add a lighter retract-to-empty primitive rather
than pretending failure-only registration and previous-release rollback are
already generic. Do not auto-activate from one episode, evaluator, or aggregate
score.

### Future role- and team-level application — not a current phase

Role-linked advice becomes a plausible later application only after a current
user job exposes a recurring role-performance problem, role/responsibility/
work-product/handoff identities exist, the simpler role prompt or workflow
baseline is inadequate, and post-pilot branch A has independently earned shared
reuse. It can then help Von retrieve before asking, choose useful work products
and handoffs, recover with less interruption, and preserve team practice. It
cannot assign responsibilities, infer permission, or replace the role's
authority and commitment model.

A SAIL research-operations role is an illustrative candidate from Von's
programme direction, not selected scope in this design. Any pilot keeps
personal, team, and organisation-visible bindings distinct, preserves human and
AI contributor identities and dissent, and compares role advice with the
simpler role prompt/workflow. Stable practice graduates into that role's prompt,
workflow, playbook, validator, or typed policy and the experimental advice is
retired rather than leaving two steering paths.

This sequence does not begin by rewriting all prompts, routing profiles,
planner hints, or workflows into a common advice ontology.

## 12. Acceptance, stop-ship, and removal conditions

### 12.1 First consumer and autonomous canary

The first runtime adoption is supportable only if:

- Arm B establishes that the advice content improves a predeclared user outcome
  or successful-path efficiency over A;
- Arm C preserves that benefit within its declared online overhead bound and
  materially improves a predeclared operational maintenance outcome over the
  equally versioned sidecar;
- a lifecycle measure counts only when it improves an operational outcome such
  as time, error rate, or human burden in correcting stale advice—not merely
  because revision and rollback function;
- the claimed latency includes retrieval and context cost;
- no advice, inaccessible advice, and resolver failure preserve the ordinary
  path;
- actor-scoped advice neither leaks nor affects another actor's projection;
- advice cannot grant authority or remove an otherwise valid candidate;
- conflicting advice is explicitly resolved or surfaced to the decision rather
  than collapsing through an accidental precedence rule; and
- revision, retirement, rollback, and canonical read-back work on the exact
  active revision.

Before autonomous activation, also verify only the items material to that
claim: root-correlated evidence counts once; the candidate author cannot alter
its approval evidence; duplicate storms remain within budget and do not affect
turn latency; declared drift and actor-scope changes behave exactly; AI feedback
cannot grant publication authority; the first active release retracts to empty;
and emergency withdrawal still works when the ordinary maintenance budget is
exhausted.

### 12.2 Stop-ship conditions

Stop ship for the affected consumer if advice causes an authority violation,
wrong durable effect, cross-scope disclosure, stale retracted projection,
suppression of explicit user intent, disappearance of a valid recovery,
uncontrolled append-only context growth, self-confirming promotion evidence,
candidate/evaluator contamination, scope widening through feedback, uncontrolled
promotion/retraction churn, failed exact retraction, or maintenance that blocks
the ordinary task.

If retraction fails, do not mark the release retired. Disable projection through
the consumer's no-advice bypass, invalidate derived caches, verify absence from
the model-visible context, report stop ship, and reconcile canonical state.
Mark retirement only after canonical read-back confirms removal.

### 12.3 Removal at three levels

Retire an **advice release** immediately when its scope is wrong, a declared
dependency invalidates it, it contradicts newer trusted evidence, it suppresses
a valid route, or its independently verified outcome is materially harmful.
Retire it after its observation window when it makes no beneficial decision
change, current model/tool behaviour dominates it, or it duplicates an existing
prompt or workflow profile.

Remove a **consumer integration** when:

- advice-off or the specialised native surface is non-inferior and cheaper;
- applicable advice is too rare to amortise lookup and maintenance;
- its adapter requires consumer-specific branches in shared code; or
- maintenance cost exceeds the burden or failed work it avoids.

Collapse or remove the **shared protocol/layer** when:

- fewer than two independently justified consumers remain;
- reuse amounts only to renaming fields into a common transfer object;
- shared changes repeatedly require domain-specific conditionals or a central
  ranker;
- retrieval and maintenance cost exceed the duplicated work removed;
- the direct/no-advice routes stop receiving equivalent maintenance; or
- autonomous promotion cannot retain evidence independence and retract-to-empty.

Persistently low candidate yield is evidence that the maintenance worker should
run less often or disappear, not a reason to relax promotion quality.

### 12.4 Subtraction order

When removing advice machinery:

1. deactivate the affected releases and read back the exact empty or prior
   active selection;
2. restore and verify the consumer's direct/no-advice path;
3. retain useful content in the simpler prompt/profile only when evidence
   supports it;
4. remove unused injector, adapter, cache, resolver, maintenance and scheduled
   trigger paths, derived manifests, configuration, seed registrations, and
   materialisation hooks;
5. preserve outcome/authority tests and raw evidence for audit without prompt
   projection, while removing route-specific tests that fossilise the deleted
   mechanism; and
6. remove unused common vocabulary only after confirming that no remaining
   consumer depends on it.

Raw episodes and evaluation evidence may remain for audit after active advice
is removed. They must not continue to shape prompts through an undeclared
fallback.

## 13. Open questions

A future pilot should resolve, rather than assume:

1. Is an attached text relation sufficient, or does independent advice identity
   materially improve revision and reuse?
2. Which remaining specialised advice-like projections are active and useful
   on a current user-job path, beyond the now-subtracted dormant
   workflow-experience route?
3. Can the existing workflow capability projection carry advice cheaply enough,
   or is a separate actor-aware query needed?
4. How much applicability should be typed versus left to model judgement?
5. When may a trusted actor-bound release process automatically promote
   low-consequence private advice under predeclared evidence and rollback
   criteria, and when should shared organisational advice require explicit
   review?
6. Does role advice belong on role/responsibility concepts, or is it usually
   better expressed in the conversation situation and workflow metadata?
7. At what point should a recurring advice item graduate into a prompt,
   workflow, or independently justified invariant?
8. Does a shared projection protocol remove measurable lifecycle and scope work
   for a second consumer without acquiring domain branches or a common ranker?
9. Which actor-private maintenance effects have standing release authority, and
   how should a team inspect or revise that delegation without per-item review?
10. Does role-linked advice reduce team burden and improve work products, or
    merely make Von's existing habits more persistent?

## 14. Current handoff decision

The architectural decision is **retain federated convergence of soft guidance
as a falsifiable hypothesis**. The completed Phase 0 implementation decision is
**subtract the dormant workflow-experience machinery and stop; do not implement
a consumer, resolver, A/B/C framework, maintenance worker, or general advice
layer**.

No first consumer is selected. Reopen that decision only when a concrete
recurring user-job failure independently motivates candidate advice and the
advice-off versus simple versioned-sidecar comparison. Represented retrieval
must then preserve any measured end-to-end benefit and improve a real
maintenance outcome over the same selected bytes, with exact retract-to-empty
and no material authority, latency, evidence-independence, or negative-transfer
regression. A shared protocol still requires a second independently motivated
consumer and measured removal of duplicated lifecycle/scope work without
imported semantics. Autonomous activation remains a later, separately earned
decision.

Related guidance:

- [Prompt programs and model routing](prompt_programs_and_model_routing_playbook.md)
- [Agent memory and enduring knowledge](agent_memory_and_enduring_knowledge.md)
- [Agent evaluation and research uptake](agent_evaluation_and_research_uptake.md)
- [Minimal imposition](minimal_imposition_design_principle.md)
- [Security considerations](security_considerations.md)
- [Testing workflows and ephemeral theories](testing_workflows_ephemeral_theories_design.md)
- [Ontology publication authority](ontology_publication_authority.md)
- [Reliability Ratchet source and cases](reliability_ratchet_articles_and_cases.md)
