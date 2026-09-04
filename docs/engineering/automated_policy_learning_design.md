# Automated Policy Learning Design

**Status**: Proposed design guidance
**Date**: 2026-04-04
**Updated**: 2026-09-04

## 1. Purpose

This document describes how Von can learn from experience and from what people
and AI participants work out together. That learning is a central purpose of
the Von architecture, not merely a repair mechanism for failed turns. Turn
Execution Records (TERs), conversation situations, corrections,
demonstrations, successful practices, and human/AI discussion may all supply
candidate lessons. Candidate formation, evaluation, runtime activation, and
autonomous policy change remain distinct decisions.

The immediate motivating case is `JVNAUTOSCI-2090`, linked to the
`JVNAUTOSCI-1894` replay programme. The same user request, `Tell me about
myself`, succeeded with `gpt-5.4-mini` but failed with `gemma4:26b`. The useful
lesson is not "ban Gemma" or "hard-code GPT for routing". The lesson is that Von
needs introspectable model-use learning from real conversation rollouts:
workflow stage, model, prompt variant, context lineage, tool adequacy, final
answer, and post-turn critique should all become evidence for future model
selection.

## 2. Core Concepts

### 2.1 Experience and discussion as learning substrates

TERs are one important observation carrier, not the whole learning substrate. A
TER captures the context, action, and outcome of an execution turn, including:

- The initial prompt and context.
- The tools invoked and their arguments.
- The intermediate reasoning steps (e.g., ephemeral theories).
- The final output and any associated reward or validation signals.

The conversation situation preserves a different but equally important source:
what participants are trying to achieve, corrections and preferences they make,
reasons they exchange, candidate practices they formulate, and unresolved
questions. Learning must preserve the provenance and time of those
contributions rather than laundering discussion into execution telemetry.

### 2.2 The Learning Progression

The architecture separates four states rather than treating policy update as
the inevitable result of observation:

1. **Observation or contribution**: Preserve the relevant experience, outcome,
   correction, demonstration, question, rationale, or deliberative proposal in
   its source situation, with contribution and evidence provenance.
2. **Attributed candidate**: Formulate zero, one, or several provisional
   lessons with an explicit contributor, purpose, target, and root evidence. A
   candidate is discoverable for inspection and revision but is not a runtime
   instruction.
3. **Evaluated candidate**: Attach supporting, contrary, or inconclusive
   evidence from a comparison proportionate to the candidate's actual claim.
   Evaluation may strengthen, narrow, revise, reject, or leave the candidate
   unresolved.
4. **Active projection**: Select a particular evaluated revision for a bounded
   actor, audience, target, situation, and decision point through the
   authoritative activation path. Record the exact projection so later outcome
   evidence can challenge it.

A single experience or human/AI discussion can justify retaining a narrow,
non-active candidate for later deliberation. It need not demonstrate recurring
harm, but neither does it prove efficacy or authorise runtime projection.
Candidates may remain useful knowledge even when no active consumer has yet
earned implementation.

### 2.3 Whose Learning, Action, and Outcomes

Learning records must not collapse different semantic roles into a single
`user`, `owner`, or `scope` field when the distinction matters:

| Dimension | Question |
|---|---|
| **Contributor** | Who supplied the observation, correction, rationale, demonstration, or candidate? |
| **Actor** | Which person, agent, team, organisation, or other principal would act differently? |
| **Target role** | Which role, capability, workflow, stage, prompt, tool, or practice is the lesson about? |
| **Audience** | Who may inspect or receive the candidate or active projection? |
| **Beneficiary** | Whose interests or outcomes is the proposed learning intended to serve? |
| **Purpose** | What end, function, work product, or wider objective is being advanced? |

Relevant outcomes may concern an individual, a role or team, an organisation,
a social group, a society, humanity, Earth, or particular ecosystems. These are
possible contexts and beneficiaries, not a fixed hierarchy, an authority
grant, or mandatory ceremony for every candidate. A lesson may have multiple
beneficiaries and purposes, and evidence may show gains for one alongside costs
or disagreement for another. Preserve that plurality rather than reducing all
outcomes to one scalar reward or assuming a single uncontested good. Visibility
comes from the source and intended audience; a broad beneficiary or purpose
does not make private material public or authorise anyone to act.

