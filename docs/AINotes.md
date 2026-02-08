"AINotes" is a living scratchpad for agents.

Goal: help a fresh agent get oriented in <2 minutes.

Rules of thumb:
- Keep this file short and current.
- Prefer pointers over prose.
- When something is no longer relevant, delete it (or move it to the archive).

Last updated: 2026-02-08

---

## Now (current focus)
- Fix/verify: interaction-session UX issues (see current branch name in git).
- Run focused tests after UI changes: `npm run test:frontend`.

## Next (likely follow-ups)
- Add a short regression note here when a bug is fixed (1–2 bullets), then move details to Jira/PR.
- Keep Phase/initiative tracking in Jira rather than in this file.

## Quick operational notes

### MCP access (Vontology/VonRAG)
- MCP resources list may only show MongoDB; Vontology/VonRAG expose tools, not resources.
- Validate access with tool calls like `mcp__vontology__get_context` and `mcp__vonrag__rag_list_collections`.

### Safe `.env` lookup
If you need values from `.env`, prefer the allowlisted helper script instead of reading the file directly:

- Script: `utilities/get_safe_env.py`
- Example: `pdm run python utilities/get_safe_env.py VON_DB_NAME`

### Test database hygiene
When running tests that require `VON_DB_NAME=test_von_db`, ensure the environment is reset afterwards.

### Server safety
Do not auto-start the server as part of automation unless explicitly requested.

### Git merge workflow
When merging feature branches: **commit → push → checkout main → pull → merge → push main → delete branch**.
Push the feature branch before merging so the remote tracking ref exists; otherwise `git branch -d` warns "not fully merged".

### Subtask branch base guardrail
- For Jira subtasks, branch from the parent issue branch, not directly from `main`.
- Example: for `JVNAUTOSCI-1087` (subtask of `JVNAUTOSCI-1084`), create/switch with `git checkout -b JVNAUTOSCI-1087-... JVNAUTOSCI-1084/main`.
- Verify ancestry before coding: `git merge-base --is-ancestor JVNAUTOSCI-1084/main HEAD` (exit code must be `0`).
- If the base is wrong and there are no local commits yet: `git rebase JVNAUTOSCI-1084/main`.

## High-signal references
- Agent rules: `AGENTS.md`
- User-facing overview: `USER_GUIDE.md`
- Engineering notes: `docs/engineering/`
- Archive (historical details): `docs/AINotes_archive.md`

## Recent decisions / changes worth remembering
- Presenter “spoken vs screen” behaviour is documented in Jira (JVNAUTOSCI-894); avoid duplicating the write-up here.
- 2026-02-08: Atlassian MCP reliability fix
	- Root cause: mixed MCP endpoints and overly narrow Copilot model sampling.
	- Canonical Atlassian MCP endpoint is now `https://mcp.atlassian.com/v1/mcp` (not `/v1/sse`).
	- Keep these aligned when debugging Jira MCP:
		- `~/.codex/config.toml` (`[mcp_servers.atlassian].url`)
		- workspace `.vscode/mcp.json` (`servers.atlassian.url`)
		- VS Code user MCP config (`%APPDATA%\\Code\\User\\mcp.json`, and Insiders equivalent if present)
	- If Atlassian tools only appear on some models, widen `chat.mcp.serverSampling` for Atlassian in both workspace and user settings.
- 2026-02-08: Von internal Jira proxy health check
	- Internal Jira path (`jira_proxy_mcp.py` -> `mcp_server.py`) uses `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN` (Basic auth), not Atlassian MCP OAuth.
	- Initial symptom was `jira_get_myself` `401`; resolved by rotating `ATLASSIAN_API_TOKEN`, updating `.env`, and restarting Von.
	- Current verified state (via `InternalMCPGateway.invoke()`):
		- `jira_get_myself`: returns user profile.
		- `jira_get_issue` (`JVNAUTOSCI-1086`): returns issue payload (`status=Done`).
	- If this regresses: rotate token -> update `.env` -> restart Von -> re-run `jira_get_myself`.

- 2026-01-06 diary
	- Swift blob store: improved OpenStack cloud config error message; fixed `openstacksdk` object-store method signature compatibility; added regression tests (JVNAUTOSCI-878).
	- Orchestrator: injected client speech-synthesis snapshot for "what voice?" questions; added regression test.
	- Write-tool guardrail: moved write permissioning into a workflow-driven policy (`write_tool_policy`) instead of hard-coded orchestrator logic; added regression test (JVNAUTOSCI-956).
- 2026-01-09 diary
	- Chat prompts: treat legacy `#V#von_llm_prompt` as a behaviour prompt so user-specific prompts load in chat and MCP introspection; updated tests; closed JVNAUTOSCI-974.


