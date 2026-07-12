# Von for Agentic AI

- **Kind:** Long-horizon programme and target-architecture design
- **Lifecycle:** Active as advisory design; implementation snapshot frozen
- **Authority:** Advisory; `AGENTS.md` and current explicit user direction govern
- **State evidence as of:** 24 April 2026
- **Freshness boundary:** Durable target-direction arguments may remain useful;
  all “current”, “already”, “still”, code-size, gap, and priority claims require
  live revalidation
- **Scope:** Von's architectural direction and the path from a capable
  neuro-symbolic assistant toward minimal-imposition agentic AI in the stronger
  Von sense

## 1. Purpose

This note records the architecture Von is actually moving toward, not merely an aspirational marketing summary.

It is intended to do four things:

1. state clearly what class of system Von is trying to become;
2. distinguish recent real architectural progress from stale earlier diagnoses;
3. identify the most important remaining gaps between the present system and Von's minimal-imposition agent platform vision;
4. make explicit the near-term proving ground in Strong AI Lab research operations and role learning;
5. give engineering and research guidance for how to close those gaps without sliding back into "LLM wrapped in Python heuristics".

This is not a full changelog. It is an architecture and programme note.

## 2. Executive Summary

Von should not evolve into a larger and larger chat application with more ad hoc Python control logic around an LLM.

The intended direction is a **deployable neuro-symbolic agent platform** in which:

- enduring knowledge lives in explicit represented form rather than only in latent model state;
- durable behavioural policy is authored in Vontology, workflows, prompt programmes, KB assertions, and other inspectable authority surfaces;
- Python provides reusable support surfaces such as execution, validation, telemetry, persistence, integration, and, where implementation in Python will not compromise agentic generality, safety;
- a **portfolio of model scales** is used intentionally rather than pretending one model class should do everything;
- the system learns and adapts by promoting evidence-backed knowledge, workflows, prompts, and policy artefacts into durable represented form;
- knowledge representation may be in many forms: human or AI authored natural-language documents in any language; propositions in a logical language based on predicate calculus; knowledge in stronger, higher-order, contextual, temporal, or modal logics, with the representational power of the Cyc KB as a useful baseline; embeddings; and other forms;
- the agent acts with **minimal imposition**: it searches, retrieves, reasons, and explains before burdening people with clarification or process disruption;
- long-horizon reasoning, episodic memory, explicit provenance, and safe revision matter as much as immediate answer quality.

Recent work moved Von materially in that direction. The architecture is cleaner than it was ten days earlier, but the programme is not finished. Several major semantic-policy surfaces still remain in Python, enduring memory is still immature, multi-agent coordination is still mostly a future shape, and continual self-improvement remains more a direction than a fully closed operational loop.

The near-term proving ground should be more explicit than earlier versions of
this note made it. Von should first become useful by improving process and
operational effectiveness for AI research in the Strong AI Lab, especially by
learning to perform otherwise hard-to-fill research-operations roles with
minimal imposition. From there it should generalise to scientific research,
discovery and R&D more broadly, then to auxiliary organisational roles, and
eventually to roles in general. This staged path narrows the immediate focus
without narrowing the AGI ambition: SAIL research operations are the first
living laboratory for the broader role-learning architecture.

## 3. Current Architectural Assessment

### 3.1 Real progress already made

The most important recent progress is not merely code motion. Several major policy surfaces have already been removed from Python or pushed closer to represented authority.

#### 3.1.1 Workflow discovery and routing

Recent landings removed some of the worst old lexical routing doctrine:

- workflow discovery no longer depends on the old BM25 / stopword capability-index path;
- structured tool-family availability is no longer driven by prompt-keyword and prefix heuristics;
- turn expected-outcome contract handling is now a first-class cross-stage boundary rather than loose per-case plumbing;
- explicit predicate-relative ontology turns now have a proper intermediate support surface through `get_predicate_incidence(...)` rather than relying only on broad relation-hit paging.

This is a meaningful shift away from language-specific pseudo-NLP toward retrieval-backed and authority-aligned support surfaces.

#### 3.1.2 Domain-interpretation cleanup

