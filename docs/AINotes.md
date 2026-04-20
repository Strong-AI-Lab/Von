"AINotes" is a living scratchpad for agents.

Goal: help a fresh agent get oriented in <2 minutes.

Rules of thumb:
- Keep this file short and current.
- Prefer pointers over prose.
- When something is no longer relevant, delete it (or move it to the archive).

Last updated: 2026-04-20

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
- 2026-04-20: Atlassian MCP transport verification/migration follow-through
	- Repo workspace config now ships an Atlassian MCP entry in `.vscode/mcp.json` on `https://mcp.atlassian.com/v1/mcp`.
	- Added `scripts/powershell/verify_atlassian_mcp_transport.ps1` to inventory live config surfaces (`~/.codex/config.toml`, workspace `.vscode/mcp.json`, VS Code user `mcp.json`) and optionally run `codex mcp list` / a read-only smoke test.
	- `scripts/powershell/setup_atlassian_codex_mcp.ps1` now preserves the rest of the user Codex config and upserts the Atlassian block instead of overwriting the whole file.
	- Current Codex expects Atlassian as a native streamable-HTTP config entry (`url = "https://mcp.atlassian.com/v1/mcp"`), not a legacy `npx mcp-remote ...` wrapper, if you want `codex mcp login atlassian` to work.
- 2026-02-08: Atlassian MCP reliability fix
	- Root cause: mixed MCP endpoints and overly narrow Copilot model sampling.
	- Canonical Atlassian MCP endpoint is now `https://mcp.atlassian.com/v1/mcp` (not `/v1/sse`).
	- Keep these aligned when debugging Jira MCP:
		- `~/.codex/config.toml` (`[mcp_servers.atlassian].url`)
		- workspace `.vscode/mcp.json` (`servers.atlassian.url`)
		- VS Code user MCP config (`%APPDATA%\\Code\\User\\mcp.json`, and Insiders equivalent if present)
	- Use `scripts/powershell/verify_atlassian_mcp_transport.ps1 -ShowCodexList` before assuming the live machine matches the repo docs.
	- If Atlassian tools only appear on some models, widen `chat.mcp.serverSampling` for Atlassian in both workspace and user settings.
- 2026-02-08: Von internal Jira proxy health check
	- Internal Jira path (`jira_proxy_mcp.py` -> `mcp_server.py`) uses `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN` (Basic auth), not Atlassian MCP OAuth.
	- Initial symptom was `jira_get_myself` `401`; resolved by rotating `ATLASSIAN_API_TOKEN`, updating `.env`, and restarting Von.
	- Current verified state (via `InternalMCPGateway.invoke()`):
		- `jira_get_myself`: returns user profile.
		- `jira_get_issue` (`JVNAUTOSCI-1086`): returns issue payload (`status=Done`).
	- If this regresses: rotate token -> update `.env` -> restart Von -> re-run `jira_get_myself`.
- 2026-02-08: WS2 workflow safety envelope (JVNAUTOSCI-1087)
	- `ActionRegistry.execute()` now stamps canonical action outcome keys in context (`last_action_failed`, `last_step_ok`, `last_action_error`, etc.) on both success and failure.
	- `WorkflowExecutor` and `DurableWorkflowExecutor` now honour explicit `on_failure` transitions instead of always failing fast on action errors.
	- Regression anchors:
		- `tests/backend/test_e2e_vontology_workflow.py::TestBranchingWorkflowWithMCP::test_on_failure_routes_to_recovery_tool`
		- `tests/backend/test_durable_workflow_executor_safety.py`
		- `tests/backend/test_mcp_action_handler.py::TestActionOutcomeEnvelope`
- 2026-02-08: WS3 executable discovery/routing hardening (JVNAUTOSCI-1088)
	- Discovery now annotates each workflow candidate with:
		- `is_executable`
		- `executability_reason` (`executable_now` / `graph_incomplete` / `non_executable_design_artifact`)
		- confidence + routing eligibility fields
	- Discovery payload now separates:
		- `candidates` (full ranked set, with reasons)
		- `matches` / `routing_matches` (selector-eligible set)
	- Orchestrator selector filtering defaults:
		- drop non-executable candidates
		- drop policy-unsafe candidates (not in current workflow registry)
	- Explicit overrides:
		- `VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE=1`
		- `VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE=1`
	- Regression anchors:
		- `tests/backend/test_workflow_discovery_service.py`
		- `tests/backend/test_orchestrator_workflow_selector_routing.py`