## 3. Mechanisms for Learning and Policy Change

### 3.1 Candidate Induction

Instead of humans manually writing new rules in `AGENTS.md` for every edge
case, the system may induce candidate guidelines from experience.

- **Lesson recognition**: Bounded experience or discussion can expose a useful
  correction, successful practice, rationale, opportunity, or failure pattern.
- **Candidate proposal**: Von or a participant may formulate a concise,
  attributed candidate without claiming that it is already effective.
- **Source-native retention**: Keep a provisional lesson in the conversation
  situation or another existing carrier. Materialise it in Vontology only when
  independent identity, cross-session revision, or a demonstrated consumer
  makes that representation useful.
- **Optional lenses**: Successful practice, failure avoidance, and
  low-imposition exploration are useful questions an inductor may ask. None is
  a compulsory output category. An observation can yield no durable candidate,
  one candidate, or several candidates of other kinds.

For textual guidance, “approved” is not implied by induction. The
[represented-advice design](represented_advice_design.md) owns the proposed
semantics, applicability, projection, retraction, and ratchet test: an induced
item remains a defeasible candidate until evidence proportionate to its scope
supports activation.

### 3.2 Retained Substrate and Retired Mechanism

The September 2026 audit distinguished the purpose and stored data of the old
episode-learning work from its defective automatic guidance mechanism:

- [`episode_critique_memory.v1`](../../src/backend/services/episode_critique_memory_service.py)
  remains a useful source-native store for episode and request identity,
  namespace/user/organisation context, critic and evaluator state, implicated
  workflows, tools and concepts, remediation and Jira links, structured
  improvement suggestions, evidence receipts, and self-improvement state.
- [Explicit episode evaluation](../../src/backend/workflows/durable/episode_evaluation_workflow.py)
  can still persist that memory and route remediation. It launches bounded
  self-improvement work only when the assessment explicitly sets
  `maintenance_follow_up_recommended` to true; ordinary critique retention does
  not imply a proposal. Existing represented
  [proposal and promotion-evaluation workflows](../../src/backend/workflows/repo_seed_bundles/episode_self_improvement_workflow_seed_bundle.json)
  can turn a selected workflow suggestion into a reviewable candidate and
  record a recommendation.
- [Workflow Studio](../../src/frontend/web/von_interface/static/js/workflowStudioPage.js)
  already projects recent workflow-targeted critique suggestions for human
  inspection, including their target, rationale, proposed change, evidence
  references, and source episode. It explicitly says that these guidance
  surfaces are not automatically applied. This is an existing
  candidate-discovery consumer, not evidence of runtime efficacy.

That substrate should be extended where it fits rather than rebuilt. The
[current episode critic prompt](../../src/backend/workflows/repo_seed_bundles/episode_evaluation_prompt_seed.md)
is predominantly repair-oriented, so it does not yet supply the full positive
function: learning successful practices, attributed discussion-derived
lessons, or optional exploratory ideas. Explicit MCP reads now support
candidate deliberation, but there is not yet a configured active
decision-point projection for such advice.

The retired path forced successful-run, failure-avoidance, and next-run
exploration text after every episode; stored literal no-lesson output; appended
items without an applicability-aware active selection; and exposed candidate
history to proposal, evaluation, or release contexts. Those mechanisms are not
requirements of learning. The three purposes survive only as optional lenses,
and candidate discovery remains separate from active projection.

The bounded replacement implements `learning_candidate.v1` as a non-active
`#V#artifact` with a canonical text body and source, contribution, target,
audience, beneficiary, purpose, visibility, and revision metadata. Internal MCP
operations support capture, get, list, and revision from a verified episode
critique or actor- or organisation-visible conversation source; an
unmaterialised chat-history session is the narrower actor-owned form. There is
no automatic trigger, inductor, prompt injection, active selector, evaluator,
or promotion path. The new surface therefore preserves a discussion- or
experience-derived lesson for later human/AI deliberation without claiming that
it already improves runtime behaviour.

