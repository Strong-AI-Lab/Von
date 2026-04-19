# AI Agent Guide

This file is the concise operating constitution for AI agents working on Von.

All AI agents must read this file and `docs/engineering/security_considerations.md` before starting any work.

## 1. Purpose

Von is being built as a deployable neuro-symbolic agentic assistant system for teams, demonstrated first for research-team support. It should remain able to absorb newer models, prompt optimisers, routing methods, memory architectures, fine-tunes, and policy-learning methods without rewriting task policy into Python.

This file is intentionally shorter and sharper than a catch-all agent handbook. It states the core operating rules, and it tells agents which additional documents become mandatory under which circumstances.

## 2. Required reading

### 2.1 Always read

- `AGENTS.md`
- `docs/engineering/security_considerations.md`

### 2.2 Read before finalising the plan for any substantial implementation task

- `docs/engineering/intro_to_modern_agentic_ai_for_coding_agents.md`

### 2.3 Situation-specific mandatory reading

Read the following before planning or implementing work in the matching area:

- **Workflow/orchestration changes**  
  `docs/engineering/von_workflow_language_manual.md`

- **Prompt behaviour, model selection, prompt optimisation, routing, or fine-tuning**  
  `docs/engineering/prompt_programs_and_model_routing_playbook.md`

- **Retrieval, memory, RAG, KB growth, or long-horizon state**  
  `docs/engineering/agent_memory_and_enduring_knowledge.md`

- **Architecture-shaping evaluation, benchmark design, or research-sensitive capability changes**  
  `docs/engineering/agent_evaluation_and_research_uptake.md`

- **Minimal-imposition, elicitation, or write-policy questions**  
  `docs/engineering/minimal_imposition_design_principle.md`

- **Frontend/UI changes, browser acceptance, or authenticated user-view testing**
  `docs/engineering/frontend_browser_user_view_validation.md`

If the task crosses multiple areas, read all relevant documents.

### 2.4 Operational companion

For practical engineering guidance distilled from prior implementation and
debugging work, see:

- `docs/engineering/operational_engineering_guide.md`
- `docs/engineering/maintaining_global_design_constraints_and_authority_alignment_with_coding_agents.md`

Use that document for environment handling, shell and host behaviour,
credential-path issues, access/tooling defaults, pytest execution practice, and
similar operational lessons that do not belong in this constitutional guide.
For frontend/browser user-view validation practice, also see
`docs/engineering/frontend_browser_user_view_validation.md`.

## 3. Core operating rules

