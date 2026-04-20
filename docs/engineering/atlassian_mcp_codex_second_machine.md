# Atlassian MCP + Codex CLI (Second Machine Setup)

This guide documents how to set up Atlassian MCP access for the Codex CLI on a new Windows machine.
It avoids VS Code MCP integration issues by using the Codex CLI directly with the Atlassian MCP server.

## Prerequisites

- Windows with PowerShell.
- Node.js + npm available on PATH.
- Access to the Atlassian site: `https://naoinstitute.atlassian.net`.
- A browser for the OAuth login flow.

## One-time Setup

1) Install the Codex CLI:

```powershell
npm install -g @openai/codex
codex --version
```

2) Preferred: use the setup helper from the Von repo root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_atlassian_codex_mcp.ps1
```

What it does:

- creates `C:\Users\<you>\.codex\config.toml` if missing
- preserves the rest of an existing Codex config instead of overwriting the whole file
- upserts the Atlassian MCP block to use `https://mcp.atlassian.com/v1/mcp`
- ensures `mcp_oauth_credentials_store = "file"`
- prints `codex mcp list` afterwards

Manual equivalent if you need to inspect/edit the file directly:

```toml
# Codex CLI configuration

mcp_oauth_credentials_store = "file"

[mcp_servers.atlassian]
url = "https://mcp.atlassian.com/v1/mcp"
```

3) Verify the transport surfaces before login:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\verify_atlassian_mcp_transport.ps1 -ShowCodexList
```

Expected result:

- Codex CLI config reports `status: current`
- workspace `.vscode\mcp.json` reports `status: current`
- no checked surface reports Atlassian on `https://mcp.atlassian.com/v1/sse`
- `codex mcp get atlassian` shows a native URL/HTTP-style transport rather than a `command = "npx"` wrapper

4) Log in to Atlassian MCP:

```powershell
codex mcp login atlassian
```

Complete the browser authorisation flow.

5) Verify MCP is logged in:

```powershell
codex mcp list
```

Expected output includes:

- `atlassian` with `Status` = `enabled`
- `Auth` = `OAuth`

6) Quick Jira sanity check:

```powershell
codex exec -s read-only "List my open Jira issues in project JVNAUTOSCI assigned to me. Return key, summary, and status."
```

## Troubleshooting

- `Auth` shows `Not logged in` after login:
  - Ensure `mcp_oauth_credentials_store = "file"` is present.
  - Retry: `codex mcp logout atlassian`, then `codex mcp login atlassian`.
- MCP startup timeout:
  - Increase `startup_timeout_sec` (e.g. 90).
- MCP 404 errors:
  - Atlassian MCP endpoint has moved; use `https://mcp.atlassian.com/v1/mcp` (not `/v1/sse`).
  - First run the verification helper:
    - `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\verify_atlassian_mcp_transport.ps1 -ShowCodexList`
  - If the verifier reports `status: legacy_wrapper`, your local Codex config is still using the old `npx mcp-remote ...` shape. Rerun the setup helper so the Atlassian block becomes a native `url = "https://mcp.atlassian.com/v1/mcp"` server.
  - Ensure every config surface is aligned:
    - `C:\Users\<you>\.codex\config.toml`
    - `<workspace>\.vscode\mcp.json`
    - `C:\Users\<you>\AppData\Roaming\Code\User\mcp.json` (and Insiders equivalent if present)
  - If your Codex config still points at `/v1/sse`, rerun:
    - `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_atlassian_codex_mcp.ps1`
- Atlassian tools only appear on some Copilot models:
  - Check `chat.mcp.serverSampling` for Atlassian in user/workspace settings.
  - If `allowedModels` is too narrow (for example only `copilot/gpt-5-mini`), broaden it to include the models you actually use.
- No Jira access:
  - Confirm the Atlassian account you authorised can access `https://naoinstitute.atlassian.net`.

## Notes

- Keep secrets out of `.env` unless explicitly instructed to edit it.
- MCP resources may appear empty; use actual tool calls to validate (e.g. Jira search).
- Von now ships a workspace `.vscode\mcp.json` Atlassian entry on the canonical Streamable HTTP endpoint, but user/home config can still drift separately. Treat the verification helper as the source of truth for what your local machine is actually using.
- For current Codex CLI, Atlassian should be configured as a native streamable-HTTP MCP server via `url = "https://mcp.atlassian.com/v1/mcp"`. The older `command = "npx"` / `args = ["-y", "mcp-remote", ...]` wrapper shape is not the preferred operator path for OAuth login.
