# Proactive Refactor and Authority-Alignment Scan

This note explains how to look for structural improvement opportunities after substantial implementation work, especially workflow- or orchestration-adjacent work.

The goal is not to create a backlog full of vague "this file is large" tickets.
The goal is to notice when the current code shape increases the risk that:

- future fixes will keep landing as local patches inside monoliths
- workflow control or verification will drift into Python
- stage-specific context shaping will silently change what different LLM phases know
- user-visible meaning will be silently distorted by support layers
- workflow bookkeeping or diagnostics will leak into the answer channel
- or support surfaces will become too tangled to evolve safely

## 1. When to do a scan

Do a bounded structural scan when one or more of the following is true:

- you have just completed a substantial workflow, orchestration, routing, or telemetry refactor
- the task required multiple local helper extractions or cleanup of duplicated logic
- pyright, tests, or review friction revealed unclear responsibility boundaries
- a large function attracted another local patch because there was no better seam
- a user-visible issue turned out to be caused by a support-layer monolith losing meaning between stages

This is especially important after tasks that change:

- workflow selection
- workflow dispatch
- launchability or validation pathways
- telemetry or diagnostics shaping
- display or rendering adapters
- route boundaries that coordinate orchestrator behaviour

## 2. What to look for

Size matters, but size alone is not enough.

The strongest refactor candidates usually combine several of the following:

- `Mixed responsibilities`
  - one function handles boundary mechanics, context construction, coordination, validation, telemetry, and response shaping all together
- `Repeated patch pressure`
  - multiple recent fixes landed in the same large function or file
- `Hidden policy drift`
  - Python is beginning to own workflow control, verification criteria, ranking logic, display-selection logic, or stage-specific semantic context selection that should live in represented authority surfaces
- `User-visible interpretation risk`
  - one support-layer function can silently change what the user sees, what a stage knows, or how workflow reasoning is explained
- `Poor test seams`
  - the only way to test behaviour is through one huge function with many nested helpers or conditionals
- `Integration-boundary sprawl`
  - a registry, factory, route, or service has become the dumping ground for many unrelated capability families
- `Duplication or stranded helpers`
  - helper functions exist in multiple places, or a useful seam still lives in local scope inside a monolith

## 3. What not to do

Do not open a refactor task solely because:

- a function is long
- a file is large
- the code feels old
- the scan can only say "clean this up" without a sharper architectural reason

Do not propose refactors that merely rearrange hidden policy inside Python.

For workflow/orchestration work in particular, a refactor is not successful if it makes Python the real home of:

- workflow control
- workflow verification
- decision policy
- stage-specific semantic context choice
- display-selection policy

Those should remain authored in Vontology/workflow/prompt/programme artefacts wherever the architecture intends them to live, with Python acting as a support surface.

## 4. How to triage a candidate

For each candidate hotspot, ask:

1. What is the actual risk?
   - Is this mainly a readability problem, or is it a change-safety / authority-alignment problem?
2. What kind of function is this?
   - route boundary
   - orchestrator support surface
   - workflow engine surface
   - diagnostics/telemetry adapter
   - integration registry/factory
   - UI/display support layer
3. Is the function only big, or is it also mixing concerns that should be separated?
4. Is Python starting to absorb logic that should instead be represented?
5. Can I describe a better support-surface decomposition?
6. Can I name the validation that would prove the refactor preserved behaviour?

If you cannot answer those questions clearly, do not create the task yet.

## 5. What a good refactor task should say

A strong task should include:

- the observed structural problem
- why it matters now, in light of recent work
- the exact file/function/line area involved
- the architectural risk
- the intended decomposition or support-surface improvement
- the explicit authority constraint
  - for example: "do not migrate workflow control or verification into Python"
- the regression tests or validation that should protect the refactor

The description should make clear what `done` means architecturally, not only mechanically.

Good examples of that:

- "make this route a thinner HTTP boundary"
- "split discovery/selector/dispatch diagnostics into dedicated builders"
- "extract validation, approval, idempotency, and transition support surfaces from the engine loop"
- "modularise registry assembly without changing authority semantics"