1. Use New Zealand English spelling by default.
2. PowerShell is the default shell. Do not emit Bash-only syntax unless explicitly asked for Bash.
3. Never clobber `.env`. Only touch it when explicitly required, and never print secrets.
4. Vontology is a first-class engineered authority surface, and the authoritative source of truth for persistent knowledge, prompts, workflow artefacts, predicates, types, and other enduring represented state unless an exception is explicitly justified.
5. Do not use direct DB access for Vontology-governed data. Use the Vontology API, MCP tools, or canonical service pathways.
6. Branch first for substantial Jira work. Keep Jira status, comments, assignee, and links in sync with the real implementation state.
7. Prefer MCP and existing repo control surfaces over ad-hoc scripts or handwritten workarounds.
8. Workflow-first / KB-authoritative is the default doctrine: if a durable behaviour or policy change can live cleanly in workflow, prompt, KB, or Vontology artefacts, prefer changing it there rather than encoding the policy in Python.
9. Decision-policy authority extends beyond routing. Ranking, recommendation, matching, classification, explanation, retrieval strategy, planning, and stage-specific context construction count as authored behaviour.
10. Python should usually provide reusable support surfaces: execution, validation, tool wrappers, rendering, telemetry, persistence, safety checks, integrations, and genuinely missing reusable primitives.
11. Treat the shared turn context as a first-class authority surface. By default, selector, planner, tool-use, and response phases should consume the same accumulated turn context; stage-specific additions are acceptable, but silent phase-specific pruning or substitution is not.
12. Workflows, tools, and telemetry are means to a user-facing end. Do not let execution bookkeeping, workflow completion summaries, or renderer diagnostics replace the answer unless the user explicitly asked for that operational view.
13. If a stage changes the effective LLM context, make the change explicit and preserve it in telemetry so later diagnosis can see what the model actually saw.
14. Do not place Vontology-governed prompt bodies in Python. Prompts are operational policy and should live in Vontology text relations.
15. If a required Vontology/workflow/prompt authority surface is unavailable, fail closed for that feature. Do not silently fall back to stale prompts or heuristic hacks.
16. Do not relax workflow- or KB-authority requirements to justify a preferred implementation. Any genuine exception requires explicit human approval and a named missing reusable primitive or authority surface.
17. Heuristic fallbacks must be explicitly temporary, non-authoritative, linked to a removal task, and easy to delete.
18. Destructive mutations require explicit confirmation or workflow escalation; do not infer permission for deletes or removals from general task context.
19. Default to minimal imposition. Exhaust machine-side retrieval, context, search, and reasoning before asking the user to do extra work.
20. Treat older Jira wording sceptically. Before implementing a Jira task that will make a significant architectural or other significant change, perform a bounded staleness/implementability review first: verify the current code path and line ranges, inspect what has materially changed since the task was written, check the current targeted validation surface, reinterpret the task toward the current Von architecture, and update Jira plus any genuinely helpful adjacent/precondition tasks when the original wording is materially stale.
21. For research-sensitive, architecture-shaping, or long-horizon-agent tasks, do a short targeted literature review before finalising the plan.
22. Run targeted impacted validation by default, including real call-path tests where relevant. Do not claim broader coverage than you actually ran.
23. End-to-end or user-visible acceptance requires direct evidence on the exact path or the nearest real path, not only nearby unit tests.
24. Substantial-task reflection is mandatory. Extract durable lessons, update docs when warranted, and create Jira tasks for real process gaps.
25. Prefer durable capability improvements over case-specific patches. If a proper noun from the triggering task appears in core logic, treat that as a design smell unless there is a strong reason.
26. Notice when functions or methods are becoming large, tangled, or repeatedly patched. Treat that as a design signal, not merely a style issue. Prefer refactoring toward clear reusable support-surface functions with explicit inputs/outputs and better test seams.
27. When coordination or decision-making logic inside Python looks like authored workflow policy, actively consider whether it should instead live in Von workflows, Vontology artefacts, prompt/programme artefacts, or other represented authority surfaces. Refactoring should reduce hidden code-side policy, not merely rearrange it.
28. Agents are authorised to create new refactoring or architecture-alignment Jira tasks when they encounter code of this kind. Such tasks should explain the observed structural problem, why it matters now, and how the proposed refactor would improve code coherence, represented-authority alignment, and future change safety.
29. Do not use size alone as the criterion for opening refactor tasks. A strong candidate usually combines size or repeated patch pressure with mixed responsibilities, weak test seams, user-visible interpretation risk, integration-boundary sprawl, or drift of workflow/control/verification logic away from represented authority.
30. After a substantial workflow/orchestration refactor, perform a bounded structural scan of adjacent code for monoliths or authority-drift risks. Create linked follow-on Jira tasks when the architectural case is clear; do not create noise tickets based only on line count.
31. In long coding sessions, perform periodic host-hygiene checks and clean up stale local helper processes spawned by the work. Before starting another local server, browser replay, or MCP-heavy batch after repeated retries/restarts, check for duplicate Von servers, stale Playwright/browser daemons, old MCP stdio helpers, and similar leftovers. Do not normalise piling up extra local services on new ports as a workaround for not cleaning up the old ones.

## 4. Workflow, prompt, and KB authority

### 4.1 Before coding behaviour changes

Before implementing behaviour, name the intended authoritative artefacts in Jira or task notes:

- workflow concepts or VWL definitions
- prompt concepts and text relations
- template or profile concepts
- routing metadata
- KB assertions or other Vontology-native structures

Also state what code will remain support-only.
Treat those artefacts as implementation surfaces, not as commentary about an implementation whose real policy still lives elsewhere in Python.

### 4.2 What counts as a design error

Pause and rethink if you are about to introduce:

- task-specific orchestration in Python that VWL could express
- lexical scoring tables, stopword lists, token-overlap scoring, or other language-specific semantic steering for durable recommendation, ranking, routing, or classification policy
- phase-specific context-thinning or ad-hoc prompt/context shaping in Python that quietly changes what different LLM stages know
- repo-side prompt/template/workflow files that become the real production authority
- code-side prompt defaults for a Vontology-governed feature
- hard-coded ontology term lists that should be resolved from Vontology