- 2026-02-08: WS4 MCP introspection reliability (JVNAUTOSCI-1089)
	- Root cause found: `chat_introspect` handler returned fields not declared in its MCP `output_schema`, causing `InternalMCPGateway.invoke("chat_introspect", ...)` to fail with `SchemaValidationError`.
	- Fix pattern:
		- keep `chat_introspect` output schema aligned with handler payload keys
		- allow forward-compatible diagnostics fields for introspection snapshots
		- expose workflow/introspection surface matrix in tool responses (`workflow_list_definitions`) and docs (`docs/engineering/workflow_mcp_capability_matrix.md`)
		- provide actionable `unknown_tool` diagnostics on `mcp_stdio_server` for `workflow_*` and chat introspection tools (classification: `internal_mcp_only_tool`)
		- keep workflow/introspection surface tool names centralised in `workflow_surface_capabilities.py` and validated by tests to prevent drift
		- add `workflow_mcp_health_check` for read-only gateway-path checks of core workflow MCP tools
	- Regression anchors (gateway path, not direct handler only):
		- `tests/backend/test_internal_mcp_chat_prompt_tools.py::test_chat_introspect_gateway_invoke_success_path`
		- `tests/backend/test_internal_mcp_chat_prompt_tools.py::test_chat_introspect_gateway_invoke_handler_error_path`
		- `tests/backend/test_internal_mcp_workflow_tools.py::test_workflow_list_definitions_gateway_invoke_success_path`
		- `tests/backend/test_internal_mcp_workflow_tools.py::test_workflow_list_instances_gateway_invoke_error_path`
		- `tests/backend/test_internal_mcp_workflow_tools.py::test_workflow_mcp_health_check_gateway_invoke_success_path`
		- `tests/backend/test_mcp_stdio_unknown_tool_diagnostics.py`
- 2026-02-08: WS5 durable schedule scalability/correctness (JVNAUTOSCI-1091)
	- Replaced broad due-schedule concept scans with a query-first path over `text_relations`:
		- primary due filter uses `predicate=#V#next_run_scheduled_for` + relation context `next_run_epoch_ms`
		- enabled filter uses `predicate=#V#is_schedule_enabled` + relation context `enabled_bool`
	- Write path now persists those context fields when schedules are created/updated (`_set_next_run_timestamp`, `_set_enabled_state`), with UTC-normalised timestamps.
	- Kept compatibility fallback for legacy relations that lack context metadata.
	- Added scheduler poll telemetry snapshot (`WorkflowScheduler.get_poll_metrics`) including:
		- due-set size
		- triggered count
		- due-lookup latency
		- total poll latency
	- Regression anchors:
		- `tests/backend/test_durable_workflow_system.py::TestScheduleManagement::test_find_due_schedules_avoids_concept_list_scan`
		- `tests/backend/test_durable_workflow_system.py::TestScheduleManagement::test_find_due_schedules_legacy_relation_fallback`
		- `tests/backend/test_durable_workflow_system.py::TestWorkflowScheduler::test_process_due_schedules_updates_poll_metrics`
- 2026-02-08: WS8 E2E/docs/rollout controls (JVNAUTOSCI-1093)
	- Added metadata validation rollout control:
		- env var `VON_WORKFLOW_METADATA_VALIDATION_MODE` with modes `enforce` (default), `warn`, `off`
		- shared telemetry fields on validation events: `mode` + `enforced`
	- If workflow hardening causes unexpected failures in production-like runs:
		1. set mode to `warn`, restart, inspect `workflow_metadata_validation_events`
		2. if still unstable, set mode to `off` as temporary fallback
		3. return to `enforce` after remediation
	- Regression anchors:
		- `tests/backend/test_orchestrator_write_tool_guard_gateway_e2e.py`
		- `tests/backend/test_internal_mcp_workflow_tools.py::test_workflow_schedule_gateway_tools_integrate_with_scheduler`
		- `tests/backend/test_workflow_metadata_validation.py`
		- `tests/backend/test_durable_workflow_executor_safety.py`

- 2026-01-06 diary
	- Swift blob store: improved OpenStack cloud config error message; fixed `openstacksdk` object-store method signature compatibility; added regression tests (JVNAUTOSCI-878).
	- Orchestrator: injected client speech-synthesis snapshot for "what voice?" questions; added regression test.
	- Write-tool guardrail: moved write permissioning into a workflow-driven policy (`write_tool_policy`) instead of hard-coded orchestrator logic; added regression test (JVNAUTOSCI-956).
- 2026-01-09 diary
	- Chat prompts: treat legacy `#V#von_llm_prompt` as a behaviour prompt so user-specific prompts load in chat and MCP introspection; updated tests; closed JVNAUTOSCI-974.


