# AI Agent Guide (Trimmed)

This document provides key instructions for AI agents working on the Von-Private project.
All AI agents must read this file and `docs/engineering/security_considerations.md` before starting any work.

## TL;DR for Agents
1. Use New Zealand English spelling always (behaviour, colour, organisation, realise). This can include careful use of Māori words.
2. PowerShell is the default shell. Do not emit Bash heredocs, `export`, `$(cmd)`, or `source venv/bin/activate` unless explicitly asked for Bash.
3. Never clobber `.env`. Only touch it if explicitly asked; never print its contents or secrets.
4. It's fine to auto-start the server; don't wait for explicit user instruction. (Still obey system/developer constraints if stricter.)
5. Use MCP tools (Vontology/Jira/Mongo) by default; for Jira, prefer Von's internal Jira MCP/proxy tools (`jira_*` via InternalMCPGateway) over Atlassian MCP OAuth. Explain if you must use another path.
6. Do not use direct DB access methods for Vontology data; use Vontology routes/services (API/MCP) instead.
7. **Vontology is THE source of truth** for all persistent knowledge and data. Exceptions (e.g., ephemeral caches, session state) must be rare and explicitly justified.
8. Jira issues must be assigned on creation (assignee = current user unless told otherwise).
9. Keep changes minimal, well-scoped, and add/update tests and docs where relevant.
10. Prefer small, composable functions; avoid monolithic helpers.
11. Always check VS Code Problems panel (or run `get_errors`) after edits and when errors are reported. If the Problems panel is not available, run `pyright` as a proxy.
12. **Pre-commit lint gate**: Before every `git commit`, run `get_errors` (or `pyright`) and fix all outstanding linting/type-checking errors. Do not commit with known unresolved diagnostics. If using `pyright`, include **every changed Python file** from `git diff --name-only` (do not hand-pick a partial subset).
13. Enable pre-commit guardrails: `git config core.hooksPath .githooks`.
14. **STOP**: Never run backend tests against `VON_DB_NAME=von_db`. Always use the test DB (`VON_DB_NAME=test_von_db`) or the `pytest:backend (test db)` task.
15. Requiring a user choice is almost always dispreferred; prefer LLM reasoning to achieve reliability and only ask the user when ambiguity cannot be resolved safely.
16. When in doubt, run tests or re-run tests without requiring user confirmation, but start with a **targeted impacted set** (touched modules + real call-path tests) and only expand to broader suites when risk or failures indicate.
16A. **Pytest execution policy**: use `pdm run python scripts/pytest_lanes.py recommend --git-diff origin/main` to plan targeted impacted validation, then run the direct targets and relevant deterministic lane(s). Treat targeted impacted testing as the default interactive validation level, and use `pdm run python scripts/pytest_lanes.py aggregate-plan` plus individual lane runs for broader coverage. Do not claim that a full pytest run was performed unless the relevant aggregate lanes were actually executed. See `docs/engineering/pytest_lane_strategy.md`.
16B. **Pytest timeout-avoidance policy**: default to small sequential pytest batches in interactive runs. Start with one file per invocation for slower or integration-heavy areas (for example orchestrator, workflow selector, RAG, DB-backed, or end-to-end call-path tests), and only combine files after the individual files have passed quickly. If a batch is slow, silent, or times out, immediately split by file, then by class/test or `-k` selection; do not keep retrying the same broad command.
17. In the case that multiple tests are failing, carefully consider the possibility that the tests are based on a design assumption that no longer holds. Tests are not definitional here, they are diagnostic, and should be changed (carefully) if they are not diagnostic for the current design. Do not allow tests to be a barrier to generality and good factoring.
18. Before closing a task, run the task-closure pass: re-read the task and its epic/subtask context, review nearby recent work, run targeted regression checks, and for substantial tasks complete the reflection pass.
18A. **Older Jira tasks require sceptical review**: task wording may be stale relative to the current codebase and evolving Von design. At task start and task closure, decide whether the task should be executed literally, reinterpreted as a general capability request, narrowed, superseded, or closed as obsolete, and update Jira/linked issues accordingly.
18B. **Literature-informed planning is expected when useful**: for research-sensitive, architecture-shaping, or long-horizon-agent tasks that could materially benefit from recent literature, do a short targeted literature review before implementation. Use it to refine or augment the Jira task/epic plan, prefer durable capability improvements over literal stale prescriptions, and record any material reinterpretation in Jira.
19. **Workflow-first**: express behaviour in VWL/Vontology when possible; do not encode workflow definitions, routing, or task-specific orchestration in Python when reusable workflow/runtime surfaces can carry the behaviour. If VWL is insufficient, add only the missing reusable execution/validation/telemetry/persistence surface and document the capability gap in Jira/Vontology.
20. **DRY first**: Create central helpers FIRST, then replace all usages. See "DRY refactoring discipline" in Workflow section.
21. **Comment for evolution**: Add comments that guide future modifications. See "Self-Documenting, Evolvable Code" section.
22. **Proactive hygiene**: Periodically review touched files and their neighbours for inconsistency, duplication, and drift. Fix proactively.
23. In docs/examples for secret env vars, use explicit placeholders like `<YOUR-CLIENT-SECRET-HERE>` and avoid token-like sample strings that can trigger secret scanners.
24. If you are reasonably confident task implementation is complete, proactively run the merge-and-close flow unless the user explicitly asks to hold.
25. **Definition of fully complete Jira task**: merged to `main`, Jira and linked issues updated, required Vontology workflow/state changes materialised, and for substantial tasks the reflection pass has produced any justified `AGENTS.md` or Jira guidance update.
26. **Fail closed when Vontology is unavailable for Vontology-governed behaviour**: do not silently fall back to in-code prompts, stale context reuse, or heuristic hacks. If required Vontology prompts/workflow data cannot be resolved, no-op that transformation and emit a clear user-visible and telemetry-visible reason.
27. **Minimal-imposition principle**: exhaust existing context/data/search first; ask humans only when necessary, and then only for concise, low-effort, high-value inputs they are likely to know without extra work.
27A. **Minimal-imposition mutation policy**: low-risk additive Vontology writes should default-allow when evidence-backed inputs make the intended additive action clear and the user has not explicitly denied it.
27B. **Canonical identifiers/URLs can be permission**: pasted canonical sources such as arXiv abstract URLs/IDs can constitute sufficient permission for low-risk additive representation work; do not require redundant verbs like `download`, `store`, or `represent`.
27C. **Destructive actions require workflow confirmation**: deletes, removals, and other destructive mutations should branch to explicit workflow confirmation/escalation rather than being executed immediately.
27D. **Human in the loop is not the default doctrine**: outside genuinely safety-critical cases, do not interrupt users or route to human approval without evidence that it is necessary.
28. When the user asks whether something is "finished" or "complete", do not treat Jira status alone as the answer. Verify effective implementation state in both code and Vontology, then report whether it is unimplemented, partly implemented, or fully implemented (with concise evidence).
29. **`.env` is authoritative for credentials and service config**: any code that reads credential or service-critical environment variables (API tokens, secrets, proxy config) **must** register those keys in `_apply_dotenv_overrides()` (`src/workflows/von/main.py`). Never rely on inheriting credential env vars from the parent shell — VS Code, CI runners, and other host processes routinely override well-known keys (e.g. `GITHUB_TOKEN`) with their own values. When adding or modifying code that resolves an env var for auth or external service access: (a) add the key to the override set, (b) verify `.env` contains a value for it, and (c) add a diagnostic log at resolution time showing which key was used.
30. **Substantial-task reflection is mandatory**: before closing a substantial task, explicitly ask what reusable practices helped, what missteps occurred, and whether `AGENTS.md` or Jira epic/task comments should be updated.
31. **Promote stable lessons, not noise**: update `AGENTS.md` only with durable, future-useful guidance that strengthens or cleanly supersedes existing rules; otherwise use Jira epic/task comments. Prefer synthesis over growth.

