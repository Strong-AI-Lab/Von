# Automated Policy Learning Design

**Status**: Proposed design guidance
**Date**: 2026-04-04
**Updated**: 2026-04-25

## 1. Purpose

This document outlines the framework for transitioning Von from manual, hard-coded prompt tweaks to automated policy learning. By leveraging the rich telemetry provided by Turn Execution Records (TERs), Von will implement automated learning loops that induce guidelines, update retrieval strategies, and adjust routing, achieving true policy learning.

The immediate motivating case is `JVNAUTOSCI-2090`, linked to the
`JVNAUTOSCI-1894` replay programme. The same user request, `Tell me about
myself`, succeeded with `gpt-5.4-mini` but failed with `gemma4:26b`. The useful
lesson is not "ban Gemma" or "hard-code GPT for routing". The lesson is that Von
needs introspectable model-use learning from real conversation rollouts:
workflow stage, model, prompt variant, context lineage, tool adequacy, final
answer, and post-turn critique should all become evidence for future model
selection.

## 2. Core Concepts

### 2.1 Turn Execution Records (TERs) as the Learning Substrate

All automated learning loops will be grounded in TERs. A TER captures the complete context, action, and outcome of an agent's execution turn, including:
- The initial prompt and context.
- The tools invoked and their arguments.
- The intermediate reasoning steps (e.g., ephemeral theories).
- The final output and any associated reward or validation signals.

### 2.2 The Learning Loop Architecture

The automated policy learning framework consists of three primary phases:

1. **Collection & Telemetry**: Gathering high-fidelity TERs and explicit validation signals (user feedback, test results, execution success).
2. **Analysis & Induction**: Parsing TERs offline or asynchronously to identify patterns of failure, successful workarounds, and capability gaps.
3. **Policy Update**: Automatically generating and applying updates to authoritative policy surfaces (guidelines, prompts, routers).

## 3. Mechanisms for Policy Update

### 3.1 Guideline Induction

Instead of humans manually writing new rules in `AGENTS.md` for every edge case, the system will induce guidelines from experience.

- **Pattern Recognition**: An offline evaluator agent analyzes TERs to find recurring failure modes.
- **Guideline Proposal**: The evaluator proposes a new, concise guideline to prevent the failure.
- **Vontology Integration**: Approved guidelines are materialised into Vontology as contextual policy objects, retrieved dynamically when relevant context is encountered.

### 3.2 Retrieval Strategy Updates

Retrieval policies will be dynamically adjusted based on usage patterns.

- **Relevance Feedback**: Track which retrieved memory fragments were actually utilised in the agent's final reasoning.
- **Weight Adjustment**: Automatically down-weight or archive noise, and boost the retrieval scores of high-utility structures (e.g., specific graph traversals or Vontology predicates).

### 3.3 Dynamic Routing Adjustment

Model and agent routing should improve through empirical performance data.

- **Cost-Performance Tracking**: Continuously evaluate the cost, latency, and success rate of different models/agents for specific task categories.
- **Router Re-weighting**: Update routing metadata to favour models that demonstrate high reliability for a given task, reserving frontier models for novel or high-complexity tasks.

## 4. Actor-Critic Model-Use Learning

Von should treat model choice as a learnable, represented policy over workflow
stages, not as a fixed application setting. The intended loop is actor-critic in
the ordinary reinforcement-learning sense, but grounded in Von's existing
workflow, Vontology, prompt, and telemetry architecture.

The **actor** is the runtime policy that chooses a model arm, prompt variant,
fallback chain, and escalation threshold for a workflow stage. It should prefer
the cheapest active model that is certified as good enough for the specific
workflow, stage, prompt profile, data sensitivity, and user/org context. For
example, the actor may choose a local Ollama model for low-risk bounded
classification, use GPT-5.5 for difficult selector or critic stages, and
escalate when structured-output validation or postcondition checks fail.

The **critic** is a stronger reviewer workflow that evaluates completed turn
rollouts. It reads the TER, LLM IO telemetry, model and prompt identifiers,
context lineage, tool calls, completion gates, final answer, user feedback, and
postcondition checks. It should not produce vacuous "looks fine" commentary. Its
job is to explain what happened, assign structured outcome signals, and propose
policy updates only when the evidence is strong enough.

