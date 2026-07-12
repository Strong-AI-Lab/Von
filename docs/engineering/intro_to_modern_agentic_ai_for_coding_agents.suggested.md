# Superseded proposal: Von Agentic AI Architecture Constitution

> **Document status: Superseded proposal; non-authoritative.** This file was
> never the guide selected by `AGENTS.md`. Do not treat its “normative”, “main
> architecture guide”, or “mandatory” wording as current authority. Use
> [`intro_to_modern_agentic_ai_for_coding_agents.md`](intro_to_modern_agentic_ai_for_coding_agents.md)
> together with [`AGENTS.md`](../../AGENTS.md). This copy is retained only for
> design provenance.

- **Kind:** Unselected architecture proposal
- **Lifecycle:** Superseded
- **Authority:** Historical only
- **Original date:** 2026-04-04

## 1. Purpose

This document was proposed as the main architecture guide for coding agents
working on Von. It was not selected as the canonical guide.

It exists to prevent a recurring failure mode: treating modern agentic behaviour as if it were mainly ordinary application logic that should be hand-coded in Python, with prompts, workflows, knowledge representations, model portfolios, and learning loops treated as secondary decoration.

For Von, that is often backwards.

Von is being built as a deployable neuro-symbolic assistant system for teams, demonstrated first for research-team support. It must stay able to absorb newer models, fine-tunes, prompt optimisers, routing methods, memory architectures, and reinforcement-learning advances without repeatedly relocating decision policy into code.

### 1.1 Four Pillars of Agentic Evolution

To drive progress towards the vision of Von as an advanced neuro-symbolic assistant, the primary direction for Von's agentic evolution is built on four key pillars:

1. **Closing the Workflow Execution "Black Box" Gap**: Ensuring LLM-supervised workflow paths where all automated and sub-agent executions are observable and produce parseable Turn Execution Records.
2. **Materialising Ephemeral Theories**: Supporting self-evolution by allowing agents to generate, test, and either promote or discard temporary working models (ephemeral theories) for self-modification and capability discovery.
3. **Maturing Multi-Agent Coordination**: Extending the Von Workflow Language (VWL) and the Orchestrator to support robust agent-to-agent delegation, capability negotiation, and shared episodic memory contexts.
4. **Transitioning to True Policy Learning**: Moving away from manual prompt tweaks towards automated learning loops that parse Turn Execution Records to induce guidelines, update retrieval strategies, and dynamically adjust model routing.

## 2. When to read it

This document is not required by `AGENTS.md`. Read it only when investigating
design provenance or material that may merit deliberate promotion into a
current guide.

It is especially important when the task touches:

- ranking, recommendation, matching, routing, classification, planning, or evaluation
- prompts, tool use, workflow behaviour, or agent policies
- retrieval, memory, knowledge bases, or context construction
- ontology or representation design
- model selection, prompt evolution, fine-tuning, or policy learning
- research-sensitive or architecture-shaping work

## 3. What Von is architecturally

Von should be treated as a layered neuro-symbolic system, not as a single model wrapped in Python.

### 3.1 The enduring-knowledge substrate

This is the durable represented world:

- Vontology concepts, types, predicates, and text relations
- structured KB assertions with provenance and uncertainty
- workflow and policy metadata
- durable memories and profiles
- materialised outputs worth reusing across sessions, users, and workflows

This substrate is part of the system's cognition, not just a cache.

### 3.2 The workflow and control layer

This is where explicit behaviour structure should live when feasible:

- state and transition structure
- action selection scaffolding
- escalation, continuation, retry, and pause logic
- schedule, event, and binding behaviour
- policy surfaces that should remain inspectable and revisable

### 3.3 The prompt-program layer

Prompts are not comments. They are operational policy.

Prompt programs should be treated as:

- authored artefacts
- versioned behaviour surfaces
- optimisable modules
- objects with provenance, evaluation history, and rollback paths

### 3.4 The model-portfolio layer

Von should assume a portfolio rather than a single default model:

- small local or inexpensive models
- medium general-purpose models
- frontier models
- narrow fine-tunes or adapters
- non-neural symbolic or deterministic modules

The portfolio should be explicitly selectable, evolvable, and measurable.

### 3.5 The runtime-support layer

Python usually belongs here:

- execution surfaces
- validators and constraint checking
- tool wrappers
- rendering and transformation helpers
- telemetry and persistence
- replay, recovery, and safety support

If Python starts containing the durable task policy, the architecture is drifting.

### 3.6 The learning and adaptation layer

Von should be designed to improve through:

- prompt optimisation
- retrieval and context-policy improvement
- guideline induction from experience
- model routing and portfolio updates
- fine-tuning when justified
- bandit or RL-style decision-policy optimisation when rewards and trajectories are available

These learning loops should improve authoritative policy surfaces, not encourage policy to migrate into hidden code.

## 4. Core architectural stance

### 4.1 Behaviour authority matters

For Von, behaviour often ought to be authored in:

- Vontology concepts and text relations
- VWL workflow definitions and metadata
- prompt concepts and related policy text
- explicit KB artefacts with provenance
- typed relations and other inspectable structures

Python should usually provide only reusable support.

### 4.2 Decision policy is broader than routing

Decision policy includes:

- ranking
- recommendation
- matching
- classification
- retrieval strategy
- explanation choice
- planning
- model selection
- when to continue, ask, escalate, or stop

If these are being implemented as hidden Python heuristics, the architecture is usually wrong.

### 4.3 Inspectability is not the same as deterministic Python

A bounded lexical heuristic can look tidy, interpretable, and reviewable while still placing the real production policy in the wrong place.

Do not confuse:

- "I can read the code"
with
- "the system's policy is in the intended authority surface"

## 5. Reference architecture for substantial behaviour features

For any substantial behaviour feature, identify all of the following before implementation:

1. **Authoritative knowledge artefacts**  
   Which concepts, predicates, prompts, workflow definitions, memory objects, or KB assertions should exist?

2. **Policy surface**  
   Is the behaviour primarily in workflow, prompt program, symbolic representation, model router, or learned control policy?

3. **Runtime support**  
   What reusable code surfaces are actually needed?

4. **Candidate models**  
   Which models or fine-tunes are plausible, and what are their cost, latency, privacy, and capability trade-offs?

5. **Evaluation and telemetry**  
   How will success, failure, reliability, consistency, and cost be measured?

6. **Learning loop**  
   If the feature should improve over time, what data, rewards, or feedback signals will drive that improvement?

7. **Fallback strategy**  
   What bounded fallback exists, and why is it explicitly temporary rather than the durable policy?

If these questions are unanswered, planning is probably too shallow.

## 6. Prompt programs and self-improvement

### 6.1 Prompts are first-class policy objects

Every important prompt should have:

- an identity in Vontology
- version history
- owner or task provenance
- intended scope
- evaluation notes
- rollback path

### 6.2 Prompts should be improvable, not merely editable

For prompt-governed features, prefer an explicit optimisation loop where feasible:

- candidate generation
- offline evaluation set
- promotion and demotion criteria
- regression checks
- production telemetry
- rollback and lineage

### 6.3 Preferred improvement ladder

Prefer the lightest effective improvement loop:

1. prompt revision by design
2. retrieval/context improvement
3. experience-derived guidelines or reflections
4. prompt-program optimisation
5. model routing change
6. small fine-tune or adapter
7. RL-style policy optimisation

Do not jump to heavier interventions when a cheaper, more inspectable layer is still likely to help.

### 6.4 Self-improving prompts must still be governed

Self-improvement is not permission for uncontrolled mutation.

Any prompt optimisation path should define:

- objective function
- held-out or replay evaluation
- failure detection
- promotion threshold
- rollback condition
- human review threshold for high-risk domains

## 7. Model portfolios, routing, and fine-tuning

### 7.1 Model choice is part of the architecture

Von should not behave as if one model choice is permanently correct.

Different surfaces may need different substrates:

- small models for cheap routine transformations
- stronger models for difficult synthesis, planning, or ambiguity
- fine-tunes for narrow stable tasks with sufficient data
- symbolic components where correctness, traceability, or constraints dominate

### 7.2 Routing should be explicit and learnable

When model choice materially affects quality or cost, design for an explicit routing policy rather than scattered ad-hoc if/else rules.

The routing policy should be able to improve using:

- task metadata
- observed performance
- preference signals
- cost and latency targets
- offline benchmark results

### 7.3 Do not overfit the codebase to current model quirks

Avoid embedding today's model-specific workaround into durable code when it really belongs in:

- prompt policy
- routing policy
- evaluation logic
- validators
- workflow structure

### 7.4 Fine-tuning is useful but should be narrow and justified

Use fine-tunes or adapters when all of the following hold:

- the task is stable and recurring
- prompt and retrieval improvements are not enough
- appropriate data exists
- the deployment trade-offs are acceptable
- the resulting behaviour remains observable and testable

## 8. Enduring knowledge and memory

### 8.1 Memory taxonomy

Von should distinguish at least four forms of durable memory:

- **semantic memory**: stable facts, concepts, relations, references
- **episodic memory**: interaction and workflow histories
- **procedural memory**: reusable policies, workflows, prompt programs, playbooks
- **policy memory**: learned preferences, routing tendencies, or context-conditional guidelines

These should not be collapsed into one undifferentiated "memory" bucket.

### 8.2 Not all memory belongs in the same surface

Use the right surface for the right durability:

- transient scratch state in runtime memory
- reusable structured knowledge in Vontology or KB
- evaluated policy or guideline artefacts in durable policy memory
- long-horizon interaction traces as episodic records with promotion rules

### 8.3 Consolidation matters

Define when traces should be promoted into durable knowledge:

- when evidence is strong enough
- when the fact or pattern is reusable
- when provenance can be retained
- when the representation type is clear

Do not leave all learning trapped in latent model state or ad-hoc logs.

### 8.4 Retrieval is a policy surface

Retrieval and context construction are part of the behaviour policy.

This includes:

- what gets indexed
- how memory is chunked or structured
- what query transformations are used
- when graph or global retrieval is preferred over local retrieval
- how context budget is allocated

### 8.5 Use stronger structure when the task needs it

Use graph, temporal, causal, or other richer structures when the task materially depends on:

- global corpus sense-making
- long-horizon dependencies
- role structure
- temporal ordering
- causal constraints
- conflict resolution or revision

Do not assume plain similarity retrieval is always sufficient.

### 8.6 Also do not assume fancy memory beats a strong baseline

New memory designs should be compared against:

- the current production baseline
- a plain strong long-context baseline where feasible
- a simpler retrieval baseline

Do not introduce elaborate memory machinery only because it sounds advanced.

## 9. Learning from experience

### 9.1 Experience can improve more than weights

Von should learn from traces in several ways:

- reflection or verbal feedback
- context-aware guidelines
- better retrievers
- better routing policies
- better prompt programs
- fine-tunes
- RL-style control optimisation

### 9.2 Default progression for decision-policy learning

When improving decision quality over repeated tasks, prefer the following progression unless there is a strong reason not to:

1. collect trajectories and telemetry
2. add evaluators and validators
3. derive reflections or guidelines
4. improve prompt and retrieval policy
5. improve routing or contextual selection
6. use bandit or RL-style optimisation if the reward signal is stable and the state/action interface is explicit
7. use fine-tuning when the policy is stable enough to compress into weights

### 9.3 RL is promising, but not a substitute for architecture

RL-style optimisation is especially relevant when the task has:

- explicit or inferable state
- repeated decision points
- delayed reward
- measurable outcomes
- stable action interfaces

But RL does not remove the need for:

- explicit authority boundaries
- observability
- safe rollback
- high-quality evaluation
- durable represented knowledge

## 10. Representation and symbolic reasoning

### 10.1 Use the weakest adequate representation

Choose the lightest representation that preserves the structure you need.

Roughly:

- plain text for raw user-authored material
- typed entities and relations for stable references and provenance
- workflow/state structure for explicit control
- graph structure for connected multi-entity reasoning
- logical or quasi-logical forms when variables, roles, modality, temporality, normativity, or causal structure matter

### 10.2 Do not over-formalise for prestige

Formal representations are justified when they buy real leverage:

- better inference
- better validation
- better revision
- better explanation
- better reuse
- better auditability

They are not justified merely because they sound sophisticated.

### 10.3 Symbolic scaffolds are helpful but not magical

Adding structure does not guarantee good prediction or decision quality.

A symbolic scaffold can clarify the problem and improve inspectability, yet the model may still fail at the remaining judgement step.

Therefore:

- use explicit structure where it buys leverage
- keep the residual learned component visible
- evaluate the residual learned component directly

## 11. Evaluation doctrine

### 11.1 Benchmark optimism is dangerous

