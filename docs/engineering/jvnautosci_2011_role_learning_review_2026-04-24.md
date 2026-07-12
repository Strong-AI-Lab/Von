# JVNAUTOSCI-2011 Role-Learning Design Review - 24 April 2026

> **Document status: Long-horizon design with a dated implementation snapshot.**
> The role-learning framing may remain useful. Claims about the current
> substrate, gaps, source anchors, or task ordering describe the 24 April 2026
> evidence base and require live revalidation.

## Purpose

This note records the design review that led to `JVNAUTOSCI-2011`, the epic
for minimal-imposition role learning.

The review question was whether Von's current design documents are sufficient
for a system that can take on otherwise hard-to-fill roles in an organisation
or team, learn how to perform them increasingly well, and improve by building
durable knowledge about workflows, interactions, people, organisations,
processes, work products, notes, artefacts, models, prompts, and skills.

The short answer is:

- the architecture direction is right;
- the substrate work is increasingly concrete;
- but the role-learning object was not first-class enough;
- and the near-term SAIL research-operations proving ground needed to be named
  more explicitly.

## Review Scope

Primary local documents reviewed:

- `docs/engineering/Von_for_AgenticAI.md`
- `docs/engineering/minimal_imposition_design_principle.md`
- `docs/engineering/minimal_imposition_benchmark_model.md`
- `docs/engineering/agent_memory_and_enduring_knowledge.md`
- `docs/engineering/prompt_programs_and_model_routing_playbook.md`
- `docs/engineering/agent_evaluation_and_research_uptake.md`
- `docs/engineering/von_workflow_language_manual.md`
- `docs/engineering/jvnautosci_1962_enduring_memory_architecture_2026-04-23.md`
- `docs/engineering/jvnautosci_1963_self_improvement_worlds_architecture_2026-04-23.md`
- `docs/engineering/jvnautosci_1964_evaluator_architecture_2026-04-23.md`
- `docs/engineering/agentic_architecture_status_2026-04-23.md`
- `docs/engineering/context_bundle_workspace_contract.md`

External research and context checked included:

- Strong AI Lab overview:
  <https://www.ai.ac.nz/sail/>
- Cyc platform and publications:
  <https://cyc.com/platform/> and <https://cyc.com/publications/>
- Gendron, Bao, Witbrock, and Dobbie, *Large Language Models Are Not Strong
  Abstract Reasoners*:
  <https://www.ijcai.org/proceedings/2024/693>
- Matuszek, Witbrock, Cabral, and DeOliveira, *Searching for Common Sense:
  Populating Cyc from the Web*:
  <https://aaai.org/Papers/AAAI/2005/AAAI05-227.pdf>
- Recent agent-memory, self-improvement, and workplace-task evaluation work,
  including OccuBench, GDPval, tau-bench, TheAgentCompany, SkillWeaver, and
  long-horizon memory surveys or benchmarks.

The external scan did not overturn Von's direction. It reinforced three
points: explicit knowledge matters, memory and self-improvement need evaluable
promotion loops, and professional-role capability must be evaluated on
realistic work products and processes rather than only on chat answers.

## Main Finding

Von's documents are strong as a **neuro-symbolic agent substrate** design, but
weaker as a **role-learning programme** design.

They already say the right things about:

- Vontology authority;
- workflows and prompt programmes;
- memory strata;
- evaluator axes;
- candidate worlds and rollback-safe self-improvement;
- model portfolios;
- provenance, telemetry, and namespace discipline;
- avoiding Python-owned semantic policy.

What was missing was a clear represented object for the thing Von should learn
to do in the near term: perform useful roles for real teams with low burden.

## Near-Term Focus

The immediate target should be process improvement and operational
effectiveness for AI research in the Strong AI Lab.

That focus should not narrow the AGI goal. It should act as the first real
environment in which the AGI-shaped substrate becomes operational:

1. SAIL AI research operations.
2. Science, discovery, and R&D more broadly.
3. Auxiliary organisational roles.
4. Roles in general.

This staged path gives the programme a practical spine while preserving the
larger aim: a deployable system that can learn, represent, evaluate, and
improve role performance across settings.

## Shortfalls

### 1. Role Performance Was Not First-Class

The design needs a canonical representation for:

- role;
- responsibilities and obligations;
- permissions and authority boundaries;
- workflows and process states;
- handoffs and interaction patterns;
- work products and quality standards;
- notes, meetings, artefacts, and decisions;
- evidence receipts and provenance;
- skills, prompts, tools, model choices, and workflow variants;
- success metrics and evaluator outputs.

Without this, Von risks becoming a set of useful capabilities rather than a
system that can learn an under-filled role and improve at it.