The **rollout** is the full conversation turn execution, not just the final
assistant text. A rollout record for model-use learning should include:

- user request, namespace, authenticated context, and selected workflow;
- candidate workflows and exclusion reasons;
- stage-level prompt concepts, prompt variants, and effective context lineage;
- model provider, model id, model settings, latency, token counts, and cost;
- raw and parsed LLM outputs, including structured-output validity;
- tool plan, tool calls, tool results, and missing-tool diagnostics;
- completion gates, validator results, fallback/escalation events, and answer;
- user feedback or downstream acceptance evidence where available.

The Gemma/GPT case demonstrates why this matters. The GPT-5.4-mini rollout chose
`#V#entity_information_retrieval_workflow`, used the expected Vontology tools,
and produced a grounded represented answer. The Gemma rollout selected
`#V#chat_assistant_workflow`, did no tool work, and ended with an operational
failure answer. The LLM IO showed several learnable failure signals:

- selector prompt/context ambiguity: the model appeared to reason about
  `Select workflow` instead of the actual user request;
- structured-output weakness: fenced JSON was treated as text and the
  structured selector evidence was not fully captured;
- empty-output weakness: a later direct response returned an empty string with
  success status;
- tool-adequacy failure: the completion report named required retrieval tools
  that had not run.

Those signals should train and certify model-use policy. They should not become
English-specific routing rules or Python-side semantic patches.

## 5. Reward and Evaluation Signals

The critic should emit multiple inspectable signals rather than a single opaque
reward. Useful signals include:

- **selector correctness**: whether the chosen workflow matched the request and
  candidate evidence;
- **schema adherence**: whether the model returned valid, parseable structured
  output without hidden repair;
- **context use**: whether the model attended to the actual user request and
  required stage context;
- **tool adequacy**: whether required bounded tool families were called and
  whether their results reached the answer;
- **groundedness**: whether claims are supported by represented facts, tool
  output, or explicit uncertainty;
- **answer usefulness**: whether the user-visible answer satisfies the prompt
  rather than exposing execution bookkeeping;
- **failure transparency**: whether failures are visible in telemetry and
  intelligible to later diagnosis;
- **cost, latency, locality, and privacy fit**: whether a cheaper or local model
  was sufficient without sacrificing task quality;
- **recovery quality**: whether the actor escalated or retried appropriately
  after validation failure.

For learning, these signals should become represented evidence attached to
model, workflow, stage, prompt variant, replay set, and policy version concepts.
The scalar optimisation target can be computed from them, but the underlying
critic verdict must remain human-inspectable.

## 6. Authoritative Representation

The learned policy should live in Vontology/model-policy artefacts, not in
scattered Python conditionals. Python may provide replay execution, telemetry
extraction, validators, persistence helpers, and policy resolution primitives,
but not the durable decision policy.

Useful represented concepts and predicates include:

- `#V#model_use_rollout_evaluation` for a critic verdict over one rollout;
- `#V#model_stage_suitability_evidence` for evidence that a model is or is not
  adequate for a workflow stage;
- `#V#prompt_variant_suitability_evidence` for model-specific prompt variant
  results;
- `#V#actor_policy_version` for a versioned model-selection policy;
- `#V#critic_workflow` for the workflow that evaluates rollouts;
- `#V#replay_set` for curated replay cases such as the `JVNAUTOSCI-1894`
  prompt families;
- predicates linking evidence to workflow, stage, model, prompt variant, replay
  set, metric vector, verdict, expiry, and provenance.

The existing workflow model policy schema remains the runtime control surface:
see `docs/engineering/workflow_model_policy_schema.md`. The longer-term goal is
to resolve entries like "cheap reliable selector for entity-relative retrieval"
through represented suitability evidence, rather than hard-coding
`provider:model` strings as permanent policy.

## 7. Prompt Variants and Model-Specific Adaptation

Model-specific prompts are acceptable when they are explicitly represented,
evaluated, and reversible. Some models may need stricter JSON instructions,
more examples, different language, or different context ordering. This is a
model-use learning problem, not a reason to hide prompt rewrites in Python.