The bounded audit found **no evidence of a knowledge-base advice ratchet**: no
relevant accumulated advice relations, scoped assertions, profiles, or live
prelude instances were found. The observed accumulation and ratchet evidence
were instead in Python code, tests, seed bundles and migration compatibility,
Jira records, and design documentation. Possible ratchets in represented
knowledge remain hypotheses to test if an active consumer is built, not
demonstrated harms that justify controls now.

### 3.3 Retrieval Strategy Updates

Retrieval policies may be adjusted from measured usage and outcome patterns.

- **Relevance Evidence**: Record which fragments were projected or explicitly
  referenced and the resulting outcome. Do not claim actual model use or causal
  benefit without an independent comparison.
- **Weight Adjustment**: Propose down-weighting or archiving noise and boosting
  useful structures for evaluation. Numeric retrieval/routing adaptation is a
  specialised policy surface, not represented textual advice merely because it
  uses the same evidence or release machinery.
- **Candidate discovery versus active projection**: Humans and AI participants
  may inspect non-active candidates to deliberate, merge, revise, or reject
  them. Runtime decisions receive only deliberately selected active revisions,
  with exact target and exposure lineage. Archival retention without any
  discoverability is not useful learning, but discoverability does not imply
  activation.

### 3.4 Dynamic Routing Adjustment

Model and agent routing should improve through empirical performance data.

- **Cost-performance evidence**: Where routing is the selected learning job,
  retain the cost, latency, and relevant outcome evidence of compared models or
  agents for the bounded task category.
- **Router candidate**: Propose a routing or escalation change when that
  evidence distinguishes it from the current policy. Re-weight active routing
  only after evaluation and activation through its own authority surface.

## 4. Actor-Critic Model-Use Learning

Von should treat model choice as a learnable, represented policy over workflow
stages where the evidence shows that fixed selection is inadequate. This is one
concrete branch of the broader learning architecture, not its definition. The
intended loop is actor-critic in the ordinary reinforcement-learning sense, but
grounded in Von's existing workflow, Vontology, prompt, and telemetry
architecture.

Here, the **actor** is specifically the runtime model-use policy; it is not the
contributor, beneficiary, or audience defined in Section 2.3. It may choose a
model arm, prompt variant, fallback chain, and escalation threshold for a
workflow stage. Where cost is material, it may prefer the cheapest active model
shown to be adequate for the specific workflow, stage, prompt profile, data
sensitivity, and actor or organisation context. It may also escalate when an
observed validation or postcondition failure makes that response useful.

The **critic** is a reviewer workflow that evaluates completed turn rollouts.
It reads the TER, LLM IO telemetry, model and prompt identifiers, context
lineage, tool calls, completion gates, final answer, user feedback, and
postcondition checks. Its job is to explain what happened, assign structured
outcome signals, and form attributed candidates when a useful lesson is
present. A stronger model can be a design variable when judgement quality needs
it, but is not a universal requirement. Candidate formation does not update the
actor policy; independent evaluation and scoped activation remain later steps.

The existing explicit episode evaluator and `episode_critique_memory.v1`
provide much of this critic substrate already. They should be reused and
extended, especially to recognise successful practice and discussion-derived
lessons, rather than supplemented with another mandatory post-turn evaluator.

The **rollout** is the full conversation turn execution, not just the final
assistant text. A rollout record for model-use learning should include the
available fields material to its claim, such as:

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

When the learning job warrants a critic, it should preserve multiple
inspectable signals rather than replacing them with a single opaque reward.
Useful signals include:

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
  was sufficient without sacrificing task quality, where those dimensions are
  material to the purpose;
- **recovery quality**: whether the actor escalated or retried appropriately
  after validation failure.