### 4.3 If VWL or Vontology is insufficient

Add only the missing reusable primitive or support surface in code:

- execution/runtime surface
- validation
- telemetry
- persistence
- tooling support

Then keep the authored workflow or policy in Vontology where possible, and document the capability gap in Jira. Do not silently make Python the long-term home of a policy merely because the represented surface needs one more reusable primitive.

### 4.4 Before closing a workflow- or KB-authoritative task

Verify all of the following:

- the authoritative decision policy lives in workflow/prompt/KB/Vontology artefacts or materialised KB assertions
- LLM-facing stages share the intended turn context, or any justified reduction is explicit, evaluated, and telemetry-visible
- user-facing answer semantics are preserved and execution bookkeeping stays in supporting surfaces unless explicitly requested
- Python remains a support surface rather than hidden task policy
- any context additions or reductions are visible in telemetry
- any heuristic fallback is explicitly temporary and non-authoritative
- repo-side seeds or snapshots could be deleted without losing authority

## 5. Prompt and model rules

1. Treat prompts, selector/classifier prompts, and stage-local instruction layers as versioned, inspectable policy artefacts.
2. When prompt behaviour changes, inspect the authoritative prompt and the actual LLM context at that stage before doing code-first diagnosis.
3. Prefer prompt revision, retrieval/context improvement, validators, or model routing over lexical pseudo-NLP or code-side semantic matching.
4. Default to a shared turn-context object across LLM stages; stage-local additions should layer onto it rather than replace it unless there is an explicit validated reason.
5. When model choice matters, think in terms of a model portfolio: small local models, medium models, frontier models, fine-tunes, and symbolic modules.
6. Keep model-specific quirks out of durable business logic whenever possible.
7. Make model differences visible through telemetry, evaluation, policy metadata, and context-lineage diagnostics when context evolves across stages.

## 6. Vontology and representation rules

1. Start Vontology-related work by resolving candidate concepts and existing predicates/types before inventing anything new.
2. Reuse existing Vontology concepts when possible; extend them rigorously when necessary.
3. Prefer Vontology types and queries over fixed concept lists in code.
4. Preserve user-authored text exactly unless the user explicitly asked to rename or normalise it.
5. Use canonical ontology predicates rather than ad-hoc structural relationship fields when predicate concepts exist.
6. Keep types and individuals cleanly separated.
7. Distinguish carefully between predicate types and predicate instances. `#V#binary_predicate` is a type of predicate, so it is a type-level concept. Concrete predicates such as `#V#hasSubprocedure`, `#V#hasInstance`, and most domain predicates are instances of `#V#predicate` (and may also be instances of a specialised predicate type such as `#V#binary_predicate`). Do not create persisted predicate concepts as types under `#V#predicate` unless you are intentionally defining a new predicate subtype. If the concept is meant to be used as an actual relationship, it should usually be an instance of `#V#predicate`, not a type of it.
8. Record chosen canonical concept IDs in Jira for ontology-shaping work.

## 7. Task lifecycle discipline

### 7.1 At task start

- create or switch to the task branch
- transition the Jira issue to `In Progress`
- when creating Jira issues on the user's behalf, assign them to the authenticated Jira user by default unless the user explicitly asks for a different assignee or Jira refuses the assignment
- review task age, linked issues, and likely staleness
- before implementing a Jira task that will make a significant architectural or other significant change, perform and record a bounded staleness/implementability review: confirm the live code path and current line ranges, identify meaningful since-ticket changes, check the present targeted test/validation surface, and update the Jira task wording or linked precondition tasks if the original framing is no longer accurate
- identify the authoritative KB/workflow/prompt artefacts
- decide which situation-specific docs are mandatory for this task

### 7.2 During work

- post concise Jira progress comments at meaningful milestones
- use existing canonical helpers and pathways before adding new ones
- run targeted tests as you go
- keep changes minimal but systemic where a shared fix is clearly better than a point fix
- during long sessions or after repeated local restarts/browser replays, run a bounded operational cleanup check for stale local servers, browser/tool daemons, and temp probe processes before starting yet another copy
- do not treat unrelated local changes as automatic exclusions from a commit; if they appear consistent with the branch direction, do not weaken task correctness, and do not conflict with explicit user intent, it is acceptable to bundle them rather than spend disproportionate effort separating them mechanically

### 7.3 Before saying the task is done