## Core AI-Focused Documents
- `docs/AINotes.md`: short-term memory and tactical log.
- `docs/concept_refactoring.md` (or current plan doc): project roadmap for major work.
- `docs/software_engineering.md`: conventions, debugging, and lessons learned.
- `docs/engineering/security_considerations.md`: required security context.
- `docs/engineering/von_workflow_language_manual.md`: primary reference for workflow-related tasks; use it to guide workflow design/changes and update it whenever workflow semantics, capabilities, or constraints change.
- `docs/engineering/atlassian_mcp_recovery_runbook.md`: canonical Atlassian MCP recovery and credential reset procedure.
- `docs/engineering/jira_components_taxonomy.md`: canonical candidate Jira Components taxonomy for Von issues.

## Workflow and Quality
- Preserve raw user-authored text when it is rendered/transformed; store originals in dataset/raw attributes where applicable.
- Users may update this file; retain any user changes.
- Add a regression test for every state/format-loss bug (load -> edit -> save -> re-edit).
- **Test through the real call path**: When modifying MCP tool handlers, don't just test the handler function directly—also test through the gateway layer (`InternalMCPGateway.invoke()`). Early-return error paths may bypass output schema validation, and handlers may produce response shapes that don't match expected schemas. If a tool has an `output_schema`, verify error responses work end-to-end.
- Prefer lightweight telemetry where practical (timings, counters, and error summaries) to support UX and future introspection.
- **Telemetry correctness is mandatory**: diagnostic payloads, stage paths, counters, and progress summaries are operational interfaces, not optional polish. If they are internally inconsistent with the underlying event stream, treat that as a bug and fix it with the same urgency as a behavioural regression.
- When fixing reliability issues, prefer **systemic, general fixes** over point fixes: update shared pipelines, validators, and policies so that behaviour remains stable across model changes and configuration drift.
- **Substantial-task reflection pass (mandatory)**: after implementation and validation, but before merge/closure, review the work explicitly for generality and future leverage.
	1. Ask: "To the extent that I did a good job of producing high-quality code that improved the overall design and function of Von, what practices would be helpful in the future, and what missteps did I make; how should I update `AGENTS.md`, or Jira epic comments, to do better in future?"
	2. Identify which parts of the solution were truly reusable improvements (shared runtime surfaces, schemas, validators, telemetry, VWL/Vontology metadata, canonical helpers) and which parts risked case-specific drift.
	3. Prefer updating shared abstractions, schemas, and metadata surfaces over adding named special cases. If a proper noun from the originating task appears in logic, treat that as a design smell unless there is a strong reason.
	4. Promote stable, repo-wide lessons into `AGENTS.md`; put narrower or still-evolving lessons into the relevant Jira epic/task comments.
	5. Before editing `AGENTS.md`, ask whether the lesson is genuinely durable, likely to help on future tasks, and strong enough to clarify or cleanly supersede existing guidance.
	6. Prefer pruning, clarifying, reorganising, or synthesising nearby guidance over simply appending new text; when adjacent rules teach the same habit, replace them with a shorter, sharper rule when possible.
	7. If the reflection finds a recurring process/design gap, create or update a Jira issue instead of leaving the lesson as an unenforced observation.
	8. Treat this reflection as part of acceptance, not optional retrospective polish.
