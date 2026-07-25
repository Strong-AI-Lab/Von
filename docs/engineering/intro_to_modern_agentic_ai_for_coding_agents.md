# Intro to Modern Agentic AI for Coding Agents

- **Kind:** Principle and engineering-background guide
- **Lifecycle:** Active
- **Authority:** Required for substantial implementation planning because it is
  selected by [`AGENTS.md`](../../AGENTS.md); subordinate to that constitution
  and current explicit user direction
- **Created:** 2026-04-04
- **Last reviewed:** 2026-07-25
- **Freshness boundary:** Design guidance, not a report of current
  implementation state; verify factual claims against live evidence

## 1. Purpose

This document is a compact design/background primer for coding agents working on Von.

It exists to prevent two reciprocal failure modes: hiding durable adaptable
behaviour in incidental Python, and turning prompts, workflows, knowledge
bases, representations, validators, and telemetry into compulsory architecture
that displaces the user job.

A particularly important variant of this failure is a lexical NLP heuristic masquerading as a "thin inspectable layer": bounded, deterministic, and explainable code that still keeps the real decision policy in Python rather than in the intended workflow/KB/prompt authority surface.

In many tasks, especially behaviour-heavy ones, the important design questions
are:

- what is the simplest adequate end-to-end path;
- whether any behaviour needs durable independent authority at all;
- what, if anything, should be represented explicitly in Vontology or workflow
  artefacts;
- what should be handled by prompted model judgement;
- what should be handled by explicit logical/typed structure;
- and what reusable runtime support Python should supply.

Related guidance:

- [AGENTS.md](../../AGENTS.md)
- [docs/engineering/von_workflow_language_manual.md](./von_workflow_language_manual.md)
- [docs/engineering/minimal_imposition_design_principle.md](./minimal_imposition_design_principle.md)
- [docs/engineering/security_considerations.md](./security_considerations.md)

## 2. When to Read It

Per `AGENTS.md`, consult the relevant sections of this document before
finalising substantial agent-behaviour or architecture-sensitive work. It is a
reference, not a cover-to-cover prerequisite for unrelated substantial work.

It is especially important when the task touches:

- ranking, recommendation, matching, routing, classification, planning, or evaluation;
- prompts, tool use, workflow behaviour, or agent policies;
- retrieval, memory, knowledge bases, or context construction;
- ontology or representation design;
- model-selection, prompt-evolution, or behaviour that may change across model generations.

## 3. Core Orientation

### 3.0 Begin with the user capability and simplest adequate path

Before choosing an agent architecture, name the user job, foreground work
product, evidence or world state that would count as success, latency and human
burden, and the best fair simpler baseline. A direct tool call, deterministic
function, retrieval plus one model call, or small workflow may be sufficient.

Represented authority is valuable when behaviour or knowledge needs durable
identity, independent authoring, provenance, revision, governance, reuse, or
evaluation. It is not a requirement to add Vontology/VWL machinery to every
user-visible behaviour. Complexity must earn its place through capability,
safety, or measured operational advantage.

Von is predominantly helping with ordinary administrative and scientific work,
not controlling an aircraft. The default candidate should therefore be a
capable collaborator acting within standing delegation: interpret reasonably,
take a bounded action, inspect the result, and repair mistakes. Do not import
safety-critical control architecture merely because an LLM can be wrong.
Constrain the concrete residual consequence, and count needless refusal,
clarification, confirmation, latency, and lost solution strategies as costs.

### 3.1 Modern LLM systems are not just "models plus wrappers"

For practical agent design, system behaviour is usually co-determined by:

- model capability and post-training;
- prompts and surrounding context;
- retrieval and memory construction;
- tool affordances and schemas;
- workflow structure and control policy;
- explicit knowledge representation and provenance;
- validation, telemetry, and feedback loops.

If a task changes system behaviour, it may be changing one or more of those authored surfaces, not merely "implementation".

When more than one model stage is justified, the accumulated context presented
to each call is one of those behaviour-shaping surfaces. If the calls receive
materially different contexts because code silently rebuilds or thins them,
Python may have become a hidden policy layer. This does not justify creating
multiple stages for a turn that a single model or direct tool path can handle.

### 3.2 Choose behavioural authority deliberately

When the governance properties justify an extra layer, behaviour may be
authored in:

- Vontology concepts and text relations;
- VWL workflow definitions and metadata;
- prompt concepts and related policy text;
- explicit KB artefacts with provenance;
- typed relations and other inspectable structures.

Treat a selected surface as first-class for the behaviour it actually governs.
Do not infer that all behaviour needs durable representation or that a direct
code/tool/model path is merely plumbing.

Python may provide:

- reusable execution surfaces;
- exact interface validation where the consumer genuinely requires it;
- bounded tool capabilities, observation, read-back, and recovery support;
- retrieval and rendering helpers;
- telemetry, persistence, and proportionate risk controls;
- generic interfaces between models, workflows, and KB artefacts.

