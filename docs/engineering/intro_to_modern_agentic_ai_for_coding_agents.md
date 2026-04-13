# Intro to Modern Agentic AI for Coding Agents

**Status**: Draft design/background guidance  
**Date**: 2026-04-04

## 1. Purpose

This document is a compact design/background primer for coding agents working on Von.

It exists to prevent a recurring failure mode: treating modern agentic-AI behaviour as if it were mainly ordinary application logic that should be hand-coded in Python, with prompts, workflows, knowledge bases, and explicit representations treated as secondary decoration.

For Von, that is often backwards.

A particularly important variant of this failure is a lexical NLP heuristic masquerading as a "thin inspectable layer": bounded, deterministic, and explainable code that still keeps the real decision policy in Python rather than in the intended workflow/KB/prompt authority surface.

In many tasks, especially behaviour-heavy ones, the most important design question is not "what Python should I write?" but:

- where the authoritative behaviour should live;
- what should be represented explicitly in Vontology or workflow artefacts;
- what should be handled by prompted model judgement;
- what should be handled by explicit logical/typed structure;
- and what reusable runtime support Python should supply.

Related guidance:

- [AGENTS.md](../../AGENTS.md)
- [docs/engineering/von_workflow_language_manual.md](./von_workflow_language_manual.md)
- [docs/engineering/minimal_imposition_design_principle.md](./minimal_imposition_design_principle.md)
- [docs/engineering/security_considerations.md](./security_considerations.md)

## 2. When to Read It

Per `AGENTS.md`, this document is required review before finalising the plan for any substantial implementation task.

It is especially important when the task touches:

- ranking, recommendation, matching, routing, classification, planning, or evaluation;
- prompts, tool use, workflow behaviour, or agent policies;
- retrieval, memory, knowledge bases, or context construction;
- ontology or representation design;
- model-selection, prompt-evolution, or behaviour that may change across model generations.

## 3. Core Orientation

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

For multi-stage turns, the accumulated turn context presented to each LLM phase
is one of those behaviour-shaping surfaces. If selector, planner, tool-use, and
response stages receive materially different contexts because code silently
rebuilds or thins them, Python has effectively become a hidden policy layer.

### 3.2 Behaviour authority matters

In Von, behaviour often ought to be authored in:

- Vontology concepts and text relations;
- VWL workflow definitions and metadata;
- prompt concepts and related policy text;
- explicit KB artefacts with provenance;
- typed relations and other inspectable structures.

Treat those surfaces as first-class implementation surfaces for durable system behaviour, not as secondary configuration or commentary about logic that really lives in Python.

Python should usually provide:

- reusable execution surfaces;
- validation and constraint checking;
- retrieval and rendering helpers;
- telemetry, persistence, and safety checks;
- generic interfaces between models, workflows, and KB artefacts.

If Python starts containing the actual task policy, Von usually becomes harder to inspect, evolve, and improve.

A good default question is therefore not only "what Python should I write?" but also "can this change be cleanly authored in Vontology, workflow, prompt, or KB artefacts instead?"

Another good default question is: "are different LLM stages seeing different
effective contexts because that is part of the designed policy, or because code
has drifted into phase-specific context shaping?"

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
- deterministic validation and type checking;
- safe tool invocation layers;
- provenance capture and telemetry;
- durable persistence and replay support;
- generic retrieval, normalisation, and transformation helpers.

Python is usually the wrong place for:

- domain-specific recommendation policy;
- long-lived ranking logic encoded as weights and thresholds;
- stage-specific semantic context pruning that decides what the model may know;
- task-specific routing rules that VWL can express;
- code-side prompt bodies for Vontology-governed features;
- durable type, predicate, or workflow policy edits that could be represented directly in Vontology;
- lists of ontology terms or relation IDs that should be resolved from Vontology.

### 4.2 Use workflows and prompts for behaviour policy

When behaviour depends on:

- choosing among actions;
- comparing candidate interpretations;
- weighing heterogeneous evidence;
- deciding what explanation to present;
- deciding when to continue, pause, escalate, or ask;

then the authoritative policy often belongs in:

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

If a design can move a stable concept, relation, profile, or policy out of ad-hoc code and into explicit represented form, that is usually progress.

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
- hand-written "mini NLP" whose real purpose is to avoid using the model, workflow, or KB properly.

These are not always forbidden, but they are design smells and require explicit justification.

## 8. What to Do Instead

When a task appears to invite heuristic policy code, ask:

1. What is the real behaviour or policy being authored?
2. Should that authority live in workflow, prompt, KB, or ontology artefacts?
3. Is the missing piece actually a reusable runtime primitive rather than task-specific logic?
4. Can model judgement be used with explicit provenance/evaluation instead of lexical hacks?
5. What explicit representation would make the behaviour inspectable and revisable?
6. What telemetry and tests would show whether the approach works across model changes?

Often the right move is one of:

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

## 10. Planning Checklist for Coding Agents

Before implementing substantial behaviour changes, answer these briefly in task notes or Jira comments:

1. What part of the task is generic support code, and what part is authored behaviour/policy?
2. Where should authoritative behaviour live?
3. Which concepts, prompts, workflows, or text relations should exist in Vontology?
4. Is there a missing reusable runtime primitive or validator?
5. Would explicit representation or prompted judgement outperform hand-coded heuristics here?
6. How will the design remain legible if the underlying model improves or changes?
7. What evidence path will test the real call path rather than only unit-shaped assumptions?

If those questions are not answered, implementation is probably starting too early.

## 10A. Closure Check for Workflow-First Behaviour

Before closing a workflow-first or KB-authoritative task, verify all of the following:

1. The authoritative decision policy is in Vontology/workflow/prompt/KB artefacts or materialised KB assertions.
2. Python support code is generic support surface rather than hidden task policy.
3. Any heuristic fallback is explicitly temporary, non-authoritative, and linked to a removal or replacement task.
4. The production path does not silently depend on lexical heuristics that merely look tidy or interpretable in code review.

## 11. Bottom Line

For Von, "modern agentic AI" does not mainly mean adding more autonomous Python.

It means designing systems in which:

- behaviour authority is explicit;
- prompts, workflows, and KB artefacts are treated as real implementation surfaces;
- representation choices are deliberate;
- Python supplies reusable support rather than frozen domain policy;
- and model capability improvements can be absorbed by the surrounding architecture rather than fought with brittle heuristics.