### 2. SAIL Research Operations Were Too Implicit

Earlier docs gesture at research-team support but do not make the near-term
operational targets concrete enough.

Candidate SAIL pilot roles should be treated as examples, not hard-coded
policy. Useful initial examples include:

- research-project continuity aide;
- literature and paper-representation steward;
- evaluation and replay coordinator;
- meeting, action-note, and follow-up maintainer;
- artefact and knowledge-base curator;
- new-lab-member onboarding assistant;
- grant, reporting, or administrative support assistant.

Each pilot should identify real workflows, work products, interaction surfaces,
and evidence of burden reduction.

### 3. Minimal-Imposition Evaluation Needs Role Metrics

The current minimal-imposition benchmark model is useful but too generic for
role performance.

Role-learning evaluation should include:

- avoided repeated explanation;
- reduced coordination burden;
- fewer unnecessary clarification or permission loops;
- work-product acceptance or revision burden;
- workflow continuity across meetings and interruptions;
- recovery cost after failure;
- process disruption;
- appropriate escalation;
- team adoption friction.

These should be layered onto the existing evaluator and benchmark stack rather
than implemented as a separate scoring system.

### 4. Memory, Evaluation, and Self-Improvement Need a Role Pipeline

The current memory, evaluator, and candidate-world tasks are good substrate
work. Role learning should consume them explicitly.

The intended loop should be:

1. observe role-relevant episodes, artefacts, interactions, and work products;
2. preserve provenance, namespace, and visibility;
3. query them through the enduring-memory manifest;
4. promote recurring facts, procedures, and guidance into semantic,
   procedural, and policy memory;
5. evaluate role performance and work-product quality through multi-axis
   evaluator contracts;
6. create candidate workflow, prompt, skill, routing, or memory improvements;
7. test candidate improvements in bounded worlds;
8. publish only after evaluator-backed promotion gates.

### 5. Multi-Agent Coordination Needs Organisational Semantics

The multi-agent coordination note currently focuses on delegation,
capability negotiation, and shared episodic context. Role learning also needs:

- human-agent handoff patterns;
- organisational authority and accountability;
- shared team state;
- arbitration when agents or people disagree;
- role-specific escalation policy;
- traceable ownership of delegated subtasks and artefacts.

This should be represented in Vontology and workflows, not hidden in
orchestration code.

### 6. Role Policy Must Not Become Python Heuristics

The same anti-patterns that apply to routing and recommendation apply to role
learning.

Design smells include:

- role-name conditionals in Python;
- SAIL-specific branches in orchestration code;
- persona-only prompts with no represented responsibilities, workflows, or
  work-product contracts;
- work-product grading hidden in string matching;
- opaque "agent memory" that cannot explain evidence, scope, or revision.

Role learning should be represented and evaluated, not simulated through a
larger prompt.

## Recommended Document Changes

The design documents should be updated in three layers.

1. `Von_for_AgenticAI.md`
   Add role learning as the near-term proving ground, with SAIL research
   operations first and broader science/R&D, auxiliary roles, and general roles
   as later stages.

2. `minimal_imposition_benchmark_model.md`
   Add role-performance/process-improvement dimensions once the evaluator
   substrate can support them.

3. Memory, evaluator, and self-improvement notes
   Cross-link role learning to `JVNAUTOSCI-1995` through `JVNAUTOSCI-2005` so
   role learning consumes the shared substrate rather than creating a parallel
   stack.

The first update has been started under `JVNAUTOSCI-2012` by updating
`docs/engineering/Von_for_AgenticAI.md` and adding this review note.

## Jira Outcome

Created:

- `JVNAUTOSCI-2011`: Minimal-imposition role learning for SAIL research
  operations and generalisable work roles.
- `JVNAUTOSCI-2012`: Update Von design framing for minimal-imposition role
  learning and SAIL research operations.

`JVNAUTOSCI-2011` is linked to `JVNAUTOSCI-1960`. The relationship is:

- `JVNAUTOSCI-1960` remains the broad minimal-imposition agentic AI substrate
  programme.
- `JVNAUTOSCI-2011` is the role-learning and SAIL research-operations
  application spine that consumes that substrate.

## Acceptance Direction

The next design and implementation work should be accepted only when it shows:

- real SAIL research-operations roles and work products, not only abstract
  personas;
- represented role artefacts in Vontology/workflow/prompt/KB surfaces;
- minimal-imposition evidence that the system reduced burden or improved
  operational effectiveness;
- evaluator-backed learning from role episodes;
- candidate-world or equivalent gated promotion before role-policy changes
  become canonical;
- no durable role policy hidden in Python.
