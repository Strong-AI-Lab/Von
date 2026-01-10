"AINotes" is a living scratchpad for agents.

Goal: help a fresh agent get oriented in <2 minutes.

Rules of thumb:
- Keep this file short and current.
- Prefer pointers over prose.
- When something is no longer relevant, delete it (or move it to the archive).

Last updated: 2026-01-10

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

## High-signal references
- Agent rules: `AGENTS.md`
- User-facing overview: `USER_GUIDE.md`
- Engineering notes: `docs/engineering/`
- Archive (historical details): `docs/AINotes_archive.md`

## Recent decisions / changes worth remembering
- Presenter “spoken vs screen” behaviour is documented in Jira (JVNAUTOSCI-894); avoid duplicating the write-up here.

- 2026-01-06 diary
	- Swift blob store: improved OpenStack cloud config error message; fixed `openstacksdk` object-store method signature compatibility; added regression tests (JVNAUTOSCI-878).
	- Orchestrator: injected client speech-synthesis snapshot for "what voice?" questions; added regression test.
	- Write-tool guardrail: moved write permissioning into a workflow-driven policy (`write_tool_policy`) instead of hard-coded orchestrator logic; added regression test (JVNAUTOSCI-956).
- 2026-01-09 diary
	- Chat prompts: treat legacy `#V#von_llm_prompt` as a behaviour prompt so user-specific prompts load in chat and MCP introspection; updated tests; closed JVNAUTOSCI-974.