- A task is not fully complete until code, Jira, and any required authoritative
  Vontology/workflow state all match the claimed outcome.
- Do not stop at local implementation, local validation success, or a "ready to commit" state unless the user explicitly asks to pause there. For Jira implementation work, the default expectation is commit, merge to `main`, verify `origin/main`, and close the Jira issue before reporting completion.
- rerun targeted regression checks
- gather direct acceptance evidence
- verify any required Vontology/workflow/KB state changes were actually
  materialised; repo-side support code alone is not sufficient closure evidence
- review linked issues and update or transition them as justified
- merge to `main`, verify `origin/main` contains the intended commit(s), then close the Jira issue
- finish branch/worktree hygiene for the completed task: fast-forward any retained local `main` worktree that is meant to track `origin/main`, move the current worktree off the completed task branch, and delete merged local/remote task branches unless there is a clearly recorded reason to keep them
- complete the reflection pass
- if the task removed or replaced heuristic code, code-side semantic steering, or other anti-patterns, verify that no tests remain that pin the old behaviour (see Testing rule 11). Tests that assert on removed heuristic outputs, autostub authority-backed replacements to avoid exercising them, or encode stale design assumptions are themselves anti-patterns and must be removed or rewritten as part of the same task.

### 7.4 Jira task design quality

Jira tasks are durable artefacts that other agents and humans rely on months later. A task that omits the analysis behind it forces the next implementer to redo the entire diagnosis. Every implementation or bug-fix task must include:

When drafting or updating an implementation task, use recent evidence as the entry point for diagnosis, not as permission for a narrow local patch. Be very clear in the description about why the work needs to be done in light of the recent changes, and what it should achieve in terms of code coherence, design quality, and alignment with Von's architectural intent. If the evidence leads into a massive or over-entangled function, say so explicitly and frame the task around the durable structural change that is needed — for example extraction of reusable support surfaces, clearer authority boundaries, or better testable seams — rather than normalising another local edit inside the monolith. For workflow/orchestration work in particular, make explicit that workflow control and verification must remain Vontology-authored and mostly determined through flexible LLM reasoning over represented artefacts; the acceptable Python changes are support-surface improvements, not migration of decision policy or verification criteria into code.

Agents should not wait for a human to request such a task explicitly. If they observe a large/tangled function, repeated local patching in the same area, or a drift risk where Python is absorbing workflow control or verification logic, they should open a refactoring or architecture-alignment Jira task proactively and document the rationale clearly.

When scanning for such opportunities, prefer opening tasks where you can state the architectural risk precisely: mixed boundary responsibilities, hidden coordination or decision policy in Python, workflow/verification logic drifting away from Vontology/workflow authority, user-visible meaning being shaped in one monolith, poor test seams, or integration-boundary sprawl that invites duplicate definitions or schema drift. Do not open a refactor task solely because a function is long.

1. **Observed failure evidence.** Concrete data: request IDs, telemetry field values, error messages, or user-visible symptoms. Quote the actual values — do not paraphrase.
2. **Code path analysis with file and line references.** Trace the execution path through the relevant functions, naming each file, function, and approximate line number. The reader should be able to follow the path without searching.
3. **Competing hypotheses with diagnosis steps.** When root cause is uncertain, state each plausible hypothesis explicitly and describe the concrete steps (log inspection, breakpoint, test case) that would confirm or eliminate it. Do not present a single guess as established fact.
4. **Fix approach per hypothesis.** For each hypothesis, describe the intended code change — which function, what logic, why it resolves the root cause. If hypotheses share a fix, say so.
5. **Regression test requirements.** Name the specific assertions the fix must be tested against (not generic "add tests"). State what conditions the test must reproduce and what the expected vs. failing outcome is.
6. **Relationship to other tasks.** Link related issues and explain the relationship (shared root cause, same pipeline stage, discovered together, one blocks another). A link without explanation is insufficient.
7. **Key file references.** List the primary files the implementer will need to read, with the relevant function or section name.
8. **User-impact summary.** Add a brief plain-language comment (not just in the description — also as a visible Jira comment) explaining what the task means for people who actually use Von. State concretely what users will see differently after the work is done, or state explicitly that nothing changes for users and why the work matters anyway (e.g. test quality, reliability, performance). This summary exists so that humans can triage, prioritise, and communicate about the task without decoding the technical diagnosis. It also forces the agent to confirm it has actually reasoned about user impact rather than only code mechanics.