- For question-generation or elicitation flows, apply the minimal-imposition principle explicitly in prompts/fallbacks: avoid broad requests, and prefer one focused ask only when machine-side retrieval cannot close the gap.
- For write-policy work, treat minimal imposition as a risk-class decision rule: additive low-risk Vontology mutations should usually proceed, while destructive mutations should route to explicit confirmation workflows.
- **DRY refactoring discipline** (CRITICAL — WET code is unacceptable):
	1. **Search first, always**: Before writing ANY function that might exist elsewhere, run `grep_search` or `semantic_search`. This is not optional.
	2. **3-strike rule**: If you're about to write similar code for the 3rd time, STOP. Do not proceed. Create a central helper first.
	3. **Scope the problem first**: Before fixing ANY bug that appears in multiple places, grep/search to find ALL instances. Understand the full scope before touching code.
	4. **Create the helper FIRST**: Write a single, well-named helper function in a central location (e.g., `utils/`, `helpers/`, `domUtils.js`, `apiService.js`).
	5. **Then replace ALL usages**: In a single operation (multi_replace_string_in_file), replace every occurrence with calls to the helper.
	6. **Never fix instances one-by-one**: Piecemeal inline fixes create maintenance burden, review noise, and guarantee future bugs.
	7. **Central locations by domain**:
		- Frontend JS utilities: `static/js/utils/` or `domUtils.js`
		- API/fetch helpers: `apiService.js`
		- Backend Python utilities: `src/backend/utils/` or domain-specific service modules
		- Vontology operations: use existing service classes, never raw DB calls
	8. **If you catch yourself copy-pasting**: You are doing it wrong. Stop and refactor.