Do not accept a behaviour feature merely because it looks good on a single benchmark or single-shot success metric.

### 11.2 Evaluate the properties that matter for deployed assistants

Where relevant, measure:

- task success
- pass@k or pass^k style consistency
- policy adherence
- perturbation robustness
- calibration and uncertainty quality
- latency and cost
- retrieval hit quality
- long-horizon state retention
- failure visibility and recoverability

### 11.3 Test the nearest real path

Especially for user-visible features, acceptance should go through the real control path or the nearest faithful path, not a simplified harness only.

### 11.4 Evaluate changes in the right place

If a change claims to improve:

- prompt policy, evaluate the prompt-governed path
- routing, evaluate the router and its downstream outcomes
- memory, evaluate long-horizon retrieval and use, not only isolated recall
- reasoning, evaluate under structural perturbation where appropriate

## 12. Research uptake and staying current

### 12.1 Design for absorbability

Von should be easy to improve when new research becomes relevant.

That means:

- keeping authority surfaces explicit
- keeping runtime support generic
- logging the data needed for future learning loops
- separating policy from implementation scaffolding
- maintaining clean evaluation and rollback paths

### 12.2 Research-sensitive tasks require a literature note

For architecture-shaping or research-sensitive work, create a short task note that states:

- what recent work was checked
- what changed in the plan because of it
- what was rejected and why
- what open question remains

### 12.3 Prefer integration patterns, not trend chasing

A new paper should not automatically become production design.

Adopt a new method only when:

- it addresses a real Von bottleneck
- it improves the authoritative architecture rather than bypassing it
- it has a clear evaluation story
- it fits Von's deployment constraints

## 13. Common design smells

Pause and reconsider when you are about to write:

- a custom tokeniser for ranking, matching, or routing policy
- stopword lists or lexical-overlap scoring as production semantics
- fixed additive weight tables for durable recommendation or classification policy
- a growing tree of if/else rules that mimics workflow logic
- repo-side workflow/template/prompt files that silently become authoritative
- code-side prompt defaults for a Vontology-governed feature
- hidden model-selection logic scattered through helper code
- a memory system with no promotion, revision, or uncertainty rules
- a fancy retrieval or memory subsystem that has not beaten a simple baseline
- an RL claim without explicit state, action, reward, and rollback definitions

## 14. Planning checklist for coding agents

Before implementing substantial behaviour changes, answer these briefly in task notes or Jira comments:

1. What part of the task is support code, and what part is authored behaviour?
2. Where should the authoritative policy live?
3. Which Vontology concepts, prompts, workflows, or memory objects should exist?
4. What model portfolio or router considerations matter?
5. What evaluation path will test the real behaviour?
6. What learning loop, if any, should make this improve over time?
7. How does the design remain legible if the underlying model improves?
8. What baseline are we comparing against?
9. What temporary fallback exists, and how will it be removed?

## 15. Closure check

Before closing a substantial behaviour task, verify all of the following:

1. The authoritative decision policy is in workflow/prompt/KB/Vontology artefacts or materialised assertions.
2. Python support code remains generic support rather than hidden business policy.
3. Prompt and model choices are visible in evaluation notes and telemetry.
4. Memory or retrieval changes were tested on the intended long-horizon or global-question path, not only a toy path.
5. Any heuristic fallback is explicitly temporary and linked to a removal or replacement task.
6. The acceptance evidence covers the nearest real production path.
7. The task notes record any architecture or literature reinterpretation.

## 16. Bottom line

For Von, modern agentic AI does not mainly mean adding more autonomous Python.

It means designing a system in which:

- enduring knowledge is explicit
- workflows and prompts are real implementation surfaces
- representation choices are deliberate
- model portfolios are explicit
- learning loops are designed in, not bolted on
- evaluation is realistic and sceptical
- and new research can be adopted by improving policy surfaces rather than by accreting code heuristics

## 17. Selected motivating literature and SAIL-adjacent themes

Useful background examples include work on:

- interleaving reasoning and acting
- reflective or guideline-based learning from experience
- prompt-program optimisation
- graph-based retrieval for global corpus questions
- model routing and cost-aware portfolios
- RL-style training for agent control
- rigorous agent benchmarking
- long-horizon agent memory evaluation
- SAIL work on abstract reasoning limits, logical robustness, counterfactual reasoning, and iterative explanation improvement
