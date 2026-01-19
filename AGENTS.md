# AI Agent Guide (Trimmed)

This document provides key instructions for AI agents working on the Von-Private project.
All AI agents must read this file and `docs/engineering/security_considerations.md` before starting any work.

## TL;DR for Agents
1. Use New Zealand English spelling always (behaviour, colour, organisation, realise). This can include careful use of Māori words.
2. PowerShell is the default shell. Do not emit Bash heredocs, `export`, `$(cmd)`, or `source venv/bin/activate` unless explicitly asked for Bash.
3. Never clobber `.env`. Only touch it if explicitly asked; never print its contents or secrets.
4. It's fine to auto-start the server; don't wait for explicit user instruction. (Still obey system/developer constraints if stricter.)
5. Use MCP tools (Vontology/Jira/Mongo) by default; explain if you must use another path.
6. Do not use direct DB access methods for Vontology data; use Vontology routes/services (API/MCP) instead.
7. JIRA issues must be assigned on creation (assignee = current user unless told otherwise).
8. Keep changes minimal, well-scoped, and add/update tests and docs where relevant.
9. Prefer small, composable functions; avoid monolithic helpers.
10. Always check VS Code Problems panel (or run `get_errors`) after edits and when errors are reported. If the Problems panel is not available, run `pyright` as a proxy.
11. Enable pre-commit guardrails: `git config core.hooksPath .githooks`.
12. **STOP**: Never run backend tests against `VON_DB_NAME=von_db`. Always use the test DB (`VON_DB_NAME=test_von_db`) or the `pytest:backend (test db)` task.
13. Requiring a user choice is almost always dispreferred; prefer LLM reasoning to achieve reliability and only ask the user when ambiguity cannot be resolved safely.

## Core AI-Focused Documents
- `docs/AINotes.md`: short-term memory and tactical log.
- `docs/concept_refactoring.md` (or current plan doc): project roadmap for major work.
- `docs/software_engineering.md`: conventions, debugging, and lessons learned.
- `docs/engineering/security_considerations.md`: required security context.