Weaker examples:

- "clean up this large file"
- "refactor for readability"
- "split into smaller functions"

## 6. Special rule for workflow/orchestration code

When scanning workflow/orchestration code, actively ask:

- should this coordination logic remain in Python at all?
- is this actually authored workflow policy hiding in code?
- are verification criteria being implemented in Python instead of represented artefacts?
- has one support layer silently replaced the shared turn context with a thinner phase-specific context?
- is workflow bookkeeping or completion narration able to replace the answer channel?
- could a clearer support seam let workflows, prompts, or Vontology own more of the behaviour?

The preferred direction is:

- represented artefacts own control and verification intent
- flexible LLM reasoning interprets those artefacts where appropriate
- Python provides execution, validation scaffolding, telemetry, persistence, rendering, and testable support seams

## 7. Typical hotspot categories

These are common scan outcomes:

- `Route-boundary monoliths`
  - too much request parsing, context derivation, orchestration, and response shaping in one HTTP handler
- `Orchestrator monoliths`
  - helper sprawl around selection, dispatch, override, and narration/routing logic
- `Context-shaping monoliths`
  - support code quietly builds different effective contexts for selector, planner, tool, and response stages without explicit authority or telemetry
- `Diagnostics adapters`
  - large functions that convert internal telemetry into user-visible meaning
- `Workflow-engine loops`
  - engine execution paths mixing validation, approval, idempotency, trace emission, and transition control
- `Display/renderer support layers`
  - deterministic validation/building code starting to absorb presentation policy
- `Registry/factory monoliths`
  - catalogue or app-factory functions accumulating too many capability families

## 8. Suggested scan method

Use a bounded scan:

1. Find the largest functions in the touched or adjacent area.
2. Group them by role: boundary, orchestrator, engine, diagnostics, display, registry, service.
3. Read just enough to identify responsibility mixing and authority risk.
4. Open tasks only where the architectural case is clear.
5. Link related tasks so the refactor queue tells a coherent story.

Avoid full-repo archaeology unless the user explicitly asks for it.

## 9. Success criteria for the scan itself

A good scan produces:

- a small number of high-signal tasks
- each with a clear architectural case
- each with an explicit statement about authority boundaries
- and each with a plausible validation plan

A poor scan produces:

- many vague tickets
- line-count complaints without design reasoning
- or refactor proposals that would move more decision policy into Python

## 10. Bottom line

The point of these scans is to protect Von from slowly reverting into a Python-first system with prompts attached.

Use them to notice where the code shape is pushing in that direction, and to create the next structural tasks that keep workflow/Vontology authority, flexible LLM-led reasoning, and clear Python support surfaces aligned.

### 10.1 Mechanical guardrails

When the risk is specifically `support-surface semantic drift`, rely on
mechanical regression gates instead of memory or good intentions alone.

The workflow-purity report is not only for workflow publication drift. It is
also the place to add deterministic counters and contracts for cases such as:

- embedded prompt-like bodies in guarded orchestration support files
- code-side fallback markers that imply hidden prompt authority
- domain-specific literals in generic orchestration surfaces

At the time of writing, the guarded counters are:

- `python_authored_support_prompt_source_count`
- `support_surface_policy_contract_violation_count`

The rule is simple:

- if a support surface needs authored wording, move that wording into
  Vontology prompt text relations
- if a support surface needs domain knowledge, obtain it through workflow,
  prompt, KB, or tool metadata authority rather than literal Python branches
- if a new drift pattern appears more than once, add a deterministic purity
  counter or contract rather than trusting future agents to remember it

These guardrails should be extended incrementally to the highest-risk support
surfaces as they are cleaned up. They are not a proof that drift is impossible,
but they materially reduce the chance of silently reintroducing conventional
Python-first behaviour.