- Changes must be **modification-tolerant**: switching underlying models should not silently remove or change capabilities; differences must be explicit and detectable (telemetry, validation, or policy).
- **Vontology-first prompt pattern (strict)**: any new or modified LLM prompt should be stored as Vontology text relations (`hasContent`/`hasDescription`, primary `en-NZ`). Do not add silent code-prompt fallbacks for Vontology-governed features. If prompt resolution fails, fail closed for that feature and emit explicit diagnostics (including prompt IDs/reasons) so the failure is obvious and actionable.
- **No hack-around reliability policy**: when an ontology/workflow/prompt dependency fails, fix the dependency path (tools, retrieval, workflow config, validation) rather than patching symptoms with ad-hoc heuristics. Temporary mitigations must be explicit, bounded, and tracked in Jira with a removal plan.
- For high-risk state changes (auth/org handling, DB writes, Vontology mutations), use a single authoritative pathway and reuse it consistently.
- Strong rule: treat canonical predicate concepts (e.g. #V#is_a_type_of) as the authoritative ontology relations. Do not introduce or rely on structural relationship fields when predicate concepts exist; kind/classification should be derived from canonical predicate usage.
- **Predicate concepts must be instances of `#V#predicate` (or a specialisation such as `#V#binary_predicate`) and must not keep `is_a_type_of` links that would force kind=`type`.** This ensures `is_predicate()` and the computed kind behave correctly.
- Do a quick factoring review whenever you touch write paths: search for existing canonical helpers/endpoints (especially for deletes/merges) and route through them rather than adding parallel pathways.
- Treat ontology/predicate investigation as a normal first step for Vontology work: resolve candidate concepts by name (including spelling variants like programme/program), search for existing predicates/types before inventing new ones, and record the findings (and chosen canonical IDs) in the related Jira issue.
- When code needs to manipulate real-world or conceptual entities (tasks, organisations, ideas, documents, relations, predicates, claims, assertions, rules, workflows, etc.), **always start by checking whether they already exist in Vontology**. Reuse/connect to existing representations; update them if needed; otherwise create new concepts rigorously.
- **Vontology representation guidance (from code behaviour):**
	- **Names:** Display names are resolved from `names[]` (NL then ABBR, preferred language first). Create/update names via `hasName` text relations (primary NL in `en-NZ`), and include CODE names for the `concept_id` and GUID where applicable. Do not rely on legacy top-level `name` fields.
	- **Text preservation:** Keep text relation values exactly as-is unless a user explicitly asks to rename or normalise them. Do not auto-replace underscores with spaces (e.g., `has_url` -> `has url`).
	- **Descriptions:** Canonical descriptions are stored in `hasDescription` text relations. Avoid writing legacy `description` fields or `concept_data.preserved_fields` directly; use the concept/text relation services.
	- **Types vs individuals:** Types use `relationships.is_a_type_of` (parents). Individuals use `relationships.is_an_instance_of` (types). Computed kind is derived from these relationships, so avoid mixing them on the same concept.
	- **Type guidance predicates:** For types, prefer `#V#salient_binary_predicate_for_type` (or `salient_predicate_scopes.type_level`) to drive salient predicate prompts. These lists are consumed by salient predicate aggregation and relation elicitation. It is almost never appropriate for a type to be #V#is_a_type_of #V#thing (similarly, individuals should not be instances of thing). Search the ontology for suitable types to attach concepts to. Use the most restrictive applicable and appropriate supertypes.
	- **Suggested relations:** Use the meta-relations service (`suggested_relations_for_type`) for type-level suggestions when possible; legacy `relationships.suggested_relations_for_type` is read as a fallback.
- **Vontology-first checklist (mandatory for ontology-related work):**
	1. Resolve candidate concepts by name (MCP search/resolve).
	2. Check for existing predicate/type concepts before inventing anything.
	3. If a list of concepts is needed, prefer a Vontology type and query its instances.
	4. Record the chosen canonical IDs in the Jira issue.
- **Workflow-first behaviour checklist (mandatory for behaviour/orchestration changes):**
	1. Inspect existing workflow definitions before coding (`workflow_list_definitions`).
	2. Prefer creating/updating workflow instances and definitions in Vontology over adding task-specific Python orchestration (`workflow_create_instance`, `workflow_get_instance`, `workflow_list_instances`).
	2A. Treat any temptation to encode workflow branching, fallback sequencing, or route selection in Python as a design error unless VWL lacks the required primitive. In that case, add only the missing reusable primitive/control surface in code, document the capability gap in Jira, keep the workflow itself in Vontology, and attach the detailed capability-gap note to the workflow or affected step in Vontology.
	3. Prefer durable bindings/schedules for repeat behaviour (`workflow_bind_event`, `workflow_create_schedule`, `workflow_set_schedule_enabled`, `workflow_trigger_schedule`).
	4. Use operational controls instead of ad-hoc runtime flags (`workflow_cancel_instance`, `workflow_retry_instance`, `workflow_delete_schedule`).
	5. Treat workflow creation/editing in Vontology as normal implementation work. Do it during the task whenever the behaviour can be expressed there; do not stop at design prose if the workflow can be materialised.
	6. If Jira wording conflicts with this policy, treat the workflow-first policy as authoritative: reinterpret the Jira task, leave a note in Jira, and proceed with workflow definitions plus supporting tool work.
	7. If workflow tools still cannot express the requirement, explicitly document the constraint and create/link a Jira capability-gap issue before introducing specialised code.
- For fairly complex capability improvements, run a background search for related Jira issues, Confluence design docs, and existing Vontology concepts before proposing a plan.
- **No hard-coded ontology lists**: do not add fixed lists of predicate/type IDs or names in code. If you believe a hard-coded list is unavoidable, you must:
	- explain why Vontology lookup is not viable,
	- add a Jira note documenting the exception, and
	- include a removal/cleanup plan.
- When uncertain, ask succinctly; do not guess or fabricate behaviour.
- **Task lifecycle discipline**:
	- Before implementation, use or create an appropriate task branch and transition the Jira task to `In Progress`.
	- At task start, review the task age, epic context, and related older issues sceptically; if the wording is stale, reinterpret the task toward the current Von design and record that in Jira instead of following outdated prescriptions literally.
	- For research-sensitive or architecture-shaping tasks, consider whether a short targeted literature review would materially improve the plan; if so, do it before coding and update the Jira task/epic or subtasks when the literature changes the intended approach.
	- During substantial work, post concise Jira progress comments at meaningful milestones.
	- Before `Done`, complete the linked-issue review and ensure implementation branch work has been committed, pushed, and merged.
	- Before `Done`, review related older tasks again and update, comment, or transition any whose original intent has been absorbed, superseded, narrowed, or made obsolete by the completed work.
	- After merge, transition the task to `Done` and post a progress update on the parent issue.
	- If implementation appears complete and there is no explicit request to hold, execute the merge-and-close flow proactively.
- When commenting on a Jira task, say that the comment is generated by you (naming the agent) on behalf of the user.
- When asked to "merge", default to: ensure changes are committed, fast-forward merge into `main`, push `main`, then delete the local and remote feature branch; if deletion needs a force flag, ask for confirmation first.

## Tooling and Automation
- Activate required external tool categories (Jira, Vontology, MongoDB, GitHub) without asking, when clearly needed.
- Treat tool categories as opt-in per session: activate before first use, and occasionally check whether a matching tool deactivation call exists (to disable categories when no longer needed).
- **Vontology workflow tools are the default control surface for behaviour**:
	- Discovery/introspection: `workflow_list_definitions`, `workflow_list_event_bindings`, `workflow_list_instances`, `workflow_mcp_health_check`.
	- Execute behaviour: `workflow_create_instance`, then monitor via `workflow_get_instance`.
	- Event-driven behaviour: `workflow_bind_event` (avoid hard-coded dispatch tables where workflow binding can be used).
	- Scheduled behaviour: `workflow_create_schedule`, `workflow_list_schedules`, `workflow_get_schedule`, `workflow_trigger_schedule`, `workflow_set_schedule_enabled`, `workflow_delete_schedule`.
	- Runtime safety/recovery: `workflow_cancel_instance`, `workflow_retry_instance`.
	- Prefer these pathways over bespoke orchestration code whenever behaviour can be represented as workflow definitions + inputs.
- **Jira default access path**: it's OK to use the Atlassian MCP tools, but if they fail, switch immediately to using Von's internal Jira proxy/tools first  for the rest of the session (`jira_get_auth_config`, `jira_get_myself`, `jira_search`, `jira_get_issue`, write helpers in `src/backend/integrations/internal_mcp/catalogue.py`). This path uses `ATLASSIAN_BASE_URL` + `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN` and is independent of Atlassian MCP OAuth reliability.
- Use Atlassian MCP OAuth tools only when explicitly needed for capabilities not available in Von's internal Jira tools.
- When Jira friction is discovered, prefer improving Von's internal Jira path (tooling, diagnostics, schemas, guardrails) and document the issue/capability gap in code/docs/Jira so reliability improves over time.
- Where Vontology search, analysis or manipulation is impeded by the current vontology MCP tools, suggest code improvements to those tools that will facilitate high quality ontological engineering in future.
- Failures in Von's own MCP tools are bugs: report them as Jira bug work items with reproducible steps, observed error payloads, and related-issue links.
- If a tool category is not enabled, request enabling it by exact name.
- If Vontology or Vonrag MCP tools are not exposed in this session, use the stdio proxy scripts (`scripts/query_vontology_mcp.py`, `scripts/query_vonrag_mcp.py`) and check cached tool lists in `data/mcp_tool_cache/`.
- After implementing a fix and tests pass, post a Jira summary comment and transition the issue to the correct state.
- Do not emit JSON tool-call payloads as text; invoke tools directly.

### Vontology/Vonrag MCP Field Notes (2026-02)
- When searching Jira for possible duplicates, prefer `jira_search` with explicit `fields` (at minimum: summary, status, assignee, issuetype, parent). Default search responses can be id-only and create avoidable follow-up calls.
- Treat `jira_get_issue` 404 responses as potentially permission-related, not just non-existence. Cross-check with `jira_search` results before deciding to create a new issue.
- For canonical singleton text predicates (especially workflow `hasDescription` in one language), use `upsert_singleton_text_relation` with `garbage_collect=true` rather than repeated `upsert_text_relation`.
- After creating or updating workflow concepts, run `concept_exists` and `get_text_relations_summary` as a quick integrity check.
- Use Vonrag `search_knowledge_base` for semantic recall, but do not rely on it for deterministic structured failure triage. When turn-level structured analysis is needed, propose/add dedicated tools and document the gap in Jira.

## Atlassian MCP Reliability (Pause + Checkpoint)
If Atlassian MCP is flaky (timeouts, empty responses, 401/403/5xx):
1. Stop further Jira writes/edits beyond bounded retries.
2. Checkpoint: what issue(s), what succeeded, what remains.
3. Ask the user to restart MCP using the steps below.
4. Resume from the checkpoint after a minimal health check.
5. Prefer lightweight recovery first (`Developer: Reload Window` + Atlassian MCP restart). In recent incidents this has often recovered auth without credential reset.

If restart/reload does not recover Atlassian MCP (especially recurring `invalid_token` / `Canceled` token fetch loops), follow:

- `docs/engineering/atlassian_mcp_recovery_runbook.md`
- This runbook includes the no-sign-out recovery path (safe backup + targeted VS Code state DB key reset).
- Prefer the helper script first: `scripts/powershell/reset_atlassian_mcp_auth.ps1`

Restart steps (VS Code):
- Command Palette -> `MCP: Browse MCP Servers` -> Atlassian -> Restart.
- If Restart is not available: `Extensions: Focus on MCP Servers - Installed View` -> Atlassian -> Restart/Stop/Start.
- If still failing: `Developer: Reload Window`.
- If it continues to fail: `Atlassian: Open Settings` -> sign out, then sign in again.

Do not "hack around" MCP failures with ad-hoc scripts or direct REST calls. Fix the MCP session instead.

## Jira Essentials
- Tasks can have Subtasks (use `issueTypeName="Subtask"` + `parent="JVNAUTOSCI-XXX"`).
- Tasks can set an Epic as `parent` via edit tooling (`{"parent": {"key": "JVNAUTOSCI-123"}}`).
- If an issue is created without an assignee, fix it via the Jira edit tool rather than duplicating.
- **Deduplicate before create**: when a user asks to create a Jira task, first search for an existing issue with the same intended effect. Prefer updating/expanding the existing issue (and linking/commenting for traceability) instead of creating a new one. Create a new issue only if no suitable existing issue exists, or if the user explicitly asks for a separate task.
- When creating or substantially rewriting a Jira task, include a concise implementation-design note by default (scope, key behaviour, edge cases, acceptance checks). For UI/UX tasks, include placement/layout, visibility rules, accessibility, motion, and responsive expectations. For substantial tasks, explicitly include a final acceptance pass requiring post-implementation reflection on reusable practices, missteps/case-specific temptations, whether `AGENTS.md` should be updated, whether any update is truly durable and future-useful, whether nearby guidance should be synthesised/pruned instead of merely extended, and whether the parent epic needs a process/design lesson comment.
- For full Jira Tasks (not sub-tasks), always identify the appropriate epic and attach them to it. In the very unlikely event that a suitable epic isn't available, plan what that epic would look like, and offer to create it.
- Add appropriate inter-task links (e.g. blocks/depends-on/relates) when the available MCP tooling supports it.
- **Link hygiene is mandatory**:
	- When creating a new Jira task, scan for existing related tasks first and add links (`relates to` / `blocks` / `is blocked by`) immediately.
	- Before starting implementation on an existing task, check whether relevant related tasks exist and add missing links before coding.
- **Completion linked-issue scan is mandatory for every task**:
	- Run a linked-issue review before merge while full code context is still available (validate linked issue statuses, add/update comments, and apply justified transitions).
	- Before marking a task Done, review all linked issues and identify any linked items still in `Backlog` / `To Do`.
	- Treat older linked issues sceptically: if the current design or completed work has changed their practical purpose, update them as reinterpreted, superseded, narrowed, or obsolete rather than leaving stale prescriptions open.
	- Add a concise status comment on each relevant linked item describing what changed, what remains, and whether it is now unblocked, superseded, or complete.
	- Apply transitions when justified by the completion outcome (for example `To Do` -> `Done` for fully satisfied scope, or `To Do` -> `SUPERSEDED` when absorbed by another task).
- If a screenshot is provided to describe an issue or support issue creation, attach it to the Jira issue whenever possible using available MCP tooling rather than leaving it only in chat context.
- **Pasted screenshot handling (mandatory default path)**:
	- Use `list_recent_screenshots` first with `match_clipboard=true` and `include_base64=true` to locate the correct local image and get a Jira-ready payload.
	- If clipboard image data is unavailable, select the most likely recent screenshot candidate, attach it with `jira_add_attachment`, and state in the Jira comment that selection was based on recency/path heuristics.
	- Prefer this flow over asking the user for a manual file path; ask only if no credible candidate is found.
- When setting Jira `Components`, use `docs/engineering/jira_components_taxonomy.md` as the default source of truth unless the user requests otherwise.
- Jira site URL: https://naoinstitute.atlassian.net/
- If a cloudId is required, fetch it from https://naoinstitute.atlassian.net/_edge/tenant_info and include that URL when requesting it.

## PowerShell-First Shell Rules
- Avoid Bash-only syntax: heredocs, `export`, `$(cmd)`, `source venv/bin/activate`.
- Use `$env:VAR = 'value'`, `$var = (cmd)`, and PowerShell here-strings.
- Prefer separate lines over `&&` unless failure short-circuit is required.
- Large multi-line Python: use a here-string variable and `pdm run python -c $code`, or add a script.

## Command Hang Guardrails
- Treat simple read commands (`Get-Content -TotalCount`, `rg`, `git status`, `git branch`) as expected to complete quickly; if a run exceeds ~30s with no new output, treat it as potentially hung.
- Prefer bounded reads and output caps (`-TotalCount`, `Select-Object -First`, targeted `rg`) instead of broad scans.
- Avoid launching multiple long `Get-Content` calls in parallel; run file reads in smaller batches.
- Treat `pytest` as a timeout-sensitive command in agent sessions: prefer explicit timeouts and narrow batches, especially for quiet or historically slow suites, and shrink the batch immediately after any stalled run rather than rerunning the same wide selection.
- When diagnosing shell delay, run a quick sanity probe first:
  - `pwsh -NoProfile -Command "Get-Content <path> -TotalCount 5"`
  - If fast manually but slow in agent tooling, assume tooling/transport stall and restart the VS Code window.
- Use explicit command timeouts for agent-run shell calls, and retry with a minimal command before escalating.

## IDE Host Introspection (Codex/Copilot)
- **Introspect execution host first** where possible (Codex extension, Copilot agent/chat, CLI, or unknown) before applying host-specific assumptions.
- If host introspection is available, record the detected host in diagnostics/comments for debugging reproducibility.
- Use an allowlist for host markers (e.g., `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`, `VSCODE_*`, process chain), and avoid dumping full environment variables.
- If host cannot be determined reliably, default to conservative behaviour:
  - use PowerShell-first commands,
  - bounded output,
  - explicit timeouts,
  - restart/retry guidance framed in host-neutral terms (e.g., `Developer: Reload Window`).
- Do not assume UI labels (e.g., `bash` output block labels) reflect the true underlying shell process.

## UI Debugging (Missing/Invisible Elements)
- First check the DOM element exists; then check CSS visibility (display/visibility/opacity), size, positioning, z-index, and overflow.
- Absolute-positioned elements need a relative parent. Off-screen transforms and `overflow: hidden` are common culprits.
- If the element exists but looks wrong, capture `outerHTML` and key computed styles before changing JS.

## Copilot Tool Selection Hygiene (when running in Copilot)
- Authoritative script: `scripts/set_vscode_copilot_selected_tools.ps1`.
- If you rely on a tool, ensure it stays enabled by updating that script.
- When not running in Copilot (for example, Codex extension), do not assume Copilot tool-selection state is active.

## Reporting IDE Agent Issues
When encountering agent-host bugs or limitations, first include host identification in the report (Codex extension vs Copilot vs other). Then use the host-appropriate path:
- Copilot issues: https://github.com/microsoft/vscode-copilot-release/issues
- Codex extension issues: use the current OpenAI/Codex feedback channel configured for this workspace/org.

Copilot issue template:

```
- Copilot Chat Extension Version: (run `code-insiders --list-extensions --show-versions | Select-String copilot`)
- VS Code Version: (run `code-insiders --version`)
- OS Version: Windows
- Feature (e.g. agent/edit/ask mode):
- Selected model (e.g. GPT 4.1, Claude Opus 4.5):
- Logs: (if applicable)

Steps to Reproduce:
1.
2.

Expected Behaviour:

Actual Behaviour:

Impact:

Suggested Fix:
```

This keeps overhead low while providing actionable reports.

## Research Prototype Engineering Philosophy
- Build research-appropriate quality: modular, extensible, and understandable.
- Avoid enterprise-scale over-engineering; keep abstractions just deep enough for near-term change.

## Self-Documenting, Evolvable Code
Code comments are not just explanations—they are **guidance for future modification**. Use comments strategically to steer the codebase toward global consistency and safe evolution.

### When to Add Guiding Comments
1. **Design rationale**: When a choice isn't obvious, explain *why* (not just *what*). Future agents and developers will otherwise repeat the same mistakes or undo intentional decisions.
2. **Cross-cutting conventions**: When a file or module establishes patterns that other code should follow, document those patterns prominently (module docstrings, section headers). Example: schema design guidelines in `catalogue.py`.
3. **Footgun warnings**: When code has non-obvious failure modes (e.g., strict validation rejecting LLM-hallucinated fields), add a comment explaining the risk and the mitigation pattern.
4. **Consistency requirements**: When a parameter, field, or pattern must be included for global consistency even if unused locally, document why. Example: `# Accept namespace for LLM consistency (ignored by handler)`.
5. **Evolution hooks**: When you anticipate future changes, leave breadcrumbs: `# TODO: when X is implemented, update Y` or `# Future: consider Z for better performance`.

### Comment Style Guidelines
- **Be actionable**: "Include `namespace` in all user-facing tool schemas" is better than "namespace is important".
- **Reference issues**: Link to Jira tickets (e.g., `See JVNAUTOSCI-1044`) so readers can find full context.
- **Prefer module/class docstrings** for conventions that affect entire files; use inline comments for localised warnings.
- **Keep comments current**: When changing code, update or remove stale comments. Wrong comments are worse than no comments.

### Examples of Good Guiding Comments
```python
# Module docstring establishing conventions:
"""
SCHEMA DESIGN GUIDELINES
========================
1. Include `namespace` as optional on user-facing tools (LLMs generalise patterns).
2. Prefer allow_unknown=True for inputs; strict validation rejects hallucinated fields.
See JVNAUTOSCI-1044 for the failure mode this prevents.
"""

# Inline comment explaining non-obvious inclusion:
optional={
    "namespace": (str, type(None)),  # Accepted but ignored; LLM consistency
}

# Warning comment for future maintainers:
# WARNING: Do not remove this fallback—external callers depend on the legacy format.
```

### Anti-Patterns
- **Commenting *what* without *why***: `# increment counter` adds nothing; `# increment to track retry attempts for rate-limit backoff` adds value.
- **Orphaned TODOs**: If you add a TODO, also add context (who should do it, when, why).
- **Defensive silence**: Not commenting tricky code because "it's obvious" guarantees future breakage.

## Agent-Friendly Engineering Practices
These practices make codebases more navigable and consistent for AI agents, who lack tribal knowledge and rely on explicit signals.

### Canonical Reference Implementations
When establishing a pattern, create ONE exemplary instance and comment it as the reference. Agents learn by example more reliably than by rule.
```python
# REFERENCE IMPLEMENTATION for tool input schemas.
# Copy this pattern for new tools. See JVNAUTOSCI-1044.
def _example_tool_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "namespace": (str, type(None)),  # Accept for LLM consistency
        },
        allow_unknown=True,  # Tolerate hallucinated fields
    )
```
Then elsewhere: `# See _example_tool_input_schema for the canonical pattern.`

### Fail-Fast with Diagnostic Errors
Error messages must include **what was provided** and **what was expected**. Agents iterating on fixes need both.
```python
# Bad:
raise ValueError("Invalid field")

# Good:
raise ValueError(f"Unexpected field '{field}'. Valid fields: {list(schema.keys())}")
```

### Schema-First Interfaces
Define types/schemas/contracts **before** implementing behaviour. Agents reason better about explicit contracts than emergent behaviour.
- Use `Schema` definitions for MCP tools
- Use TypedDict/dataclass for internal interfaces
- Document return shapes in docstrings when formal schemas aren't practical

### Structured Telemetry and Introspection
Embed diagnostic payloads throughout the system (like the `aux_llm_calls` JSON). When something fails, the debugging story should be readable from structured output.
- Include `stage`, `type`, `error`, and `context` fields in diagnostic records
- Prefer structured dicts over free-form log strings
- Make introspection endpoints (like `chat_introspect`) available for debugging
- If route selection, workflow membership, stage mapping, or tool accounting disagree with each other, the telemetry is wrong; either reconcile the fields against one authoritative event stream or omit the derived field and emit an explicit reason.

### Convention Over Configuration
Reduce the number of valid ways to accomplish a task. Fewer choices = fewer agent mistakes.
- Establish one canonical path for common operations (e.g., one way to add a relationship)
- When multiple approaches exist, deprecate the worse ones explicitly
- Document "the one right way" in module docstrings

### Proactive Consistency Reviews
Periodically scan touched files and their neighbours for inconsistency, duplication, and drift—then fix proactively.
- When editing a file, skim related files for parallel patterns that should match
- After completing a task, do a quick grep for similar code that might need the same fix
- Treat documentation (like this file) as code: review it for internal consistency when editing
- Flag accumulated debt in Jira rather than ignoring it