Outcome evidence may also concern effects on a role, team, organisation, social
group, society, humanity, Earth, or an ecosystem. Retain the dimensions that
are material to the candidate's stated beneficiaries and purpose, including
contrary or distributional effects; do not invent every dimension for every
turn. A scalar optimisation target may be computed for a bounded decision, but
it must not erase the underlying evidence, uncertainty, dissent, or plural
outcomes.

Raw telemetry, documents, traces, and events should remain in their
fit-for-purpose stores. Reuse `episode_critique_memory.v1` and other existing
carriers for structured summaries and evidence locators. Materialise a
first-class Vontology evidence item only when stable identity, cross-session
retrieval, independent revision, governance, or an active consumer actually
needs it. Whatever the store, retain enough inspectable evidence to distinguish
the critic's verdict from the observation it interprets.

## 6. Candidate and Active-Policy Representation

An attributed candidate should first remain in the weakest existing carrier
that supports discovery, provenance, revision, and its intended audience. A
policy revision selected for runtime use should live in the authoritative
Vontology or model-policy surface, not in scattered Python conditionals.
Python may provide replay execution, telemetry extraction, validators,
persistence helpers, and policy resolution primitives, but should not silently
become the durable home of task-specific semantic policy.

If a concrete model-use consumer earns represented evidence and policy, useful
concepts and predicates may include:

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

These are candidate additions, not a schema that every observation must
instantiate. Search first for equivalent identities and extend the retained
episode evidence, critique, proposal, and policy surfaces where adequate.

The existing workflow model policy schema remains the runtime control surface:
see `docs/engineering/workflow_model_policy_schema.md`. A future model-use
consumer may resolve entries like "cheap reliable selector for entity-relative
retrieval" through represented suitability evidence rather than hard-coding
`provider:model` strings as permanent policy. That goal does not imply that a
general active advice loop exists today.

## 7. Prompt Variants and Model-Specific Adaptation

Model-specific prompts can be useful when observed behaviour distinguishes a
prompt-fit problem from model, tool, transport, authority, or context failures.
Some models may need stricter JSON instructions, more examples, different
language, or different context ordering. A durable active variant should be
inspectable at the authority surface and its actual selection observable. This
is a model-use learning problem, not a reason to hide prompt rewrites in
Python.

For example, if Gemma reliably misreads a selector instruction unless the user
request is repeated in a labelled field, that can justify a Gemma-specific
selector prompt variant. The critic should record the evidence, the actor should
select the variant only for matching model/stage conditions, and telemetry
should show that a variant was used. If Mistral or DeepSeek performs better with
another language or prompt style, the same mechanism can certify that variant
without changing the workflow's semantic authority.

## 8. Promotion, Rollback, and Governance

Only selection into active runtime use is a promotion. Retaining, displaying,
discussing, or revising a non-active candidate does not require the release
machinery below. For a consequential active policy change, use only the parts
of this path that its actual claim and residual consequence require:

1. identify the exact candidate revision, actor, target, audience, beneficiary,
   purpose, applicability claim, and root evidence that are material;
2. compare it with the best fair simpler baseline using real turns or bounded
   replay where that comparison can change the decision;
3. evaluate the material outcome dimensions independently of the candidate's
   own induction context, using deterministic checks or a suitable critic where
   each adds value;
4. select it through the existing standing authority for that policy surface;
5. publish and read back the exact active locator, while preserving the
   previous selection or an empty no-advice state when recovery needs it; and
6. observe actual projections and later outcomes so counterevidence can lead to
   revision, supersession, retraction, or retest.

Do not require every metric, neighbouring prompt family, evaluator type,
expiry, or rollback mechanism by default. Apply the lightest lifecycle that
preserves the active item's real scope, provenance, observation, and feasible
recovery. Human inspection should remain available throughout. Human approval
is required when the governing authority surface requires it or when a material
choice cannot be resolved from standing delegation and evidence, not simply
because a policy change is imaginable.

For example, replacing a proven model on an important workflow stage may need a
reviewer and broader outcome comparison because the decision has a credible
quality and operability consequence. A bounded reversible ranking adjustment
with direct outcome observation may need less. Candidate capture alone changes
neither behaviour nor authority and should not inherit those controls.