Keep these tripwires narrow and structurally precise. Prefer symbol-, AST-, or
contract-aware checks over raw keyword grep when the guarded file can
legitimately mention the retired concept in comments, docstrings, or
telemetry-facing prose. The goal is to catch semantic-policy drift, not to
punish explanatory text.

Current examples of this narrower style are:

- `orchestrator.py`: retired semantic-regex helper names and code-fallback
  markers
- `workflow_capability_service.py`: retired BM25/stopword/tokeniser artefacts
- `write_tool_policy.py`: any regex expansion beyond the bounded confirmation
  and destructive-confirmation backstops

### 10.2 Domain-name pull and stale-task drift

A recurring failure pattern is `domain-name pull`: a user-visible failure names
a concrete domain object, so the coding agent starts building a domain-specific
Python service even though the actual failure class is generic.

The risk is highest when all of these are true:

- the triggering prompt or Jira title contains a proper noun or domain label
- nearby Python already contains legacy domain tools, fallback metadata, or a
  large registry that makes another local addition look natural
- the observed failure is actually a generic authority-boundary gap, such as
  missing predicate validation, missing context propagation, or weak telemetry
- the task wording was drafted before the architecture diagnosis was complete

The countermeasure is to name the failure class before coding:

- `domain symptom`: what concrete case exposed the bug
- `generic failure class`: what reusable capability or authority boundary
  failed
- `authoritative surface`: the workflow, prompt, predicate, profile, or KB
  artefact that should own the policy
- `support-only Python scope`: the reusable primitive, validator, telemetry
  field, or tool wrapper that is legitimate to change

If the domain symptom is the only reason a new Python service exists, delete or
quarantine that service and rewrite the task notes before proceeding. A
domain-specific workflow may still be appropriate, but it should be authored as
workflow/Vontology/prompt policy and should reuse generic support primitives.

### 10.3 Warning comments at drift-prone seams

Warning comments can help, but only when they are placed at precise seams where
future agents are likely to mistake legacy support code for policy authority.
They should interrupt a known bad inference, not restate global doctrine in
every file.

Good candidates are:

- legacy fallback tables that still contain operational metadata because the
  represented authority surface is not fully populated yet
- MCP/catalogue/registry builders where a domain-specific tool near the edit
  site can make another domain-specific Python tool look normal
- monolithic orchestrator or diagnostics adapters where local branches have
  historically become user-visible policy
- compatibility layers that are necessary today but should not become the
  long-term authoring surface

A useful warning comment should say three things:

1. what the code is allowed to own, such as wiring, validation, telemetry, or
   compatibility defaults
2. what it must not own, such as domain modelling policy, routing semantics,
   user-facing recovery wording, or represented workflow control
3. where the authority should live instead, such as Vontology predicates,
   workflow definitions, prompt concepts, or tool metadata concepts

Do not scatter vague comments such as "avoid hacks" or "keep this clean".
Those become noise. Prefer comments that name the specific wrong move a future
agent is likely to make.

For example, a fallback tool-metadata table may legitimately expose a generic
planner hint while Vontology metadata catches up. It should not become the
place where a coding agent adds a new domain-specific policy because the last
failed turn happened to mention a paper, event, trip, or diary entry.

When a task has already drifted into Python policy and is later corrected,
leave a precise comment only if the same code seam is likely to attract the
same mistake again. Otherwise, record the lesson in task notes, Jira, or this
guidance document rather than decorating unrelated code.

## 11. Broader engineering and research context

This note is also motivated by a more general problem that appears to be emerging in current coding-agent practice.

In recent coding-agent output, very large functions, giant local regular-expression blocks, and defect-specific patching patterns are often signs that global design constraints are not being maintained well over time. These are not only style defects. They are warning signs for future maintainability, reliability, and architectural drift.

Our working hypothesis is that this problem is likely to be widespread, especially when agents are operating in codebases whose core design constraints do not closely resemble the dominant patterns in their training data.

Von is a good example of that difficulty:

- it is a large research system
- it relies heavily on large-language-model reasoning
- it treats knowledge-base and Vontology representation as a primary authority surface
- it uses a neurosymbolic combination that is still rare in mainstream software engineering and fairly rare even in the AI literature