When creating diagnostic or bug tasks from a failed-turn analysis:

- Include the request_id and the turn execution record field values that demonstrate each failure.
- When telemetry shows a candidate was excluded, record which filter excluded it and what the filter's inputs were.
- When the failure involves a pipeline (discovery → selector → dispatch), trace the data through each stage boundary and identify where values diverge from expectation.
- Do not describe test gaps abstractly. Name the specific structural flaw in the existing test (e.g. "the test pre-registers workflows, so the registry gate always passes").

Tasks that consist only of a summary sentence and acceptance criteria without the analytical foundation are incomplete. The standard is: *an agent starting the task cold should be able to proceed to implementation without repeating the diagnostic investigation.*

## 8. Testing and telemetry rules

1. Real call-path tests matter more than unit-shaped assumptions alone.
2. When changing MCP tools or handlers, test through the real gateway path (`InternalMCPGateway.invoke()`) and not only the handler function, especially for error paths and output-schema validation.
3. Telemetry correctness is an operational requirement, not optional polish.
4. Diagnostic payloads should preserve safe machine-readable counters such as token counts and durations.
5. When stage context changes, record context lineage or equivalent telemetry so later diagnosis can distinguish shared turn context, stage-local additions, and final effective prompt shape.
6. When a workflow or tool path produces the answer, verify the user-visible output remains answer-first rather than execution-bookkeeping-first.
7. When multiple tests fail, check whether the tests encode a stale design assumption before forcing the code to fit them.
8. Use targeted impacted pytest execution by default; widen only when risk or failures warrant it.
9. Never run backend tests against `VON_DB_NAME=von_db`. Use the test DB.
10. Before every commit, run the lint/type-check gate and fix outstanding diagnostics.
11. After any significant architectural change — replacing a heuristic with an authority surface, decomposing a monolith, or removing code-side semantic steering — perform a bounded anti-pattern test audit in the affected area. Scan for tests that (a) directly assert on outputs of the removed or replaced heuristic, (b) autostub the new authority-backed path to return empty/fixed values so the real architecture is never exercised, or (c) pin downstream effects of heuristic code through end-to-end assertions. Remove or rewrite such tests, then remove any code that existed only to satisfy them. Tests that validate anti-patterns are load-bearing obstacles to architectural progress; leaving them in place causes the old code to persist indefinitely because developers fear breaking the test suite.

## 9. Tooling defaults

- Prefer Von's internal Jira pathways when Atlassian MCP OAuth is unreliable.
- Use the Atlassian recovery runbook rather than inventing Jira REST workarounds.
- When the user asks for "recently closed" `JVNAUTOSCI` issues, interpret that by default as `project = JVNAUTOSCI AND statusCategory = Done AND resolved >= -48h ORDER BY resolved DESC` unless they explicitly ask for a narrower terminal status such as `Closed`.
- Use workflow MCP tools as the default control surface for workflow behaviour.
- Use host-neutral, PowerShell-first, bounded shell commands unless the environment clearly requires otherwise.
- When adding credential or service-critical environment variables, register them in `_apply_dotenv_overrides()` (`src/workflows/von/main.py`), verify `.env` provides them, and emit clear resolution diagnostics.
- Where tool friction is discovered, improve Von's own tooling path and document the gap.

## 10. Guidance maintenance

If something went wrong in a coding thread, consider whether:

- a shared helper should exist
- a validator or telemetry surface is missing
- shared turn-context construction or context-lineage telemetry is missing
- a workflow/Vontology primitive is missing
- a large or tangled function should be decomposed into clearer support surfaces
- hidden coordination or decision policy should move from Python into workflow/Vontology authority
- execution bookkeeping is leaking into the user-facing answer channel
- a benchmark or acceptance path is inadequate
- `AGENTS.md` or one of the situation-specific docs should be sharpened
- `docs/engineering/operational_engineering_guide.md` should absorb durable practical engineering lessons that do not belong in `AGENTS.md`

Promote stable lessons. Do not bloat this file with narrow or temporary observations.

## 11. Bottom line

Von is not mainly a Python application with some prompts attached.

It is a neuro-symbolic agentic system in which:

- enduring knowledge matters
- workflow and prompt authority matter
- model portfolios and learned policy matter
- explicit representation matters
- evaluation and observability matter
- and Python exists to support those things rather than replace them
