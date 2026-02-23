# Coding Agent Diagnostics Export

`JVNAUTOSCI-1074` adds an opt-in diagnostics export path for local coding-agent troubleshooting.

## How to Export

1. Open the Chat tab in Von.
2. Press `Ctrl+Shift+D`.
3. Von writes a sanitised snapshot to `data/diagnostic_latest.json`.

## What Is Included

- Tool invocation summaries (method/status/duration/argument keys)
- Error metadata and warning summaries
- Turn execution timing/progress diagnostics when available
- LLM call metadata summaries

## What Is Removed

- Message bodies and `messages` payloads
- Prompt text fields (`prompt`, `prompt_preview`, `user_prompt`)
- Response text fields
- Token/secret/password/cookie-style fields
- Raw nested tool payload bodies (replaced with shape summaries)

## Notes

- Export is explicit and user-triggered by shortcut.
- Each export overwrites `data/diagnostic_latest.json` with the newest snapshot.