If Python starts containing adaptable task policy that should be independently
authored, governed, or learned, Von usually becomes harder to inspect, evolve,
and improve. Conversely, moving exact algorithms or interface semantics out of
code can make the system slower and less reliable. This does not make broad
categories such as writes, schemas, transactions, or destructive actions
automatically hard; the concrete capability and residual risk still decide the
assurance needed.

A good default question is therefore: "which smallest surface—direct code or
tool, model judgement, prompt, workflow, KB artefact, or composition—best serves
the user job, and does any added authority layer earn its cost?"

If multiple model calls are actually needed, also ask whether each receives a
minimum-sufficient projection from the shared situation with enough lineage to
explain material differences.

### 3.3 A strong model is often better at semantics than brittle lexical code

Modern models are often substantially better than hand-written lexical heuristics at:

- semantic matching;
- explanation generation;
- ranking with nuanced trade-offs;
- synthesising evidence across multiple sources;
- interpreting underspecified or paraphrased language.

That does not mean "always let the model decide". It means you should not reflexively replace semantic judgement with:

- token-overlap scoring;
- stopword lists;
- hand-tuned weights;
- arbitrary thresholds;
- special-case string hacks;
- brittle pseudo-NLP built in Python.

Those may be acceptable only as explicit, temporary baselines or bounded fallbacks with clear justification.

This means a key acceptance question is not merely "is the code inspectable?" but:

- did we actually move the decision policy into the intended authority surface; or
- did we just wrap heuristic Python in a nicer interface?

## 4. Division of Labour: What Goes Where

### 4.1 Use Python for general capability surfaces

Python is usually the right place for:

- generic workflow/runtime primitives;
- structural validation and type checking where an interface requires them;
- bounded tool invocation, receipts, read-back, and recovery mechanisms;
- provenance capture and telemetry;
- durable persistence and replay support;
- generic retrieval, normalisation, and transformation helpers.

Python is usually the wrong place for adaptable, independently governed policy
such as:

- domain-specific recommendation policy;
- long-lived ranking logic encoded as weights and thresholds;
- stage-specific semantic context pruning that decides what the model may know;
- task-specific routing rules that VWL can express;
- code-side prompt bodies for Vontology-governed features;
- durable type, predicate, or workflow policy edits that could be represented directly in Vontology;
- lists of ontology terms or relation IDs that should be resolved from Vontology.

### 4.2 Use workflows and prompts when behaviour needs represented policy

When behaviour depends on:

- choosing among actions;
- comparing candidate interpretations;
- weighing heterogeneous evidence;
- deciding what explanation to present;
- deciding when to continue, pause, escalate, or ask;

then the authoritative policy often belongs in the following surfaces when it
needs independent identity, revision, reuse, governance, or evaluation:

- workflow state/transition structure;
- prompt concepts and prompt text relations;
- explicit policy metadata stored in KB/Vontology artefacts.

### 4.3 Use Vontology/KB artefacts for explicit, inspectable knowledge

Knowledge bases are not merely caches for retrieval.

In Von they are part of the system's explicit cognitive architecture:

- they preserve provenance and inspectability;
- they support durable state across turns and workflows;
- they expose knowledge and policy for revision;
- they provide a place for structured entities, relations, and constraints that should not remain hidden in latent model state.

If a design can move a stable concept, relation, profile, or adaptable policy
out of ad-hoc code and into explicit represented form without adding more cost
than value, that is usually progress.

For Von, this often means that changing a Vontology type, predicate, text relation, or VWL artefact is not "avoiding implementation work"; it is doing the implementation work at the correct authority layer.

## 5. Prompt Evolution, Model Improvement, and Robustness

### 5.1 Design for changing model capability

Models improve. Prompt behaviour shifts. Post-training changes can alter which tasks are easy, hard, brittle, or cheap.

So:

- avoid baking current model quirks into code;
- avoid assuming today's lexical workaround is a durable requirement;
- prefer prompt, workflow, evaluation, and validation surfaces that can evolve without rewriting policy code;
- make behaviour differences observable via telemetry and tests.

### 5.2 Prompts are not comments

Prompts are often operational policy.

Treat them as:

- authored artefacts;
- versionable design surfaces;
- objects that need provenance, inspection, and revision;
- part of the system's behaviour contract.

Do not hide prompt policy in Python strings when `AGENTS.md` says a Vontology-governed feature should keep prompt authority in Vontology text relations.

### 5.3 RL, post-training, and eval improvements do not remove the need for design

Stronger post-trained models can reduce the need for brittle heuristics, but they do not eliminate the need for:

- explicit authority boundaries;
- representation choices;
- workflow control;
- evaluation and telemetry;
- safe fallbacks and failure visibility.

Think "better substrate for good architecture", not "architecture no longer matters".

## 6. Knowledge Representation and Logic

### 6.1 Representation is a design choice, not a prestige signal

Use the weakest adequate representation that preserves the needed structure, provenance, and inferential leverage.

