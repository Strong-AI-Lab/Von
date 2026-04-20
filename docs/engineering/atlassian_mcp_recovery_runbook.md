# Atlassian MCP Recovery Runbook (VS Code)

This runbook documents the canonical recovery path for recurring Atlassian MCP auth failures in VS Code and VS Code Insiders.

Use this when Atlassian MCP appears logged in, but requests still fail with `401 invalid_token` or token acquisition is cancelled.

Transport note:

- Atlassian MCP should now be on the Streamable HTTP endpoint `https://mcp.atlassian.com/v1/mcp`.
- If a live config surface still points at `https://mcp.atlassian.com/v1/sse`, auth resets alone are not enough; fix the transport first.

## Failure Signature

Typical log pattern in `mcpServer.mcp.config.*.atlassian.log`:

- `Error getting token from server metadata: Canceled: Canceled`
- `Connection state: Error 401 ... {"error":"invalid_token","error_description":"Missing or invalid access token"}`

Notes:

- `Could not fetch resource metadata: AggregateError...` may appear in healthy and unhealthy sessions; treat it as non-definitive.
- Successful recovery ends with `Discovered 28 tools` (tool count may change in future releases; treat this as an example signal).

## Standard Recovery (Always First)

0. Verify the active config surfaces are on the canonical endpoint:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\verify_atlassian_mcp_transport.ps1 -ShowCodexList
```

If the Codex CLI config is stale, repair it first:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_atlassian_codex_mcp.ps1
```

For current Codex CLI builds, the Atlassian block should be a native streamable-HTTP entry:

```toml
[mcp_servers.atlassian]
url = "https://mcp.atlassian.com/v1/mcp"
```

If the verifier reports `legacy_wrapper`, the config is still using the old `npx mcp-remote ...` shape and `codex mcp login atlassian` will not treat it as an OAuth-capable streamable server.

1. Command Palette -> `MCP: Browse MCP Servers` -> Atlassian -> Restart.
2. If Restart is unavailable: `Extensions: Focus on MCP Servers - Installed View` -> Atlassian -> Restart/Stop/Start.
3. Run `Developer: Reload Window`.
4. Re-test Atlassian MCP calls.

Operational note:
- In recent incidents, `Reload Window` + Atlassian MCP restart has frequently restored auth without deeper intervention.

If still failing (especially repeated `401 invalid_token` / `Canceled` loops), continue to the credential reset path below.

## Credential Reset Path (No Sign-Out UI Case)

Use this when VS Code does not expose a usable sign-out action for the Atlassian MCP provider.

### Preferred: Use the helper script

Use `scripts/powershell/reset_atlassian_mcp_auth.ps1` first.

Dry-run (default behaviour):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\reset_atlassian_mcp_auth.ps1 -VSCodeProfile Insiders -DryRun 1
```

Apply (from an external PowerShell window, with VS Code host fully closed):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\reset_atlassian_mcp_auth.ps1 -VSCodeProfile Insiders -Apply
```

To process both hosts:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\reset_atlassian_mcp_auth.ps1 -VSCodeProfile Both -Apply
```

The script is safe by default:

- dry-run unless `-Apply` is passed
- timestamped backup before writes (unless `-NoBackup`)
- refuses apply mode from VS Code integrated terminal
- refuses apply mode while target VS Code host is still running

### Manual fallback (if scripting is unavailable)

If you cannot use the helper script, follow the manual steps below.

### 1) Fully close VS Code host first

Close all windows for the host you are fixing:

- VS Code Insiders -> `Code - Insiders`
- VS Code Stable -> `Code`

This is required. If the host is still running, it can recreate auth keys while you delete them.

### 2) Back up state database

```powershell
$stateDb = Join-Path $env:APPDATA 'Code - Insiders\User\globalStorage\state.vscdb'
$backup = "$stateDb.bak.$((Get-Date).ToString('yyyyMMdd-HHmmss'))"
Copy-Item -LiteralPath $stateDb -Destination $backup -Force
```

For stable VS Code, use:

```powershell
$stateDb = Join-Path $env:APPDATA 'Code\User\globalStorage\state.vscdb'
```

### 3) Remove Atlassian MCP auth/config rows

```powershell
$code = @'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("DELETE FROM ItemTable WHERE key LIKE 'secret://%mcp.atlassian.com/%'")
cur.execute("DELETE FROM ItemTable WHERE key LIKE 'secret://{\"isDynamicAuthProvider\":true,\"authProviderId\":\"https://mcp.atlassian.com/%'")
cur.execute("DELETE FROM ItemTable WHERE key LIKE 'mcp.config.%atlassian%https://mcp.atlassian.com/%'")
conn.commit()
print(f"Deleted rows: {conn.total_changes}")
conn.close()
'@
python -c $code $stateDb
```

Expected: non-zero deleted rows on first run, then zero on repeat.

### 4) Re-open host and re-authenticate

1. Re-open VS Code/Insiders.
2. Start Atlassian MCP server.
3. Complete OAuth browser flow when prompted.

### 5) Verify success

Check Atlassian MCP log for successful initialisation:

- `Connection state: Running`
- `Discovered ... tools` (for example `Discovered 28 tools`)

Absence of recurring `401 invalid_token` confirms recovery.

For Codex CLI validation from the same machine:

```powershell
codex exec -s read-only "Using Atlassian MCP, find Jira issue JVNAUTOSCI-1946 and return its key, summary, and status."
```

If that fails while the transport inventory still shows `/v1/sse`, fix the transport before repeating OAuth recovery.

## Additional Guidance

- Do not switch to Jira API token authentication as a substitute for Atlassian MCP OAuth; they are separate auth paths.
- Do not bypass MCP with ad-hoc REST scripts when MCP is flaky; recover the MCP session itself.
- If needed, repeat this runbook for both Insiders and Stable because each host keeps separate state.
- The SQLite reset patterns in the helper script intentionally match `mcp.atlassian.com` broadly, so they still cover both historic `/v1/sse` and current `/v1/mcp` dynamic-auth rows.
