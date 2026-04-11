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
9. Decision-policy authority extends beyond routing. Ranking, recommendation, matching, classification, explanation, retrieval strategy, and planning count as authored behaviour.
10. Python should usually provide reusable support surfaces: execution, validation, tool wrappers, rendering, telemetry, persistence, safety checks, integrations, and genuinely missing reusable primitives.
11. Do not place Vontology-governed prompt bodies in Python. Prompts are operational policy and should live in Vontology text relations.
12. If a required Vontology/workflow/prompt authority surface is unavailable, fail closed for that feature. Do not silently fall back to stale prompts or heuristic hacks.
13. Do not relax workflow- or KB-authority requirements to justify a preferred implementation. Any genuine exception requires explicit human approval and a named missing reusable primitive or authority surface.
14. Heuristic fallbacks must be explicitly temporary, non-authoritative, linked to a removal task, and easy to delete.
15. Destructive mutations require explicit confirmation or workflow escalation; do not infer permission for deletes or removals from general task context.
16. Default to minimal imposition. Exhaust machine-side retrieval, context, search, and reasoning before asking the user to do extra work.
17. Treat older Jira wording sceptically. Reinterpret stale tasks toward the current Von architecture and note the reinterpretation in Jira.
18. For research-sensitive, architecture-shaping, or long-horizon-agent tasks, do a short targeted literature review before finalising the plan.
19. Run targeted impacted validation by default, including real call-path tests where relevant. Do not claim broader coverage than you actually ran.
20. End-to-end or user-visible acceptance requires direct evidence on the exact path or the nearest real path, not only nearby unit tests.
21. Substantial-task reflection is mandatory. Extract durable lessons, update docs when warranted, and create Jira tasks for real process gaps.
22. Prefer durable capability improvements over case-specific patches. If a proper noun from the triggering task appears in core logic, treat that as a design smell unless there is a strong reason.

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
- lexical scoring tables for durable recommendation or ranking policy
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
- Python remains a support surface rather than hidden task policy
- any heuristic fallback is explicitly temporary and non-authoritative
- repo-side seeds or snapshots could be deleted without losing authority

## 5. Prompt and model rules

1. Treat prompts as versioned, inspectable policy artefacts.
2. When prompt behaviour changes, inspect the authoritative prompt and rendered context before doing code-first diagnosis.
3. Prefer prompt revision, retrieval/context improvement, validators, or model routing over lexical pseudo-NLP.
4. When model choice matters, think in terms of a model portfolio: small local models, medium models, frontier models, fine-tunes, and symbolic modules.
5. Keep model-specific quirks out of durable business logic whenever possible.
6. Make model differences visible through telemetry, evaluation, and policy metadata.

## 6. Vontology and representation rules

1. Start Vontology-related work by resolving candidate concepts and existing predicates/types before inventing anything new.
2. Reuse existing Vontology concepts when possible; extend them rigorously when necessary.
3. Prefer Vontology types and queries over fixed concept lists in code.
4. Preserve user-authored text exactly unless the user explicitly asked to rename or normalise it.
5. Use canonical ontology predicates rather than ad-hoc structural relationship fields when predicate concepts exist.
6. Keep types and individuals cleanly separated.
7. Record chosen canonical concept IDs in Jira for ontology-shaping work.

## 7. Task lifecycle discipline

### 7.1 At task start

- create or switch to the task branch
- transition the Jira issue to `In Progress`
- when creating Jira issues on the user's behalf, assign them to the authenticated Jira user by default unless the user explicitly asks for a different assignee or Jira refuses the assignment
- review task age, linked issues, and likely staleness
- identify the authoritative KB/workflow/prompt artefacts
- decide which situation-specific docs are mandatory for this task

### 7.2 During work

- post concise Jira progress comments at meaningful milestones
- use existing canonical helpers and pathways before adding new ones
- run targeted tests as you go
- keep changes minimal but systemic where a shared fix is clearly better than a point fix

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
- complete the reflection pass

### 7.4 Jira task design quality

Jira tasks are durable artefacts that other agents and humans rely on months later. A task that omits the analysis behind it forces the next implementer to redo the entire diagnosis. Every implementation or bug-fix task must include:

1. **Observed failure evidence.** Concrete data: request IDs, telemetry field values, error messages, or user-visible symptoms. Quote the actual values — do not paraphrase.
2. **Code path analysis with file and line references.** Trace the execution path through the relevant functions, naming each file, function, and approximate line number. The reader should be able to follow the path without searching.
3. **Competing hypotheses with diagnosis steps.** When root cause is uncertain, state each plausible hypothesis explicitly and describe the concrete steps (log inspection, breakpoint, test case) that would confirm or eliminate it. Do not present a single guess as established fact.
4. **Fix approach per hypothesis.** For each hypothesis, describe the intended code change — which function, what logic, why it resolves the root cause. If hypotheses share a fix, say so.
5. **Regression test requirements.** Name the specific assertions the fix must be tested against (not generic "add tests"). State what conditions the test must reproduce and what the expected vs. failing outcome is.
6. **Relationship to other tasks.** Link related issues and explain the relationship (shared root cause, same pipeline stage, discovered together, one blocks another). A link without explanation is insufficient.
7. **Key file references.** List the primary files the implementer will need to read, with the relevant function or section name.

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
5. When multiple tests fail, check whether the tests encode a stale design assumption before forcing the code to fit them.
6. Use targeted impacted pytest execution by default; widen only when risk or failures warrant it.
7. Never run backend tests against `VON_DB_NAME=von_db`. Use the test DB.
8. Before every commit, run the lint/type-check gate and fix outstanding diagnostics.

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
- a workflow/Vontology primitive is missing
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