Roughly:

- plain text is often enough for raw user-authored material;
- typed entities and relations are useful when stable reference and provenance matter;
- workflow/state structure is useful when behaviour needs explicit control flow;
- logical or quasi-logical forms are useful when compositional inference, role structure, modality, temporal structure, or event semantics materially matter.

Do not over-formalise merely because a representation sounds sophisticated.

### 6.2 FoL, HoL, modal logics, Davidsonian forms, and episodic logic

These traditions can be highly relevant, but they are tools, not badges.

They become useful when they solve a concrete problem such as:

- representing event structure and participants;
- separating assertion from modality, uncertainty, obligation, or possibility;
- supporting compositional inference over explicit variables and roles;
- preserving episodic or temporal structure that free text would blur;
- grounding explanations or reasoning traces in inspectable symbolic form.

They are not a licence to:

- invent an unnecessary formal layer;
- bypass existing Vontology patterns without justification;
- replace practical workflow/KB improvements with abstract notation.

If you think a richer logical representation is needed, say exactly:

- what inferential or representational failure the current system has;
- why text, typed relations, or existing workflow metadata are insufficient;
- what reusable primitive Von lacks;
- how the proposed representation improves behaviour or inspectability.

## 7. Common Design Smells

Pause and reconsider when you are about to write:

- a custom tokeniser for recommendation, matching, ranking, or routing policy;
- a stopword list to approximate semantics;
- fixed additive score weights or threshold tables for durable decision policy;
- a bounded, deterministic, "inspectable" heuristic layer that still acts as the primary production decision policy;
- a growing tree of case-specific if/else rules to mimic workflow policy;
- repo-side JSON/YAML/DSL files that become the real production authority;
- code-side prompt defaults for a Vontology-governed feature;
- hard-coded ontology IDs or name lists that should be resolved from Vontology;
- hand-written "mini NLP" whose real purpose is to avoid using the model, workflow, or KB properly;
- a universal safety class or approval table whose `write`, `send`, or `delete`
  label substitutes for examining delegation, consequence, and recovery.

These are not always forbidden, but they are design smells and require explicit justification.

## 8. What to Do Instead

When a task appears to invite heuristic policy code, ask:

1. What is the real behaviour or policy being authored?
2. Does it need durable independent authority, and if so should that live in
   code, workflow, prompt, KB, or ontology artefacts?
3. Is the missing piece actually a reusable runtime primitive rather than task-specific logic?
4. Can model judgement be used with explicit provenance/evaluation instead of lexical hacks?
5. What explicit representation would make the behaviour inspectable and revisable?
6. What telemetry and tests would show whether the approach works across model changes?
7. Would a proposed gate or compulsory stage outperform a permissive baseline
   on the complete user job?

Often the right move is one of:

- keep or simplify a direct tool/function/model path;
- add or update a workflow primitive;
- author or revise a prompt in Vontology;
- represent missing concepts/relations in Vontology;
- improve retrieval/context construction;
- add a validator or evaluator around model output;
- create a temporary, well-labelled baseline only while a better authority surface is being built.

## 9. Temporary Baselines and Exceptions

Sometimes a heuristic baseline is still useful.

If so, treat it as an explicit exception, not as the default design.

The exception should say:

- why a KB/workflow/prompt/representation-first path is not yet available;
- what reusable primitive is missing;
- what Jira issue tracks the gap;
- what telemetry/evaluation will reveal the baseline's limitations;
- what the removal or replacement plan is.

Temporary baselines should be:

- narrow in scope;
- easy to delete;
- clearly labelled in code and Jira;
- non-authoritative by intent.

## 10. Planning prompts for coding agents

For substantial behaviour changes, use only the prompts that materially help:

1. What user job and work product are being improved?
2. What is the simplest adequate path and fair baseline?
3. Does any proposed compulsory restriction meet the evidential burden in
   `AGENTS.md`?
4. Which knowledge or behaviour needs durable represented identity, revision,
   governance, or reuse?
5. Is there a missing reusable runtime primitive or validator, or would adding
   one over-generalise a local need?
6. How will the design remain legible if the model improves or changes?
7. What validation tier and evidence path match the actual claim, including
   latency, cost, and human burden where material?

## 10A. When represented authority was deliberately selected

Verify only the material claims:

1. The selected artefact really governs the behaviour it claims to govern.
2. No hidden case-specific path contradicts it.
3. Its governance or reuse benefit justifies the extra layer against a simpler
   baseline.

This check does not require represented authority for a direct path that does
not need it.

## 11. Bottom Line

For Von, "modern agentic AI" does not mainly mean adding more autonomous Python.

It means designing systems in which:

- behaviour authority is explicit;
- prompts, workflows, and KB artefacts are treated as real implementation surfaces;
- representation choices are deliberate;
- Python supplies reusable support rather than frozen domain policy;
- and model capability improvements can be absorbed by the surrounding architecture rather than fought with brittle heuristics.