Multiple domain services that previously held English-language or regex-heavy semantic policy in Python have been moved toward authority-backed interpretation surfaces, especially in:

- file-copy interpretation;
- workflow authoring and annotation extraction;
- write-request evidence inference.

That matters because it reduces the tendency for "temporary" deterministic semantic logic to become the real policy substrate.

#### 3.1.3 Turn correctness and postcondition evaluation

The turn-correctness path has improved significantly.

Committed work through `JVNAUTOSCI-1956` stopped grounded false-negative turns from being silently marked as verified success and removed the remaining low-information answer-surface fallback from the primary completion path. The prompt-backed postcondition critic now exists as an explicit subworkflow, with:

- a dedicated prompt seed for the critic;
- explicit evidence-bundle construction;
- a subworkflow pathway for authoritative evaluation;
- tests that exercise this critic pathway as a workflow-level support surface.

That is the right direction. Correctness judgement should not remain a frozen Python string-matching layer.

#### 3.1.4 Anti-drift and test quality

Recent work also improved the meta-architecture:

- anti-drift purity checks now guard several reclaimed support surfaces;
- stale tests that pinned removed heuristics have been repaired;
- real-path and near-real-path validations are increasingly being used instead of relying only on unit-shaped assertions.

That matters because without this layer, the codebase would continuously regress back to heuristic patches.

### 3.2 The current code reality is still mixed

Despite the above progress, Von still has major oversized integration files.

As of 21 April 2026:

- `src/backend/integrations/internal_mcp/orchestrator.py` is still `34,608` lines
- `src/backend/integrations/internal_mcp/catalogue.py` is still `27,466` lines
- `src/backend/server/routes/von_routes.py` is still `13,348` lines

So the direction is improving, but the largest files are still large enough to attract inline patching pressure and hidden policy drift.

### 3.3 What is still wrong in the current architecture

The most important surviving issues are now more concentrated.

#### 3.3.1 The contract-text tool-inference seam has now been removed

The April 21, 2026 bounded `JVNAUTOSCI-1913` / `JVNAUTOSCI-1972` cleanups moved
turn-contract tool requirements onto the explicit `required_tools` field of the
shared `TurnExpectedOutcomeContract` support surface and removed the remaining
contract-text tool-inference and prose-backfill helpers from the orchestrator.

That means the live path no longer decides Jira, KB, predicate/relation,
relation-argument, or web retrieval requirements by scanning English contract
prose inside Python.

The next risk is therefore no longer this semantic-policy seam. It is the
structural pressure created by the remaining orchestrator monolith and the
remaining represented-metadata follow-through tasks.

#### 3.3.2 The earlier write-policy hotspot has already moved

`JVNAUTOSCI-1970` removed the remaining live write-denial heuristic and hard-coded write-risk classes from the main write path. Earlier draft language pointing at `_ADDITIVE_LOW_RISK_WRITE_TOOLS`, `_MUTATIVE_NON_DESTRUCTIVE_WRITE_TOOLS`, `_DESTRUCTIVE_WRITE_TOOLS`, `_CONFIRMATION_PATTERN`, `_DESTRUCTIVE_MUTATION_PATTERN`, and `prompt_explicitly_denies_write(...)` is therefore stale as a guide to the next architecture step.

The broader lesson about multilingual policy risk still matters, but it is no longer the sharpest remaining Python-policy seam.

#### 3.3.3 Buttonify had one remaining residual authority seam

The live buttonify path already ran through `#V#chat_buttonify_workflow` and structured `buttonify_options_json` validation. The remaining problem was narrower: `src/backend/services/buttonify_service.py` still carried residual heuristic/prose-recovery helpers, and stale docs/tests made it look as if Python could still reconstruct UI options from prose.

`JVNAUTOSCI-1971` removes that residual helper slab so buttonify options now come only from the authority-backed structured output contract, with Python limited to validation, normalisation, and telemetry.

#### 3.3.4 Predicate-incidence is the right abstraction but may become expensive

The new `get_predicate_incidence(...)` surface is architecturally good, because it fills a real gap between:

- `entity -> relation hits`
- and `predicate -> extent`