For example, if Gemma reliably misreads a selector instruction unless the user
request is repeated in a labelled field, that can justify a Gemma-specific
selector prompt variant. The critic should record the evidence, the actor should
select the variant only for matching model/stage conditions, and telemetry
should show that a variant was used. If Mistral or DeepSeek performs better with
another language or prompt style, the same mechanism can certify that variant
without changing the workflow's semantic authority.

## 8. Promotion, Rollback, and Governance

Policy updates should move through a promotion pipeline:

1. collect rollouts from real turns or controlled replay arms;
2. run critic evaluation with a strong model and deterministic validators;
3. generate an evidence-backed policy proposal;
4. replay against baseline and neighbouring prompt families;
5. promote only when thresholds are met for quality, cost, latency, locality,
   and regression safety;
6. record expiry or retest requirements for each certification;
7. retain rollback to the previous actor policy version.

High-impact changes, such as replacing GPT-5.5 with a cheaper model for an
important workflow stage, should require human review until the evaluation
programme has strong calibration. Low-impact changes, such as demoting a model
after repeated schema failures on a replay set, can be proposed automatically
but should still remain inspectable.

## 9. Failure Modes to Guard Against

The actor-critic loop must avoid these traps:

- **vacuous criticism**: critic output that says a turn failed but does not
  identify the stage, telemetry fields, or policy implication;
- **reward hacking**: rewarding short, cheap, or schema-valid outputs that do
  not answer the user;
- **overfitting to `JVNAUTOSCI-1894`**: certifying a model on one replay family
  without nearby or randomly sampled real-path cases;
- **prompt variant sprawl**: creating many model-specific prompts without
  evidence, expiry, or rollback;
- **stale certification**: continuing to trust a model after provider changes,
  local quantisation changes, prompt changes, or workflow changes;
- **hidden Python policy**: turning critic findings into hard-coded routing
  branches instead of represented policy updates;
- **opaque aggregate scores**: losing the stage-level evidence that explains
  why a model was accepted or rejected.

## 10. Implementation Slices

`JVNAUTOSCI-2090` should be treated as the first concrete implementation and
evaluation task for this direction. A sensible sequence is:

1. Extend the `JVNAUTOSCI-1894` replay harness to run controlled model arms
   such as `ollama:gemma4:26b` and `openai:gpt-5.5`.
2. Normalise TER extraction for model-use learning: stage IO, model settings,
   prompt variant, context lineage, tool adequacy, completion gates, answer,
   cost, and latency.
3. Add critic evaluation output with structured metric vectors and textual
   rationales.
4. Materialise model/stage/prompt suitability evidence in Vontology or the
   model registry through canonical service pathways.
5. Teach the runtime actor to consume active certifications and choose the
   cheapest certified model for each stage, with explicit escalation on live
   guard failure.
6. Add replay and telemetry acceptance reports that explain why a model is now
   trusted, distrusted, or pending retest for each stage.

Success for the first pass does not require full online reinforcement learning.
It requires an inspectable offline or asynchronous learning loop whose evidence
can safely update represented model-use policy.

## 11. Safety and Governance

Automated policy learning requires strict governance to prevent catastrophic forgetting or policy degradation.

- **Evaluation Gates**: All induced guidelines and policy updates must pass a regression suite before being promoted to production.
- **Human-in-the-Loop Thresholds**: High-impact policy changes require explicit human review and approval.
- **Rollback Capabilities**: Every policy update must be versioned, allowing the system to instantly revert to a previous state if performance degrades.

## 12. Related Design Documents

- `docs/engineering/workflow_model_policy_schema.md`
- `docs/engineering/prompt_programs_and_model_routing_playbook.md`
- `docs/engineering/agent_evaluation_and_research_uptake.md`
- `docs/engineering/agent_memory_and_enduring_knowledge.md`
- `docs/engineering/real_path_server_replay_and_telemetry_loop.md`
- `docs/engineering/jvnautosci_1964_evaluator_architecture_2026-04-23.md`

## 13. Conclusion

Transitioning to automated policy learning ensures that Von's capabilities compound over time. By systematically learning from its own execution history, Von will evolve from a statically prompted system into a continuously improving, self-optimising neuro-symbolic assistant.
