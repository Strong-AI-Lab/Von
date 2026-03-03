# GitHub Internal MCP Runbook

This runbook documents the GitHub MCP proxy used by Von internal MCP tools.

## Purpose
- Provide a bounded GitHub tool surface for workflows and orchestrator actions.
- Keep writes fail-closed by default.
- Preserve deterministic diagnostics for operator troubleshooting.

## Environment setup
- `GITHUB_PERSONAL_ACCESS_TOKEN` or `GITHUB_TOKEN` or `GH_TOKEN`: GitHub token (required).
- `VON_GITHUB_MCP_COMMAND`: command for GitHub MCP server process (default `npx`).
- `VON_GITHUB_MCP_ARGS`: args for GitHub MCP server process (default `-y @modelcontextprotocol/server-github`).
- `VON_GITHUB_REPO_ALLOW_LIST`: comma-separated owner/repo allow-list (default `Strong-AI-Lab/Von`).
- `VON_INTERNAL_MCP_GITHUB_EXECUTE_MODE`: set `1`/`true` to allow execute-mode writes when `execute=true`.

## Guardrail model
- Write tools default to `dry_run=true`.
- Non-dry-run writes require one of:
  - `approved=true`, or
  - `execute=true` and `VON_INTERNAL_MCP_GITHUB_EXECUTE_MODE=1`.
- Owner/repo must be in `VON_GITHUB_REPO_ALLOW_LIST`.
- Idempotency cache is applied for non-dry-run calls when `request_id` is supplied.

## Internal tool matrix
Read:
- `github_get_auth_config`
- `github_list_tools`
- `github_get_me`
- `github_get_file_contents`
- `github_list_commits`
- `github_search_code`
- `github_list_pull_requests`
- `github_pull_request_read`
- `github_issue_read`
- `github_list_releases`
- `github_get_latest_release`
- `github_list_tags`
- `github_list_branches`

Write (guarded):
- `github_create_branch`
- `github_create_or_update_file`
- `github_create_pull_request`
- `github_update_pull_request`
- `github_create_pull_request_with_copilot`

## Operational checks
1. Run `github_get_auth_config` and confirm:
   - `token_present=true`
   - expected allow-list
   - expected command/args
   - `proxy_tools_available=true`
2. Run `github_list_tools` to verify external GitHub MCP server surface.
3. Run a dry-run write (`github_create_branch` with `dry_run=true`) before live writes.

## Failure modes
- `github_proxy_error`: subprocess/auth/server issue.
- `repository_not_allowlisted`: owner/repo not in allow-list.
- `approval_required`: non-dry-run write without explicit approval.