However, the current type-mode implementation gathers all relevant instances and then aggregates relation hits across them. For broad classes or high-degree entities, this is a real runtime and deployability concern.

That means the design is correct, but the scaling strategy still needs work.

### 3.4 A note on current state discipline

One live engineering caution is worth making explicit.

The main risk is now documentation lag rather than an unmerged critic prototype. High-level notes need to distinguish carefully between:

- what is already committed and merged;
- what exists only in a current working tree;
- what remains aspirational.

That distinction matters because otherwise the repo risks documenting local intent as if it were already part of the durable shipped architecture.

## 4. The Architecture Von Should Be Becoming

The correct target is not "an LLM plus a growing pile of wrappers". It is a **capability-composed agentic system** with explicit, revisable, inspectable cognition.

### 4.1 Vontology as the cognitive substrate

Vontology should be treated as a first-class durable substrate for:

- semantic memory;
- represented concepts, predicates, and relations;
- provenance-bearing knowledge;
- workflow artefacts;
- prompt programmes;
- policy metadata;
- long-horizon organisational state.

This is crucial to Von's ambition. A system that only "remembers" through latent weights or ephemeral vector recall cannot be trusted to adapt over months or years while preserving inspectability, revision, and provenance.

Von should therefore continue treating durable represented state as an implementation surface, not merely a cache.

For the concrete Vontology tool families implied by this direction, see
`docs/engineering/vontology_tooling_from_ka_kcap_kr_literature.md`. That note
connects KA, KCAP, commonsense KR, Cyc-style contextual representation, and
scientific KR literature to proposed Vontology read/write/support tools.

### 4.2 Workflows, prompts, and contracts as behavioural authority

Durable behaviour policy should be authored in:

- VWL workflow definitions;
- prompt concepts and text relations;
- represented routing metadata;
- KB assertions and explicit constraints;
- contract objects such as turn expected-outcome contracts and required-effects contracts.

Python should not quietly decide ranking, matching, recommendation, retrieval strategy, or answer semantics through lexical helpers and conditionals if those policies can be represented cleanly elsewhere.

### 4.3 Shared turn context as a first-class surface

A genuinely agentic system cannot allow each stage to see materially different hidden contexts unless that reduction is explicit, justified, and telemetry-visible.

Von should continue pushing toward:

- one accumulated turn context reused across selector, planner, tool use, critic, and narration stages;
- explicit stage-local additions where needed;
- explicit telemetry of what each stage actually saw.

Without this, Python becomes an invisible policy layer even when the prompts live in Vontology.

### 4.4 Model hierarchy and capability composition

Von should assume a model portfolio rather than a monolithic-model doctrine.

That means:

- smaller models for bounded, cheap, frequent, structurally constrained work;
- stronger models for difficult synthesis, planning, ambiguity resolution, and critic functions;
- specialised symbolic or deterministic modules for validation, persistence, and explicit typed structure;
- routing and escalation policies that are inspectable and revisable.

This aligns directly with the Von view that robustness should come from designed capability composition rather than pure parameter scaling.

### 4.5 Explicit memory strata

Von needs to treat memory as a set of distinct architectural layers, not one big "RAG" bucket.

At minimum it should continue toward:

- **semantic memory:** durable facts and relations;
- **episodic memory:** traces of interactions, workflows, and long-running organisational processes;
- **procedural memory:** workflows, prompts, playbooks, validators;
- **policy memory:** learned routing guidance, critiques, context-conditional advice, and other reusable operational knowledge.

This is essential if Von is to support long-horizon reasoning, continual learning, and adaptive deployability without repeatedly re-deriving everything from transient conversation context.

### 4.6 Minimal-imposition operational behaviour

Von should continue optimising for a form of helpfulness that reduces burden rather than only maximising answer fluency.

That implies:

- search and retrieval before clarification;
- explicit uncertainty and provenance instead of bluffing;
- preference for low-risk additive actions over unnecessary permission loops;
- escalation for destructive or high-impact changes;
- preserving institutional and scholarly practice rather than flattening it into a narrow internal schema.

Minimal imposition is not merely a UX preference. It is a central operational criterion for real-world deployability.