## 9. Learned Failure Modes and Risks to Observe

The retired path provides direct design evidence for several traps. Preserve
the intended learning function while rejecting these mechanisms:

- **forced lesson production**: requiring success, failure, and exploration
  guidance after every episode, including literal no-lesson text, creates work
  and apparent policy without a lesson;
- **additive latest-N activation**: treating recently appended text as active
  and applicable lacks deliberate selection, deduplication, revision, or
  retraction;
- **self-contaminated evaluation**: injecting candidate history into the
  proposal's own evaluator or release context weakens independent evidence;
- **candidate-as-system-policy**: rendering unpromoted critique suggestions as
  runtime system guidance falsely upgrades their epistemic and authority
  status;
- **mandatory post-episode mutation**: an additional evaluator or write tail on
  every episode needs a demonstrated learning job and consumer.

Other failure modes should be monitored when a concrete implementation makes
them reachable:

- **vacuous criticism**: critic output that says a turn failed but does not
  identify the stage, telemetry fields, or policy implication;
- **reward hacking**: rewarding short, cheap, or schema-valid outputs that do
  not answer the user;
- **overfitting to `JVNAUTOSCI-1894`**: claiming broad model suitability from
  one replay family;
- **prompt variant sprawl**: creating many model-specific prompts without an
  evidenced maintenance or removal path appropriate to their effect;
- **stale certification**: continuing to trust a model after provider changes,
  local quantisation changes, prompt changes, or workflow changes;
- **hidden Python policy**: turning critic findings into hard-coded routing
  branches instead of represented policy updates;
- **opaque aggregate scores**: losing the stage-level evidence that explains
  why a model was accepted or rejected.

This list is diagnostic, not a universal control checklist. A merely
hypothetical harm can motivate observation or a bounded experiment; it does not
justify restricting an otherwise authorised strategy before a credible causal
path or evidence shows that the harm is materially reachable.

## 10. Implementation Slices

Implementation should begin with reuse and one selected learning job, not a new
general loop. The current repository implements bounded non-active portions of
slices 1, 2, and 4; slices 3, 5, and 6 remain future, separately earned work:

1. **Inventory and reuse the substrate.** Treat TERs, episode evidence bundles,
   evaluator axes, `episode_critique_memory.v1`, explicit episode evaluation,
   structured improvement suggestions, Workflow Studio inspection, and the
   reviewable self-improvement proposal path as existing components. Extend
   them only where the selected job exposes a concrete gap.
2. **Preserve discussion contributions.** Use or extend the smallest
   source-native way to retain an attributed correction, rationale, successful
   practice, or proposal from the conversation situation. The first
   implementation links a concise candidate to an actor- or
   organisation-visible conversation concept, or to an actor-owned chat-history
   session, so ordinary capture does not require materialising the whole
   conversation in Vontology. Link to root evidence rather than copying the
   whole discussion into a new policy store.
3. **Broaden candidate formation.** Let the existing critic or a separately
   justified inductor return zero, one, or several candidates. Treat success,
   failure avoidance, and low-imposition exploration as optional lenses, and
   preserve contributor, target, audience, beneficiary, purpose, and plural
   outcome claims when material.
4. **Make non-active candidates discoverable and revisable.** Extend the
   existing human inspection surface before creating a second inventory. The
   first implementation exposes trusted-scope MCP capture, get, list, and
   revise operations for AI-assisted deliberation, while Workflow Studio keeps
   its existing human view of structured episode suggestions. There is not yet
   a unified human candidate-management view. Do not inject this inventory into
   runtime prompts.
5. **Select one real consumer.** Only after a candidate class and decision point
   demonstrate value, add the smallest active locator and applicability-aware
   projection. Compare it with the best fair simpler baseline and with existing
   specialised learners rather than duplicating them.