## Workflow and Quality
- Preserve raw user-authored text when it is rendered/transformed; store originals in dataset/raw attributes where applicable.
- Users may update this file; retain any user changes.
- Add a regression test for every state/format-loss bug (load -> edit -> save -> re-edit).
- Prefer lightweight telemetry where practical (timings, counters, and error summaries) to support UX and future introspection.
- For high-risk state changes (auth/org handling, DB writes, Vontology mutations), use a single authoritative pathway and reuse it consistently.
- Strong rule: treat canonical predicate concepts (e.g. #V#is_a_type_of) as the authoritative ontology relations. Do not introduce or rely on structural relationship fields when predicate concepts exist; kind/classification should be derived from canonical predicate usage.
- **Predicate concepts must be instances of `#V#predicate` (or a specialisation such as `#V#binary_predicate`) and must not keep `is_a_type_of` links that would force kind=`type`.** This ensures `is_predicate()` and the computed kind behave correctly.
- Do a quick factoring review whenever you touch write paths: search for existing canonical helpers/endpoints (especially for deletes/merges) and route through them rather than adding parallel pathways.
- Treat ontology/predicate investigation as a normal first step for Vontology work: resolve candidate concepts by name (including spelling variants like programme/program), search for existing predicates/types before inventing new ones, and record the findings (and chosen canonical IDs) in the related Jira issue.
- When code needs to manipulate real-world or conceptual entities (tasks, organisations, ideas, documents, relations, predicates, claims, assertions, rules, workflows, etc.), **always start by checking whether they already exist in Vontology**. Reuse/connect to existing representations; update them if needed; otherwise create new concepts rigorously.
- **Vontology representation guidance (from code behaviour):**
	- **Names:** Display names are resolved from `names[]` (NL then ABBR, preferred language first). Create/update names via `hasName` text relations (primary NL in `en-NZ`), and include CODE names for the `concept_id` and GUID where applicable. Do not rely on legacy top-level `name` fields.
	- **Descriptions:** Canonical descriptions are stored in `hasDescription` text relations. Avoid writing legacy `description` fields or `concept_data.preserved_fields` directly; use the concept/text relation services.
	- **Types vs individuals:** Types use `relationships.is_a_type_of` (parents). Individuals use `relationships.is_an_instance_of` (types). Computed kind is derived from these relationships, so avoid mixing them on the same concept.
	- **Type guidance predicates:** For types, prefer `#V#salient_binary_predicate_for_type` (or `salient_predicate_scopes.type_level`) to drive salient predicate prompts. These lists are consumed by salient predicate aggregation and relation elicitation. It is almost never appropriate for a type to be #V#is_a_type_of #V#thing (similarly, individuals should not be instances of thing). Seach the ontology for suitable types to attach concepts to. Use the most restrictive applicable and appropriate supertypes.
	- **Suggested relations:** Use the meta-relations service (`suggested_relations_for_type`) for type-level suggestions when possible; legacy `relationships.suggested_relations_for_type` is read as a fallback.
- **Vontology-first checklist (mandatory for ontology-related work):**
	1. Resolve candidate concepts by name (MCP search/resolve).
	2. Check for existing predicate/type concepts before inventing anything.
	3. If a list of concepts is needed, prefer a Vontology type and query its instances.
	4. Record the chosen canonical IDs in the Jira issue.
- **No hard-coded ontology lists**: do not add fixed lists of predicate/type IDs or names in code. If you believe a hard-coded list is unavoidable, you must:
	- explain why Vontology lookup is not viable,
	- add a Jira note documenting the exception, and
	- include a removal/cleanup plan.
- When uncertain, ask succinctly; do not guess or fabricate behaviour.
- When working on a task that may involve changes, you may open a branch based on the Jira task name (e.g. `JVNAUTOSCI-956-short-title`).
- When you start work on a task (including restarting from Done or other closed states), transition it to In Progress.
- When commenting on a JIRA task, say that the comment is generated by you (naming the agent) on behalf of the user
- When asked to "merge", default to: ensure changes are committed, fast-forward merge into `main`, push `main`, then delete the local and remote feature branch; if deletion needs a force flag, ask for confirmation first.

## Tooling and Automation
- Activate required external tool categories (Jira, Vontology, MongoDB, GitHub) without asking, when clearly needed.
- Treat tool categories as opt-in per session: activate before first use, and occasionally check whether a matching tool deactivation call exists (to disable categories when no longer needed).
- Where Vontology search, analysis or manipulation is impeded by the current vontology MCP tools, suggest code improvements to those tools that will facilitate high quality ontological engineering in future.
- If a tool category is not enabled, request enabling it by exact name.
- If Vontology or Vonrag MCP tools are not exposed in this session, use the stdio proxy scripts (`scripts/query_vontology_mcp.py`, `scripts/query_vonrag_mcp.py`) and check cached tool lists in `data/mcp_tool_cache/`.
- After implementing a fix and tests pass, post a Jira summary comment and transition the issue to the correct state.
- Do not emit JSON tool-call payloads as text; invoke tools directly.

## Atlassian MCP Reliability (Pause + Checkpoint)
If Atlassian MCP is flaky (timeouts, empty responses, 401/403/5xx):
1. Stop further Jira writes/edits beyond bounded retries.
2. Checkpoint: what issue(s), what succeeded, what remains.
3. Ask the user to restart MCP using the steps below.
4. Resume from the checkpoint after a minimal health check.

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
- Jira site URL: https://naoinstitute.atlassian.net/

## PowerShell-First Shell Rules
- Avoid Bash-only syntax: heredocs, `export`, `$(cmd)`, `source venv/bin/activate`.
- Use `$env:VAR = 'value'`, `$var = (cmd)`, and PowerShell here-strings.
- Prefer separate lines over `&&` unless failure short-circuit is required.
- Large multi-line Python: use a here-string variable and `pdm run python -c $code`, or add a script.

## UI Debugging (Missing/Invisible Elements)
- First check the DOM element exists; then check CSS visibility (display/visibility/opacity), size, positioning, z-index, and overflow.
- Absolute-positioned elements need a relative parent. Off-screen transforms and `overflow: hidden` are common culprits.
- If the element exists but looks wrong, capture `outerHTML` and key computed styles before changing JS.

## Copilot Tool Selection Hygiene
- Authoritative script: `scripts/set_vscode_copilot_selected_tools.ps1`.
- If you rely on a tool, ensure it stays enabled by updating that script.

## Research Prototype Engineering Philosophy
- Build research-appropriate quality: modular, extensible, and understandable.
- Avoid enterprise-scale over-engineering; keep abstractions just deep enough for near-term change.