### 4.7 Critic and evaluator workflows as first-class citizens

A powerful agent platform needs not only planners and executors, but also explicit evaluator and critic components.

Von should therefore treat critic workflows as standard architecture, not an afterthought. This includes:

- postcondition critics;
- evidence-answer consistency critics;
- uncertainty and failure-surface critics;
- recovery-decision workflows;
- long-horizon reflection and promotion workflows.

The landed prompt-backed postcondition critic is therefore important not just as a bug fix, but as part of the right architectural doctrine.

### 4.8 Capability plug-ins and secure deployment surfaces

Von's longer-term platform direction points toward a "suite" of capability plug-ins deployable into partner and Living Lab environments, including sensitive environments where raw data cannot simply be exported.

Von therefore needs to become a clean capability platform, not just an application bundle. That implies:

- well-bounded integration surfaces;
- deployable plugin/capability modules;
- explicit trust, provenance, and namespace boundaries;
- a clean split between represented authority and runtime substrate;
- support for private local deployment with selective escalation to stronger remote models when policy allows.

### 4.9 Role learning as the near-term proving ground

Von's most important near-term application spine should be
**minimal-imposition role learning**.

The system should learn how to take on useful roles in an organisation or team
when those roles are otherwise hard to fill because of availability, cost, or
capability constraints. For the current programme, the first target should be
process improvement and operational effectiveness for AI research in the Strong
AI Lab.

The staged path is:

1. **SAIL AI research operations:** help with real research-process drag,
   continuity, literature and evaluation support, project tracking, meeting and
   action-note follow-through, artefact curation, and onboarding.
2. **Science, discovery, and R&D:** generalise from SAIL to broader research
   workflows, experiment and evaluation management, interdisciplinary
   collaboration, and discovery-support work.
3. **Auxiliary organisational roles:** extend to coordination, administration,
   communications, compliance, support, and other roles around research teams.
4. **Roles in general:** abstract the role-learning machinery so new
   organisations can safely onboard Von into hard-to-fill roles through bounded
   observation, represented role modelling, evaluation, and improvement loops.

The role itself should become a represented object, not a prompt persona or a
Python role-name branch. A role-performance representation should include:

- responsibilities, authority boundaries, permissions, and escalation rules;
- recurring workflows, process states, handoffs, and interaction patterns;
- expected work products and quality/evidence contracts;
- notes, dossiers, evidence receipts, and episodic traces;
- procedural memory such as workflows, prompts, skills, validators, and
  playbooks;
- policy memory such as role-specific guidance, routing rules, model choices,
  and learned improvement advice;
- evaluator axes for operational effectiveness, burden reduction, work-product
  quality, groundedness, calibration, recovery, and long-horizon continuity.

This is where the AGI-shaped substrate becomes operationally concrete. Von
should learn from work episodes, promote useful traces into semantic,
episodic, procedural, and policy memory, test candidate role improvements in
bounded worlds, and publish improvements only through represented,
evaluable, rollback-aware artefacts.

## 5. Engineering Implications of This Architecture

This architecture changes how engineering work should be understood.

### 5.1 More behaviour work becomes knowledge and workflow engineering

Many changes that look like "implementation" in ordinary software should be authored in:

- represented knowledge;
- prompt concepts;
- workflow definitions;
- critic/evaluator artefacts;
- reusable contracts and metadata.

That is not avoiding engineering. It is doing engineering at the correct authority layer.

### 5.2 Monolith drift is the main enemy

Large integration surfaces naturally attract quick fixes. In Von, that is particularly true of:

- `orchestrator.py`
- `catalogue.py`
- `von_routes.py`

The main danger is not only file size. It is that mixed responsibilities invite:

- hidden routing policy;
- hidden verification policy;
- stage-specific context shaping;
- answer-surface repair patches;
- integration-boundary drift.

### 5.3 Evaluation has to become richer and more honest

This architecture cannot be validated by ordinary unit tests alone.

Useful evaluation must include:

