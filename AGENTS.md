# AI Agent Guide (Trimmed)

This document provides key instructions for AI agents working on the Von-Private project.
All AI agents must read this file and `docs/engineering/security_considerations.md` before starting any work.

## TL;DR for Agents
1. Use New Zealand English spelling always (behaviour, colour, organisation, realise). This can include careful use of Māori words.
2. PowerShell is the default shell. Do not emit Bash heredocs, `export`, `$(cmd)`, or `source venv/bin/activate` unless explicitly asked for Bash.
3. Never clobber `.env`. Only touch it if explicitly asked; never print its contents or secrets.
4. It's fine to auto-start the server; don't wait for explicit user instruction. (Still obey system/developer constraints if stricter.)
5. Use MCP tools (Vontology/Jira/Mongo) by default; explain if you must use another path.
6. JIRA issues must be assigned on creation (assignee = current user unless told otherwise).
7. Keep changes minimal, well-scoped, and add/update tests and docs where relevant.
8. Prefer small, composable functions; avoid monolithic helpers.
9. Always check VS Code Problems panel (or run `get_errors`) after edits and when errors are reported.
10. Enable pre-commit guardrails: `git config core.hooksPath .githooks`.

## Core AI-Focused Documents
- `docs/AINotes.md`: short-term memory and tactical log.
- `docs/concept_refactoring.md` (or current plan doc): project roadmap for major work.
- `docs/software_engineering.md`: conventions, debugging, and lessons learned.
- `docs/engineering/security_considerations.md`: required security context.

## Workflow and Quality
- Preserve raw user-authored text when it is rendered/transformed; store originals in dataset/raw attributes where applicable.
- Add a regression test for every state/format-loss bug (load -> edit -> save -> re-edit).
- For high-risk state changes (auth/org handling, DB writes, Vontology mutations), use a single authoritative pathway and reuse it consistently.
- When uncertain, ask succinctly; do not guess or fabricate behaviour.

## Tooling and Automation
- Activate required external tool categories (Jira, Vontology, MongoDB, GitHub) without asking, when clearly needed.
- If a tool category is not enabled, request enabling it by exact name.
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
