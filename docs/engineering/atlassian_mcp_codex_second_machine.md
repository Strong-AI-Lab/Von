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

2) Create or update `C:\Users\<you>\.codex\config.toml`:

```toml
# Codex CLI configuration

mcp_oauth_credentials_store = "file"

[mcp_servers.atlassian]
command = "npx"
args = ["-y", "mcp-remote", "https://mcp.atlassian.com/v1/sse"]
startup_timeout_sec = 60
```

3) Log in to Atlassian MCP:

```powershell
codex mcp login atlassian
```

Complete the browser authorisation flow.

4) Verify MCP is logged in:

```powershell
codex mcp list
```

Expected output includes:

- `atlassian` with `Status` = `enabled`
- `Auth` = `OAuth`

5) Quick Jira sanity check:

```powershell
codex exec -s read-only "List my open Jira issues in project JVNAUTOSCI assigned to me. Return key, summary, and status."
```

## Troubleshooting

- `Auth` shows `Not logged in` after login:
  - Ensure `mcp_oauth_credentials_store = "file"` is present.
  - Retry: `codex mcp logout atlassian`, then `codex mcp login atlassian`.
- MCP startup timeout:
  - Increase `startup_timeout_sec` (e.g. 90).
- MCP 404 errors against `/v1/sse`:
  - Use `npx mcp-remote` via the `command/args` config above. That is the supported workaround.
- No Jira access:
  - Confirm the Atlassian account you authorised can access `https://naoinstitute.atlassian.net`.

## Notes

- Keep secrets out of `.env` unless explicitly instructed to edit it.
- MCP resources may appear empty; use actual tool calls to validate (e.g. Jira search).