- exact or near-real call-path tests;
- replay-based failure analysis;
- retrieval quality and answer-grounding assessment;
- long-horizon memory tasks;
- calibration and uncertainty evaluation;
- interruption burden and minimal-imposition metrics;
- robustness across paraphrase, multilingual phrasing, and organisational variation;
- cost and latency effects of richer multi-stage cognition.

### 5.4 Telemetry is not optional polish

If Von is to become a self-improving, inspectable agent platform, telemetry must remain first-class.

That includes:

- stage context lineage;
- workflow discovery and routing traces;
- selected-workflow traces;
- evidence bundles for critic stages;
- completion-gate diagnostics;
- recovery-route traces;
- long-horizon episodic records that can later be re-analysed or promoted.

### 5.5 Security, provenance, and namespace discipline stay central

A minimal-imposition agent that works in sensitive real environments cannot trade away access safety for convenience. Namespace discipline, provenance preservation, and clear authorisation boundaries must remain part of the design, not bolt-ons.

### 5.6 Role-learning work must consume the shared substrate

Role-learning implementation should not create a parallel stack.

When Von learns a role, the evidence and improvements should flow through the
same architectural surfaces already being built:

- enduring-memory manifests and query surfaces for role episodes, work products,
  dossiers, notes, and evidence receipts;
- memory-promotion workflows for role facts, process knowledge, procedural
  artefacts, and policy guidance;
- multi-axis evaluators for grounded helpfulness, calibration, recovery,
  continuity, operational effectiveness, and work-product quality;
- candidate worlds for evaluating workflow, prompt, skill, model-routing, or
  retrieval-policy changes before promotion;
- Vontology-authored workflows, prompt concepts, role concepts, and policy
  artefacts as the durable authority surfaces.

Python may add reusable support for manifests, validators, telemetry,
connectors, durable execution, and benchmark harnesses. It should not become
the place where SAIL-specific or role-specific task policy is encoded.

## 6. What Is Still Missing on the Path to Fully Helpful Agentic AI

The biggest gaps are now less about obvious hacky routing and more about missing higher-order capabilities.

### 6.1 Remaining code-side semantic policy removal

Von still needs to remove the remaining Python-owned semantic policy surfaces,
especially:

- any remaining hard-coded tool and risk metadata that belongs in represented authority;
- any further decision-policy remnants that are still living inside oversized orchestration helpers instead of represented authority surfaces or clearly bounded support code.

This is still necessary groundwork. A system cannot become truly multilingual, general, and maintainable while core policy still depends on hidden English token lists.

### 6.2 Enduring memory and consolidation

Von has the right semantic-memory direction but still lacks a mature explicit memory architecture for:

- episodic trace retention and retrieval;
- promotion from episodic evidence to durable semantic knowledge;
- revision and retraction pathways;
- policy-memory induction from repeated failures or successes;
- long-horizon task-state reconstruction across interruptions.

Without this, Von will remain a capable turn-by-turn assistant rather than a truly enduring collaborator.

The Vontology tooling design note at
`docs/engineering/vontology_tooling_from_ka_kcap_kr_literature.md` should be
used as the current companion for graph, provenance, context, Davidsonian
role-frame, scientific-claim, and knowledge-acquisition tool surfaces that make
this memory architecture usable from workflows.

### 6.3 Safe continual self-improvement

Von's ambition requires gain-of-function introspection and adaptive self-evolution. That means Von needs more than prompt tweaks and Jira tasks after failures.

It needs explicit self-improvement loops for:

- benchmark and replay analysis;
- prompt and workflow revision proposals;
- retrieval-policy learning;
- policy-memory induction;
- isolated testing of candidate changes;
- promotion only after evaluation and rollback-safe validation.

The right model is not unconstrained self-modification. It is controlled improvement through represented, evaluable, promotable artefacts.

### 6.4 Long-horizon temporal and institutional reasoning

To support months-long or years-long processes, Von needs stronger representations and retrieval for:

- temporal structure;
- event sequences;
- institutional procedures and obligations;
- workflow histories and state transitions;
- evolving user and team preferences;
- conflict, uncertainty, and revision over time.

The current architecture is becoming capable of this, but not yet fully equipped for it.

### 6.5 Multimodal knowledge acquisition and fusion