Because architectures like this are uncommon, coding agents are more likely to fall back to conventional but inapplicable coding habits, especially in Python:

- local orchestration logic instead of represented workflow control
- heuristic patches instead of clearer support-surface extraction
- Python-side verification logic instead of represented verification criteria
- monolithic control functions instead of stable seams with explicit contracts

In other words, the model may continue to generate code that is locally plausible relative to common training examples, while being globally misaligned with the actual architecture and correctness conditions of the system under construction.

## 12. Monolith drift and \"one more local patch\" mode

One of the most important practical observations is that once functions become large and tangled, it becomes much harder to force effective and consistent refactors.

At that point, coding agents often stop initiating structural improvement on their own and instead settle into a pattern of:

- one more local fix
- one more conditional
- one more helper nested in local scope
- one more regex or one-off adapter

This pins the system in what we can call `one more local patch` mode.

That mode has several bad effects:

- it makes future refactors more expensive
- it increases the chance that hidden policy accumulates in code
- it weakens clarity about which artefacts are authoritative
- and it makes review increasingly local and tactical rather than architectural

The lesson is that proactive structural scans are not optional hygiene. They are one of the few practical tools for preventing a large agent-built system from getting trapped in cumulative monolith drift.

## 13. Why planning and tests help, but are not enough

Practices such as explicit planning and Jira-based task decomposition help materially. They create a place to record:

- recent evidence
- exact code paths
- architectural intent
- and the difference between support-surface code and represented authority

That helps agents avoid some classes of accidental drift.

However, planning alone is not a complete solution. A model can still produce locally coherent changes inside the wrong structural frame if nobody explicitly notices that the frame itself has become the problem.

Large test suites are similarly double-edged.

They help because they:

- catch many regressions
- preserve important behavioural expectations
- and make risky refactors more feasible

But they can also pin bad structure in place, because an agent may optimise for \"keep tests green\" while continuing to implement changes inside the same monolith or behind the same implicit policy boundary.

So the combination we want is:

- strong tests
- explicit design/task documentation
- and proactive structural scanning for authority drift and monolith formation

None of those is sufficient alone.

## 14. Working hypotheses about why this happens

These are not established conclusions, but they are useful working hypotheses.

1. `Conventional-code prior`

Current coding models appear to have a strong prior toward patterns common in large bodies of conventional human-written code, especially Python code. That prior is often useful, but it can also be actively harmful when the system being built has unusual architectural constraints.

2. `Weak maintenance of global constraints`

Agents can often follow explicit local instructions, but they are still relatively weak at preserving uncommon global design constraints over long implementation sequences, especially when those constraints cut against familiar implementation habits.

3. `Refactor initiation problem`

Even when a refactor is clearly needed, agents often do not initiate it autonomously unless prompted by explicit policy, documentation, or task structure. Without such guidance, they may continue patching locally because that is the shortest path to immediate success.

4. `Support-surface confusion`

When a codebase depends on represented authority, it is easy for an agent to confuse:

- support surfaces that should exist in code
- with behavioural policy that should remain outside code

That confusion is especially dangerous in orchestration, validation, ranking, routing, and user-visible explanation layers.

## 15. Implications for future work

The encouraging point is that these problems do not appear insurmountable.

Even with current levels of LLM reasoning, it seems plausible to do much better if we provide stronger machinery for:

- global consistency maintenance
- structural and architectural constraint tracking
- authority-boundary awareness
- explicit support-surface versus policy-surface distinction
- and proactive detection of monolith drift

This aligns closely with broader research on:

- global constraints in learning and reasoning
- inference under structural consistency conditions
- large knowledge systems
- and neurosymbolic methods that maintain explicit represented structure while still exploiting flexible statistical reasoning

For systems like Von, the ambition should not merely be to make coding agents produce code that looks conventional. It should be to make them extremely good at preserving global code quality, architectural intent, and represented-authority discipline over long horizons.