6. **Observe exposure and outcome.** Record the exact active revision projected
   at the decision, then attach supporting, contrary, or inconclusive outcome
   evidence and exercise revision or removal when the observed result warrants
   it.

`JVNAUTOSCI-2090` remains one concrete model-routing branch. It may extend the
`JVNAUTOSCI-1894` replay harness and normalise stage IO, model settings, prompt
variant, context lineage, tool adequacy, completion gates, answer, cost, and
latency. Its fair baseline includes the existing workflow/model policy and any
current numeric outcome-derived selector learning; it should not establish a
general advice layer by assertion.

The first repository slice demonstrates source-grounded non-active capture,
AI-assisted discovery, canonical read-back, and revision. It does not yet
demonstrate a human candidate-management view or improvement to a real
decision. A later slice may show that one evaluated candidate improves a real
decision through bounded active projection. Neither slice requires full online
reinforcement learning, materialising every signal in Vontology, or claiming
that the loop is already closed.

## 11. Proportionate Governance and Stopping Rule

Governance belongs at the effect it protects. Candidate capture needs source,
contribution, visibility, and epistemic status preserved; it does not need the
same release controls as an active production policy. Active changes need
evidence and recovery proportionate to their bounded claim and credible
residual consequence.

- **Evaluate the claimed effect**: A broad or consequential active policy may
  warrant matched regression evidence. A narrow observable and recoverable
  projection may need only a bounded comparison and direct read-back.
- **Preserve authority distinctions**: Contributor, actor, target role,
  audience, beneficiary, and purpose do not confer authority on one another.
  Use the standing authority of the selected policy surface and ask only when a
  material unresolved choice or missing authority requires it.
- **Keep recovery feasible**: Use an explicit active selection, prior revision,
  empty selection, or other recovery mechanism when the implemented effect can
  otherwise persist after contrary evidence. Do not demand instant rollback or
  version every provisional observation when deletion or source-native revision
  is adequate.
- **Let risks earn controls**: A specific, credible, materially unacceptable
  outcome and a causal path or evidence that it is reachable may justify the
  least restrictive effective control. Speculation alone should lead at most to
  observation, not a pre-emptive ratchet of approvals, gates, schemas, or
  prohibitions. Organisational review and approval procedures are interventions
  too: they carry human burden and can ratchet just as code and tests do, so
  retain them only while their stated need remains justified.
- **Stop when the decision is supported**: Do not expand evaluation to every
  model, task, metric, beneficiary, or imagined failure. Stop when further
  evidence is unlikely to change capture, activation, rollback, or rejection,
  and report bounded uncertainty honestly.

## 12. Related Design Documents

- [JVNAUTOSCI-2719](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2719)
  — represented-advice retirement, retained learning substrate, and next
  bounded candidate-learning slice
- `docs/engineering/workflow_model_policy_schema.md`
- `docs/engineering/prompt_programs_and_model_routing_playbook.md`
- `docs/engineering/agent_evaluation_and_research_uptake.md`
- `docs/engineering/agent_memory_and_enduring_knowledge.md`
- `docs/engineering/represented_advice_design.md`
- `docs/engineering/real_path_server_replay_and_telemetry_loop.md`
- `docs/engineering/jvnautosci_1964_evaluator_architecture_2026-04-23.md`

## 13. Conclusion

Von learns when it can preserve what experience and discussion reveal, make
attributed candidates available for later human/AI deliberation, and revise
them as evidence changes. That purpose is valuable before any candidate becomes
active. Runtime policy changes make a further claim: an evaluated revision
improves a real decision over the fair simpler baseline for its stated actors,
beneficiaries, and purposes. Only that later claim needs scoped activation,
exposure evidence, and proportionate recovery.

The current repository contains useful episode evidence, critique, human
inspection, proposal, and promotion-recommendation substrate, plus a new
source-grounded non-active candidate capture and revision path. It still has no
active advice consumer or closed exposure-to-outcome learning loop. Building on
the retained substrate selectively preserves learning without repeating the
demonstrated code-and-process ratchet or imposing a new one in response to
harms that remain merely hypothetical.
