# Gmail MCP Plan (JVNAUTOSCI-734, JVNAUTOSCI-2222)

## Current capability surface

Von has a profile-scoped Gmail integration exposed through the service layer,
the internal MCP gateway, and the stdio MCP manifest.

- `gmail_list_profiles(profile?)` lists configured/authorised Gmail profiles
  without returning secrets.
- `gmail_list_messages(profile, query?, label_ids?, max_results?,
  bypass_profile_query_prefix?)` lists messages.
- `gmail_get_message(profile, message_id, format?)` fetches message details.
- `gmail_get_attachment(profile, message_id, attachment_id)` fetches
  attachments.
- `gmail_list_labels(profile)` lists labels.
- `gmail_modify_labels(profile, message_id, allow_mutation, add_labels?,
  remove_labels?)` performs guarded label mutation.
- `gmail_send_message(profile, to, subject, body_text, allow_send, cc?, bcc?,
  reply_to?, body_html?)` sends outbound email and returns Gmail send metadata.

The default posture remains read-only. Mutations and sends are available only
through explicit guard fields, send/mutation-capable OAuth scopes, and the
workflow/write-policy guards that authorise external side effects.

## Gmail REST support matrix

This matrix is the repo-side support summary for the Gmail REST resource
families. It is not the production authority for whether a turn may perform a
side effect; that authority remains in Vontology/workflow/prompt/tool metadata.

| Gmail resource family | Von support | Tracking |
| --- | --- | --- |
| Profiles and auth status | Supported through profile config, agent OAuth token storage, and `gmail_list_profiles`. | Current |
| `users.messages.list/get/attachments.get` | Supported through `gmail_list_messages`, `gmail_get_message`, and `gmail_get_attachment`. | Current |
| `users.messages.send` | Supported through `gmail_send_message` with `allow_send=true`, send-capable scopes, and external write guardrails. | Current |
| Message label mutation | Partially supported through `gmail_modify_labels`; broader batch/trash/permanent mutation is deferred. | `JVNAUTOSCI-2224` |
| `users.labels` CRUD | `list` is supported through `gmail_list_labels`; create/update/delete is deferred. | `JVNAUTOSCI-2224` |
| `users.drafts` and threaded reply workflows | Deferred. Drafts are not send evidence and need separate workflow semantics. | `JVNAUTOSCI-2223` |
| `users.threads` read/mutation/reply semantics | Deferred except where message reads expose thread IDs. | `JVNAUTOSCI-2223`, `JVNAUTOSCI-2224` |
| `users.history`, `watch`, and `stop` | Deferred; requires workflow-owned monitor lifecycle and Pub/Sub/webhook configuration. | `JVNAUTOSCI-2225` |
| `users.settings`, filters, forwarding, send-as, delegates, POP/IMAP, vacation | Deferred; high-impact mailbox settings need explicit represented authority and scope design. | `JVNAUTOSCI-2226` |
| Import/insert/permanent delete | Deferred; these operations need a dedicated high-risk write-policy design before exposure. | `JVNAUTOSCI-2224` |

## Design choices

- **Profile-based config**: Multiple Gmail profiles, each with its own token,
  scopes, and optional label/query filters. Config comes from
  `VON_GMAIL_PROFILES` JSON or the legacy single-profile environment fallback.
- **Explicit writes**: Label mutation requires `allow_mutation=true`; outbound
  sending requires `allow_send=true`. The tool descriptions and prompt
  authority instruct planners not to treat reads or label changes as evidence
  of sending.
- **Workflow-authorised external side effects**: Gmail send is classified as an
  external non-Vontology write. The generic `workflow_mcp.invoke_tool` path can
  execute it only when represented workflow metadata supplies external mutation
  authority and a side-effect policy allowing `gmail_send_message`.
- **No automatic interactive flows**: The Gmail service layer does not auto-run
  OAuth flows; tokens must be provisioned deliberately to avoid unexpected
  prompts in headless environments.
- **Separation of credentials**: Never reuse Von's token for user mail.
  Profiles are resolved explicitly by ID or authorised email address on each
  call.

## Agent Gmail OAuth

Von supports an explicit, UI-driven OAuth flow to authorise agent Gmail profiles
without altering the existing user-login OAuth.

- The UI initiates `/von/api/agent/gmail/oauth/start?profile_id=...` and stores
  resulting tokens in MongoDB encrypted at rest.
- The Gmail service prefers DB-stored tokens when present and falls back to the
  existing `token_path` file behaviour for backwards compatibility.

Required/optional configuration:

- `GMAIL_TOKEN_ENCRYPTION_KEY` is required for DB token storage.
- `VON_AGENT_GMAIL_OAUTH_REDIRECT_URI` should match the configured callback URL.
- `VON_AGENT_GMAIL_OAUTH_CLIENT_SECRET_PATH` optionally overrides the profile's
  `credentials_path`.

OAuth 403 troubleshooting:

- If the consent screen is in Testing, add the account under OAuth consent
  screen -> Audience -> Test users.
- Ensure the OAuth client has an exact Authorised redirect URI matching the
  callback URL.

## Configuration

Preferred: `VON_GMAIL_PROFILES` JSON, as a list or object, with fields:

- `profile_id` (required)
- `token_path` (required for file-token mode)
- `credentials_path` (optional, used for agent OAuth or refresh in file-token
  mode)
- `user_id` (default `me`)
- `scopes` (default read-only; sending requires `gmail.send`, `gmail.compose`,
  `gmail.modify`, or `mail.google.com`)
- `label_filter` (optional array)
- `query_prefix` (optional string)

Fallback single-profile envs:

- `VON_GMAIL_TOKEN_PATH`
- `VON_GMAIL_CLIENT_SECRET_PATH` or `GOOGLE_CLIENT_SECRET_PATH`
- `VON_GMAIL_USER`
- `VON_GMAIL_LABELS`
- `VON_GMAIL_QUERY_PREFIX`
- `VON_GMAIL_MUTATION` (legacy mutation opt-in)

Optional default:

- `VON_GMAIL_DEFAULT_PROFILE` can auto-fill Gmail tool calls when UI/local
  profile context is not provided.

## Validation expectations

- Service tests should cover MIME construction, recipient validation,
  send-capable scope checks, and `allow_send` fail-closed behaviour.
- Gateway tests should invoke `gmail_send_message` through
  `InternalMCPGateway.invoke()`, not only the service helper.
- Workflow tests should cover `workflow_mcp.invoke_tool` with
  `gmail_send_message` as a guarded external write, including the block path
  without represented write policy.
- Turn-level validation must not claim an email was sent unless authenticated
  Gmail send execution returned success evidence for the resolved profile,
  recipient, subject, and body.

## Risks and mitigations

- Missing tokens or scopes -> explicit errors; no interactive fallback.
- Profile mix-ups -> explicit profile ID/address resolution per call.
- Accidental sends -> `allow_send=true`, send-capable scopes, and external write
  policy are all required.
- False success claims -> prompt authority requires `gmail_send_message`
  execution evidence; listing, reading, or labelling Gmail messages is not send
  evidence.
