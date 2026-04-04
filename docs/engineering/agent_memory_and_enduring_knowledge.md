# Agent Memory and Enduring Knowledge

## 1. When to read this

Read this document before planning or implementing work that touches:

- retrieval or RAG
- long-horizon context
- memory systems
- KB growth or consolidation
- profile, preference, or persistent state handling
- graph or causal retrieval structures

## 2. Core doctrine

Von should distinguish transient context from enduring knowledge.

Not everything worth remembering belongs in the same store, format, or durability class. Treat memory design as an architectural choice, not as a retrieval afterthought.

## 3. Memory classes

At minimum, distinguish:

- **semantic memory**: durable facts and relations
- **episodic memory**: traces of interactions and workflows
- **procedural memory**: reusable workflows, prompts, playbooks, validators
- **policy memory**: learned routing rules, guidelines, preferences, context-conditional advice

## 4. What belongs in Vontology or KB

Promote information into durable represented form when it is:

- evidence-backed
- reusable
- worth revising later
- linked to identifiable entities, concepts, or policies
- important across sessions or users

Do not leave all durable knowledge trapped in opaque model state.

## 5. Retrieval is policy

Retrieval policy includes:

- indexing choices
- chunking or graph construction
- salience estimation
- local vs global retrieval
- context budget allocation
- conflict handling
- uncertainty handling

Treat these as policy surfaces that require evaluation.

## 6. Consolidation rules

When consolidating episodic traces into durable knowledge, define:

- evidence threshold
- provenance retention
- confidence or uncertainty representation
- revision or retraction pathway
- conflict resolution behaviour

## 7. Structure choice

Prefer richer structure when the task depends on:

- global corpus synthesis
- connected multi-entity reasoning
- causal or temporal dependencies
- revision of earlier beliefs
- long-horizon tool use
- user preferences or task-state grounding across interruptions

But compare against simpler baselines. More structure is not automatically better.

## 8. Long-horizon evaluation

Evaluate memory features on tasks that actually require:

- incremental accumulation
- buried evidence retrieval
- task-state reconstruction
- preference grounding
- global or cross-document synthesis

A memory feature that only looks good on explicit fact-recall questions is not enough.

## 9. Acceptance checklist

Before closing memory- or retrieval-related work, verify:

- the memory type was named clearly
- durable knowledge promotion rules are explicit
- provenance and uncertainty are preserved where relevant
- the system was compared against a simple baseline
- the evaluation task required the intended memory capability
