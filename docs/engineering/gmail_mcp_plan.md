# Gmail MCP Plan (JVNAUTOSCI-734)

## Use cases
- **Von mailbox**: Headless processing of agent-facing mail (e.g., agent-mailbox@example.com) with predictable labels/queries. Favour read + label mutation only.
- **User/organisation mail**: Opt-in per user, per token. No cross-account reuse of Von's token.

## Design choices
- **Profile-based config**: Multiple Gmail profiles, each with its own token, scopes, and optional label/query filters. Config via `VON_GMAIL_PROFILES` JSON or legacy env fallbacks for a single `von-service` profile.
- **Read-first posture**: Default scopes are read-only. Mutation scope (`gmail.modify`) only when explicitly requested via env flag (`VON_GMAIL_MUTATION=true`) or profile scopes.
- **No interactive flows**: The service does not auto-run OAuth flows; tokens must be provisioned out of band to avoid unexpected prompts.
- **Separation of credentials**: Never reuse Von's token for user mail. Profiles are resolved explicitly by ID on each call.

## Initial surface (MVP)
- **Service helpers** (in code now): `list_messages`, `get_message`, `get_attachment`, per-profile.
- **Planned MCP tools** (next step):
  - `gmail_list_messages(profile, query?, label_ids?, max_results?)`
  - `gmail_get_message(profile, message_id, format?)`
  - `gmail_get_attachment(profile, message_id, attachment_id)`
  - `gmail_list_labels(profile)`
  - Guard mutation calls behind explicit `allow_mutation` flag.
- **Query shaping**: Optional `query_prefix` in profiles for inbox scoping (e.g., `label:agent-inbox`).

## Configuration
- Preferred: `VON_GMAIL_PROFILES` JSON (list or object) with fields:
  - `profile_id` (required)
  - `token_path` (required)
  - `credentials_path` (optional, for refresh if needed)
  - `user_id` (default `me`)
  - `scopes` (default read-only)
  - `label_filter` (optional array)
  - `query_prefix` (optional string)
- Fallback single-profile envs:
  - `VON_GMAIL_TOKEN_PATH` (required)
  - `VON_GMAIL_CLIENT_SECRET_PATH` or `GOOGLE_CLIENT_SECRET_PATH` (optional, refresh only)
  - `VON_GMAIL_USER`, `VON_GMAIL_LABELS` (comma-separated), `VON_GMAIL_QUERY_PREFIX`, `VON_GMAIL_MUTATION` (boolean)
- Optional default: `VON_GMAIL_DEFAULT_PROFILE` can be set to auto-fill Gmail tool calls when UI/local profile is not provided. The settings page now allows a "Gmail profile ID" text field stored in the browser (localStorage key `von_gmail_profile`).

## Phases
1) **Service layer (done)**: Profile loader + Gmail helpers with tests.
2) **MCP stdio server**: Expose tools above; profile required per call; read-only default.
3) **Internal MCP gateway**: Add proxy methods to `catalogue.py` and stdio HTTP parity.
4) **UI/hooks (optional)**: Wire Von UI or orchestrator to select profile and call tools; add audit logging via `knowledge_interaction_logger` for mailbox actions.

## Risks / mitigations
- Missing tokens → explicit errors; no interactive flows.
- Scope creep → read-only default, mutation behind flag.
- Profile mix-ups → explicit profile ID per call and separate token paths.