Von's target architecture points toward integration across text, images, speech, and other data sources. Von has made progress on file-copy interpretation, but a mature multimodal architecture still requires:

- common provenance models across modalities;
- authority-backed interpretation surfaces for multimodal evidence;
- cross-modal linking into Vontology;
- uncertainty-aware fusion rather than silent flattening into text summaries.

### 6.6 Multi-agent coordination

The current architecture is still primarily single-agent in its core lifecycle, even though it already contains many of the pieces needed for richer composition.

What is still missing includes:

- explicit agent identities and capability profiles;
- safe delegation and subtask ownership;
- shared episodic and semantic state across collaborating agents;
- coordination protocols for handoff, arbitration, and conflict resolution;
- evaluation of multi-agent failure modes such as context divergence and duplicated work.

This is essential for Von's goal of coordinated agent systems in complex domains.

### 6.7 Normative governance beyond simple obedience

A deployable real-world agent needs to bridge rules, norms, uncertainty, and practical adaptation.

Von still needs stronger explicit handling of:

- organisational norms;
- cultural and institutional constraints;
- policy pluralism and conflict;
- uncertainty-bearing recommendation rather than false certainty;
- documented reasons for abstention, escalation, and refusal.

### 6.8 User-facing transparency and calibration

A fully helpful agent is not just one that retrieves better. It is one that:

- exposes what it knows and how it knows it;
- distinguishes evidence from conjecture;
- reveals uncertainty when it matters;
- remains answer-first while still preserving inspectable operational traces;
- helps people trust the right things and distrust the right things.

Recent work on critic pathways and answer-first discipline helps here, but the broader transparency model is still incomplete.

### 6.9 Role-performance representation and evaluation

The current architecture still does not make role performance first-class
enough.

Von needs a canonical way to represent:

- a role in an organisation or team;
- the role's responsibilities, obligations, authority boundaries, and
  escalation points;
- recurring workflows, interactions, handoffs, and process states;
- work products, review standards, and acceptance evidence;
- notes, artefacts, meetings, decisions, and follow-up state;
- the skills, prompts, models, tools, and other capabilities used to perform the
  role;
- evidence that the role is being performed better over time.

This is especially important for the near-term SAIL research-operations aim.
Without this representation, Von risks becoming a collection of helpful
features rather than a system that can learn an under-filled role, improve at
it, and generalise that learning to new research teams and eventually to new
organisational roles.

## 7. A Concrete Path Forward

### 7.1 Near-term engineering priorities

The most important near-term engineering work has two connected tracks.

First, continue the substrate work that prevents Von from regressing into a
large Python application with prompts attached:

1. finish replacing remaining Python semantic policy surfaces with represented authority or prompt-backed critic/evaluator surfaces;
2. continue extracting mixed responsibilities out of the orchestrator and route monoliths;
3. keep anti-drift tests and purity checks aligned with the newer architecture;
4. harden scaling and telemetry around new support surfaces such as predicate incidence;
5. keep documentation and programme notes aligned with the actually landed authority surfaces so follow-on work targets the right remaining seams.

Second, start the role-learning application spine:

1. define the canonical role-performance representation for SAIL research
   operations;
2. identify a small set of SAIL process-improvement pilot roles and their work
   products;
3. connect those pilots to enduring-memory, evaluator, and candidate-world
   tasks rather than inventing a separate role stack;
4. extend minimal-imposition evaluation to measure operational effectiveness,
   avoided burden, workflow continuity, work-product quality, and team adoption
   friction;
5. use the SAIL pilots to discover missing reusable Vontology, workflow,
   evaluator, and telemetry primitives.

### 7.2 Medium-term platform priorities

After that, the most important platform-level advances are:

1. explicit episodic memory and memory-promotion workflows;
2. isolated self-improvement and benchmark worlds;
3. richer retrieval and reasoning over time, events, and workflows;
4. structured multimodal evidence acquisition and fusion;
5. capability packaging for partner deployment.

### 7.3 Longer-horizon Von priorities

To reach the fuller Von vision, Von should grow into:

- a compositional multi-agent platform;
- a secure deployable capability suite for partners;
- a system that can improve through represented policy and knowledge promotion;
- a long-horizon collaborator that preserves and revises knowledge over months or years;
- a platform that scales by combining specialised capabilities, explicit knowledge, and model hierarchy rather than only chasing larger parameter counts.

### 7.4 Current programme umbrella in Jira

The current umbrella Jira epic for this broader research and engineering direction is
[`JVNAUTOSCI-1960`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1960), which now functions as the main shaping epic for the broader minimal-imposition agentic AI programme around Von.

That epic is intended to **shape** the programme rather than prematurely overdetermine it. It does not replace the anti-hack and authority-alignment substrate work under `JVNAUTOSCI-1116` / `JVNAUTOSCI-1913`; instead, it integrates that line with the wider memory, evaluation, multimodal, multi-agent, and deployment questions required for a true minimal-imposition agent platform.

The near-term role-learning application spine is tracked separately in
[`JVNAUTOSCI-2011`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2011).
That epic should consume the `JVNAUTOSCI-1960` substrate rather than compete
with it. It exists to make the practical first deployment path explicit:
minimal-imposition role learning for SAIL research operations, then broader
science and R&D, then auxiliary organisational roles, then roles in general.

The first design-framing task for that epic is
[`JVNAUTOSCI-2012`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2012).

At the time of writing, its main child tasks are:

- [`JVNAUTOSCI-1961`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1961): define the minimal-imposition benchmark and deployment acceptance model for Von
- [`JVNAUTOSCI-1962`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1962): define the enduring-memory and memory-promotion architecture across semantic, episodic, procedural, and policy memory
- [`JVNAUTOSCI-1963`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1963): build isolated self-improvement loops with ephemeral theories and benchmark worlds
- [`JVNAUTOSCI-1964`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1964): expand authority-backed critic and evaluator workflows for grounded helpfulness, calibration, and long-horizon quality
- [`JVNAUTOSCI-1965`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1965): design represented multi-agent coordination, delegation, and shared-memory substrates for Von
- [`JVNAUTOSCI-1966`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1966): define the multimodal evidence-fusion and provenance architecture for Von
- [`JVNAUTOSCI-1967`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1967): design the capability-suite and secure partner-deployment architecture for Von
- [`JVNAUTOSCI-1968`](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1968): design the represented normative-governance, uncertainty, and escalation architecture for minimal-imposition deployment

Taken together, these tasks mark the transition from "clean up anti-patterns in today's assistant" to "deliberately build the research and deployment substrate for the next class of deployable, minimal-imposition agent systems".

## 8. Anti-Patterns to Keep Rejecting

To preserve the architecture, the following remain design smells:

- lexical pseudo-NLP in Python for durable routing, ranking, or classification policy;
- hidden phase-specific context thinning;
- prompt bodies in Python for Vontology-governed features;
- code-side answer-repair heuristics that should really be critic or workflow policy;
- role-name or organisation-name branches in Python that turn a SAIL pilot into
  hidden durable role policy;
- persona-only "role learning" that does not represent workflows,
  responsibilities, work products, evidence, and improvement lineage;
- treating vector search or transient context as if they were adequate substitutes for durable represented memory;
- treating one giant general model as if it removes the need for architecture.

## 9. Bottom Line

Von is increasingly becoming the right kind of system.

It is moving away from a brittle "application plus LLM" model and toward a represented, inspectable, capability-composed neuro-symbolic agent platform. Recent progress on workflow discovery, turn contracts, predicate-incidence retrieval, authority-backed interpretation surfaces, and critic/evaluation pathways is real.

But the work is not done.

The path to fully helpful minimal-imposition agentic AI still requires:

- removal of the remaining Python semantic policy seams;
- mature enduring memory and consolidation;
- safe self-improvement loops;
- first-class role-performance representation and SAIL research-operations pilots;
- multimodal knowledge fusion;
- multi-agent coordination;
- stronger temporal, institutional, and normative reasoning;
- continued discipline about represented authority, telemetry, and evaluation.

If Von continues in that direction, it can become not merely a useful assistant, but a genuine platform for the next class of deployable, provenance-bearing, minimal-imposition agent systems.
