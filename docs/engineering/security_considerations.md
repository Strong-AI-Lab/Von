# Security Considerations for Von

- **Kind:** Security guidance with dated deployment-posture observations
- **Lifecycle:** Active
- **Authority:** Canonical security guidance routed by [`AGENTS.md`](../../AGENTS.md)
- **Last reviewed:** 2 September 2026
- **Evidence boundary:** Statements about current users, deployments, and
  implemented controls are dated observations and must be revalidated; the
  security requirements do not expire merely because implementation evidence
  changes

## When to read this guide

Read the applicable sections before work with material security exposure:
authentication or authorisation, private or cross-namespace data, secrets,
untrusted content combined with tool authority, effects outside ordinary
bounded and recoverable standing delegation, deployment, or
administrator/operator surfaces. The risk- and profile-calibrated security
principles in `AGENTS.md` apply to every task; an ordinary bounded write or
integration does not require this entire guide solely because it is a write or
integration.

Implementation and deployment observations in this document are dated. Verify
the affected current code and deployment profile before relying on a statement
labelled "Current".

## Critical Security Context

**Von is currently a research prototype. As far as we know on 2026-04-24, the
live repository and operational instances are being used only by the Strong AI
Lab (SAIL) at the University of Auckland.**

That SAIL-only context matters: the current near-term threat model is mostly
trusted operators, local development, and a small research group rather than a
public multi-tenant SaaS. It does **not** mean Von should be designed as if it
will remain a trusted single-user tool. The public repository, external
contributors, future partner pilots, and Von's agentic-AI programme all create
legitimate security and safety risks that should be handled deliberately.

The correct posture is therefore **profiled security**:

- keep local SAIL research work fast enough to make progress;
- allow useful public/read-only demo and contributor workflows where data is
  intentionally public;
- keep private user/org data and secrets outside capabilities not delegated to
  the current actor;
- allow bounded, observable, recoverable action within standing delegation,
  including ordinary reversible mutations without automatic confirmation;
- reserve blocking, approval, or stronger confinement for effects whose
  residual harm cannot be kept tolerable through scoping, read-back, recovery,
  compensation, or rate/blast-radius limits;
- make the stricter partner/production path visible and actionable rather than
  burying it as generic "production TODOs".

Von is still **not production-ready for multi-tenant or adversarial
environments** without the hardening work listed below.

### Risk, recovery, and useful autonomy

Security sets the maximum capability available to an actor; it should not
silently become a deterministic interpretation of what the actor means.
Classify the concrete consequence, not the operation name. `Write`, `send`, and
`delete` cover effects ranging from a versioned draft or recoverable Trash move
to an irreversible disclosure or purge.

Choose the least restrictive assurance that keeps residual harm within the
deployment profile and delegated envelope. Consider impact, blast radius and
repetition, detectability, restoration probability and time, secondary effects,
external commitment, and recovery burden. Prefer bounded capability,
reversible defaults, receipts/read-back, compensation, and monitoring when
they are adequate. Use unbypassable prevention or action-specific approval when
an effect exceeds delegated authority or those recovery mechanisms are
demonstrably insufficient.

Reversibility does not create authority, and an advertised undo is not enough:
recovery must be independently credible. Equally, a probabilistic semantic
label wrapped in deterministic code is not a security boundary.

## Operating Profiles

Security requirements should be interpreted by deployment profile, not as one
undifferentiated rule set.

| Profile | Intended use | Acceptable loosenings | Required tightenings |
| --- | --- | --- | --- |
| Local SAIL development | Trusted SAIL researchers on local machines | Local diagnostics, trusted automation headers, public/sample corpora, experimental namespace warnings, bounded recoverable action under standing delegation | Never commit secrets/runtime data; private RAG remains namespace-scoped; materially irreversible or high-blast effects need stronger confinement or approval |
| Shared SAIL service | Small trusted SAIL group using a shared backend | Read-only operational diagnostics may remain visible to authenticated SAIL users | Admin actions require admin auth; audit sensitive mutations; avoid raw client identity trust |
| Public open-repo contributor | External users cloning/running their own instance | Public repo docs/sample knowledge can be searchable without login | Root `SECURITY.md`, secret/dependency/code scanning, safe defaults, no expectation that contributors have SAIL credentials |
| Partner pilot | Partner or Living Lab deployment with real organisational data | Limited public/onboarding help can remain unauthenticated | Strict auth, admin endpoint protection, audit logs, backup encryption, namespace strict mode for high-risk paths |
| Production/multi-tenant | Adversarial or internet-facing service | No private-data loosening by default | Full authN/authZ, rate limits, RLS/ACL defence in depth, security monitoring, incident response, penetration testing |

## Data and Capability Classes

Use different controls for different data and capability classes.

| Class | Examples | Default access posture |
| --- | --- | --- |
| Public project knowledge | README, public docs, sample knowledge, public papers | May be exposed through unauthenticated read-only search with provenance |
| SAIL internal operational data | Jira tasks, SAIL project notes, internal telemetry | SAIL-authenticated or trusted local operator only |
| User/org private data | chat history, uploaded files, private RAG chunks, partner data | Authenticated, namespace-scoped, fail closed on missing or conflicting scope |
| Secrets and credentials | `.env`, API keys, OAuth tokens, DB URIs, device secrets | Never printed, never committed, redacted in diagnostics |
| Authority-bearing artefacts | Vontology prompts/workflows/policies, publication gates, MCP write tools | Keep effects within authenticated standing delegation; for canonical ontology publication or scope changes, require the separate semantic-authority and exact-delegation boundary, plus provenance, read-back, and recovery appropriate to the commitment |
| Admin/operator actions | sync, reindex, DB reconnect, shutdown, imports, backups | Admin auth or local trusted operator profile only |

## Known Security Limitations

### 1. Authentication identity boundary

Google OAuth email addresses must resolve only through the dedicated
`#V#hasVonLoginEmail` predicate. The ordinary `#V#has_email` predicate is
descriptive contact information: adding it to a person must never let that
address authenticate as the person. Login-email bindings are an explicit
governed allow-list, are unique across user concepts, and fail closed when
absent, stale, or ambiguous. An unbound OAuth identity must not inherit a
browser-supplied or prior-session concept and must not auto-create a user.
For a database upgraded from the pre-cutover reader, an explicitly approved
one-time migration may preserve the unambiguous `#V#has_email` bindings that
already granted login authority before this boundary existed. The migration
does not make subsequent ordinary contact-email assertions authoritative.

`#V#hasVonLoginEmail` specialises and entails `#V#has_email`; the reverse
inference is forbidden. Generic ontology mutation surfaces reserve the narrow
predicate so only the dedicated identity-binding lifecycle can change it.
Newly resolved Google sessions carry a versioned login-assurance marker at the
trusted session boundary. Pre-cutover email sessions lack that evidence and
must fail closed rather than retaining an actor or organisation scope.

The internal conversational lifecycle separates inspection from mutation.
`get_von_login_email_bindings` reads only the narrow predicate for a represented
member of the actor's server-bound current organisation and requires the
actor's live `MANAGE_MEMBERS` permission there. `manage_von_login_email_binding`
uses the same exact-organisation check, actor-bound idempotency, a durable
receipt, conflict-safe serialisation, and canonical read-back. Because a login
binding authenticates the global person, a real bind or removal additionally
requires the actor to administer every organisation the target can enter, or
to hold the separate Von operational-administrator role. A bind request also
requires that authority before certifying an existing address as globally
unambiguous; the dedicated read can still report the target's current bindings
without making that stronger claim. An absent removal is a target-local no-op.
Generic KB visibility remains unchanged, and a conflict does not disclose the
other bound identity. Removing a binding prevents future OAuth resolution but
does not terminate an already active session.

### 1a. Narrow endpoint safeguard implemented in December 2024

**Location**: `src/backend/server/routes/von_routes.py` - `/generate` endpoint

**Bounded result**: Client-provided user IDs are rejected on the named path.
This is not a claim that Von's authentication or deployment posture is secure
as a whole.

**Implementation**: The system requires proper authentication for private
user/org-scoped operations via:

1. **Server-side session** (populated during login flow via `/api/auth/google/login`)
2. **Validated headers** (`X-User-Concept-ID`) with person concept verification
   for trusted automation/local integration paths only

**Behaviour**:
```python
# SECURITY: Do NOT trust client-provided user_id - require proper authentication
user_concept_id = get_effective_user_concept_id()
if not user_concept_id:
    # Client-provided user_id is IGNORED for security
    # RAG tools will return "namespace_required" error
```

**Unauthenticated browser experience**:
- The main Von page is replaced by a sign-in gate.
- Chat, search, Messages, Settings, and actor-scoped background work are not
  initialised until the server session reports both authentication and a
  resolved Von user concept.
- Browser storage is only a compatibility projection of the login-bound actor;
  it cannot select or replace that actor.
- Authentication-status transport failures leave the application fail-closed
  and offer retry without treating an unknown result as logout or erasing the
  last actor-bound organisation preference.
- User-scoped Settings profile and recommendation-review routes require a
  matching authenticated session actor (or the existing explicit privileged
  role); legacy endpoints that enumerated or fabricated users are absent.
- Explicitly public, read-only API or knowledge surfaces may remain available
  separately; the home gate is not a substitute for endpoint authorisation.

**Important limitation**:

`X-User-Concept-ID` validation proves only that the header names an existing
person/von_user concept. It is not equivalent to authentication of the caller.
It is acceptable in local SAIL development and trusted automation profiles, but
partner/production deployments should either remove it, accept it only from a
trusted reverse proxy, or bind it to a signed service token.

**Useful loosening**:

Unauthenticated users should be allowed to query explicitly public/read-only
knowledge surfaces, such as repo documentation or sample corpora, once those
surfaces are separated from user/org private RAG. Do not use private namespace
fallbacks to achieve this; create a distinct public scope with provenance.

**Agent Transparency**:
The agent receives authentication status in system prompt:
- **Authenticated**: "🔐 AUTHENTICATION STATUS: Authenticated (namespace: #V#user)"
- **Unauthenticated**: "⚠️ AUTHENTICATION STATUS: NOT AUTHENTICATED - RAG tools unavailable"

### 2. Namespace-Based Isolation

**Approach**: User data isolation implemented via `namespace` field filtering in database queries.

**Current Protection**:
- Private/user-scoped RAG tools (`rag_list_indexed`, `rag_get_item`, `search_knowledge_base`) **require** namespace parameter
- Namespace derived from authenticated user context
- Queries filtered by namespace at database level
- **Fail closed**: If no namespace can be determined, return error (not empty results)

**Limitations**:
- Relies on proper namespace derivation from user context
- If user context is compromised (see #1), namespace isolation is bypassed
- No database-level ACLs or row-level security

**Best Practice** (Production):
- Implement middleware authentication layer
- Add database-level row-level security (RLS) as defense in depth
- Audit logging for all cross-namespace queries (currently missing)

### 2A. Namespace Component Discipline (User + Organisation)

**Intent**: The security outcome is that one actor cannot read or mutate
another actor's private data. Namespace is the current implementation's main
partitioning mechanism, not the only architecture that could satisfy that
outcome.

Current implementation details:
- See `docs/engineering/effective_namespace_contract.md` for the current
  resolver order, propagation fields, policy modes, and migration history.
  Treat those details as compatibility notes, not requirements for a
  replacement path.

**Current model (research phase)**:
- Support both user-only namespaces (`#V#<user>`) and user@org namespaces (`#V#<user>@<org>`).
- Treat namespace as a structured identity scope with two components:
  - user component (`user_concept_id`)
  - organisation component (`organisation_concept_id`, optional)
- Respect both the combined namespace string and its components. They are related but not interchangeable.

**Engineering guidance (now)**:
- Derive and normalise namespaces via shared namespace helpers (`namespace_service`) rather than ad-hoc string handling.
- Preserve org scope when present; do not silently degrade `#V#user@org` flows to user-only scope in downstream tool calls or persistence.
- Carry component context alongside namespace where practical (`namespace`, `user_concept_id`, `organisation_concept_id`, `namespace_source`) for traceability and future policy enforcement.
- Keep fail-closed behaviour for missing namespace on user-scoped operations (especially RAG).
- Monitor compact namespace-isolation counters via `/admin/rag_status` (and `/diag`) to detect mismatch/missing-component regressions before enabling stricter rejection modes.

**Tightening path (later)**:
- During the current experimental phase, prefer visibility and diagnostics for non-critical mismatches over aggressive hard failures.
- Avoid inconsistent duplicate resolvers while the current mechanism remains;
  a replacement may use a different, simpler isolation design.
- Design new handlers so switching from “diagnose mismatch” to “reject mismatch” is a policy change, not a codebase-wide rewrite.

**Profile guidance**:

- Local SAIL development may keep experimental diagnose/warn behaviour where it
  preserves progress and emits telemetry.
- Partner pilots and production should use strict mismatch rejection for
  user/org-private operations.
- Public/read-only corpora should use a clearly named public scope and must not
  silently reuse a user's namespace.

### 3. Session Management

**Current**: Flask server-side sessions with secret key.

**Limitations**:
- Session fixation if not properly rotated after login
- No explicit session timeout configuration documented
- Cookie security flags (httponly, secure, samesite) should be verified

**Recommendations**:
- Document session configuration in production deployment guide
- Implement explicit session rotation on privilege escalation
- Add session timeout and idle timeout

### 4. MCP Tool Access Control and Agentic-AI Threats

Canonical ontology publication is not part of an ordinary tool's visibility
aperture. The `JVNAUTOSCI-2632` candidate separates organisation/global
semantic roles from Von operational administration and requires an exact,
server-issued, short-lived, non-recursive delegation when authority for a
covered ontology effect is handed to a separately acting agent. A workflow
effect that stays inside one trusted authenticated actor-bound execution may
evaluate the final exact intent against that actor's live user, organisation,
or global semantic authority after the workflow write ceiling is checked; that
is not a semantic delegation. The marker grants no authority and missing or
mismatched roles still fail closed. The current
design/implementation boundary is
[Ontology publication authority](ontology_publication_authority.md). This is a
candidate branch reference, not evidence that a deployment has represented
roles or released the capability.

**Current candidate as of 22 August 2026**: When internal MCP is enabled,
authenticated ordinary turns receive a bounded additive representation
aperture. Trusted actor, current-organisation, and namespace values are
server-bound. For `create_concepts`, the model may select actor-private,
current-organisation, or global publication within the actor's live semantic
authority; omission remains actor-private. A rejected wider scope is not
silently downgraded, and selecting a scope cannot enlarge the actor's authority.
Canonical relationships inherit their source concept's publication context;
an independently scoped relation-like fact uses the provenance-bearing scoped
assertion path. An invented canonical-relationship scope field fails without
mutation instead of being silently ignored. Optional organisation context does
not disable an otherwise actor-authorised user-scoped assertion capability:
payload organisation claims are hidden and cleared, then any current
organisation is recovered only from the already trusted ambient actor context.
Where authority is handed to a separately acting principal or crosses an
untrusted or sessionless boundary, the server issues an exact delegation only
after resolving the effect and rechecking that live authority; no additional
human confirmation is required.
Post-creation publication-scope changes use the governed preview-and-execute
path: user and organisation restrictions are independently added or removed,
and an exact one-user-plus-one-organisation composite requires authority over
both components. Legacy aliases, multiplicity, and malformed scope remain
mixed or historical rather than being treated as a canonical composite. Gmail
reads are projected only when a represented actor-to-profile relation
authorises a configured profile, and the model may choose only among the
actor-authorised stable resource selectors supplied by the trusted entry point.
Runtime aliases are resolved server-side and every Gmail handler rechecks the
selected profile against trusted invocation provenance and current represented
authority. Revalidate the catalogue and adaptive-turn service before relying
on this dated candidate implementation claim.

The Jira account-owner read path uses the authenticated actor and the configured
Atlassian account's unique, governed `#V#hasVonLoginEmail` identity binding.
Ordinary contact email, imported Jira person data and organisation membership
do not supply that binding. The server projects `jira_search`, `jira_get_issue`
and `jira_get_comments` with a hidden account selector only for that owner.
Jira retrieval rechecks the actual proxy account and current binding, including
direct workflow calls; a model-supplied selector or identity is not authority.
The account's Jira permissions determine its issue visibility. This bounded
owner path does not delegate the deployment account to other Von users or
release Jira writes. Explicit trusted-local operator access remains separate.
Missing or ambiguous identity binding fails closed, and a changed credential
configuration cannot silently reuse the previous account's proxy.

**Protection**:
- The ordinary-turn projection excludes undelegated effects; an operation's
  catalogue category alone does not establish authority
- Gateway handlers distinguish trusted ambient actor context from raw
  tool-payload identity claims
- Model-selected ontology scope is bounded by server-supplied actor/current-org
  identity and live semantic-role checks; it is not an identity or authority
  carrier
- Governed scope edits bind an optimistic fingerprint and exact component
  delta, preserve any unedited restriction, and require canonical read-back
- Actor-private RAG, chat, turn, experiment, and critique-memory reads validate
  or derive namespace from the trusted actor context
- Deployment-global diagnostics, host-local paths, connector accounts, and
  operator control-plane reads are excluded or source-constrained
- No namespace → error response (fail closed)

**Limitations**:
- Standalone trusted-local/developer MCP entry points are separate authority
  surfaces and may use operator-supplied scope. They are not part of the
  ordinary actor-scoped projection and must not be described as though every
  MCP route shared its identity model.
- Local stdio diagnostic reads use an explicit server-side operator allow-list
  in `mcp_stdio_server.py`. Existing actor contexts are preserved; supplied
  telemetry references retain their signed target checks. This does not grant
  operator provenance to workflow execution, recovery, or ontology mutation.
- External MCP servers (arXiv, future integrations) may not respect namespace
- No rate limiting on tool invocations
- No audit trail of tool access by user
- Untrusted content from web pages, PDFs, Jira, email, chat, or RAG can contain
  prompt-injection instructions
- LLM-driven tool use can turn prompt injection into excessive agency if tools
  are over-privileged

**Recommendations**:
- Add rate limiting per user/namespace
- Implement audit logging for sensitive tool calls (RAG access, concept mutations)
- Clearly document which MCP tools are namespace-aware vs global
- Treat external content as data from the least-privileged party that supplied
  it. Processing an untrusted PDF, web page, or email must not grant the LLM
  access to tools that the content's author should not have.
- Enforce the actor's maximum tool capability outside untrusted content and
  model output where bypass would expose undelegated data or effects. Do not
  turn that ceiling into deterministic semantic policy for every action.
- Expose the effect, scope, reversibility, recovery, external commitment, and
  relevant deployment limits of tools when those facts help the model and
  runtime choose proportionate assurance. Avoid a universal verb-based risk
  taxonomy.
- Apply the semantic read-only boundary in [`AGENTS.md`](../../AGENTS.md) to
  derived maintenance.
- Log enough tool input/output metadata to investigate suspicious tool use while
  redacting secrets and private content where required.

### 5. Jira (Atlassian) Authentication Diagnostics

Von’s internal Jira tools authenticate using Atlassian credentials from environment variables:

- `ATLASSIAN_BASE_URL` (e.g. `https://naoinstitute.atlassian.net`)
- `ATLASSIAN_EMAIL`
- `ATLASSIAN_API_TOKEN`

**Important (Windows gotcha):** editing `.env` does not necessarily update the environment seen by a running Von process. Von now applies targeted `.env` overrides for the Atlassian keys during startup (see `src/workflows/von/main.py`). If you update the token in `.env`, you still need to restart Von.

#### Debug tools

Use these tools to confirm what account Von is using and whether Jira auth is actually working.

1) `jira_get_auth_config` (no network call)
- Shows which `base_url` and `email` Von is configured with.
- Reports `token_present` and `token_length` only (never returns the token).

2) `jira_get_myself` (calls Jira `/rest/api/3/myself`)
- Returns identity details (`displayName`, `emailAddress`, `accountId`, etc.) when auth is valid.

#### Running the tools

These are internal tools available to Von’s chat/orchestrator. Run them with no arguments.

Examples (paste as plain text in a fenced block so the UI doesn’t mangle it):

```text
jira_get_auth_config
```

```text
jira_get_myself
```

If `jira_get_myself` returns `401 Unauthorised`, confirm the configured email/base URL with `jira_get_auth_config`, then rotate the API token (Atlassian API tokens are per-account) and restart Von.

### 6. Admin, Diagnostics, and Operator Endpoints

**Examples**:

- `/admin/rag_status`
- `/admin/rag_runtime`
- `/admin/rag_integrity`
- `/admin/rag_sync`
- `/admin/chat_history_backfill`
- `/admin/chat_history_reindex`
- `/admin/db/health`
- `/admin/db/reconnect`
- `/admin/policy_comparison`
- `/admin/workflow_materialisation_diagnostics`
- `/diag`

**Current**: Several diagnostics/admin endpoints are unauthenticated or only
partly gated, depending on route. Some are read-only diagnostics; some are
side-effecting operator actions.

**Risk**:

- Any client can discover system state from unauthenticated diagnostics.
- Some endpoints can trigger sync/reindex/reconnect work.
- Diagnostics can expose user/org identifiers, namespace shapes, model/cache
  state, helper-process state, or other information useful for attack planning.

**Mitigation (current SAIL/local profile)**:

- Local development can keep lightweight diagnostics available when bound to
  localhost or a trusted SAIL network.
- Side-effecting admin actions should still require authentication or an
  explicit local/admin token where practical.

**Recommendations**:
- Classify admin/diagnostic endpoints as:
  - public health (`/health`, minimal no-secret status only)
  - authenticated user diagnostics
  - authenticated SAIL/operator diagnostics
  - admin-only side effects
- Add `@require_admin` or equivalent policy decorators to admin-only endpoints
- Implement admin role checks via `access_control.py`
- Consider moving to dedicated admin API with separate authentication
- Keep `/diag` and diagnostic exports redacted, bounded, and disabled or
  authenticated outside local/dev.

### 7. Room Device Identity (Meeting-Room Voice)

**Purpose**: Support a server-authoritative identity for a shared “room device” (e.g., a meeting-room microphone/speaker) without trusting browser `localStorage`.

**Implementation**:
- Provisioning endpoint: `POST /admin/room_devices/provision`
    - Requires header `X-Admin-Token` matching env `VON_ADMIN_TOKEN`
    - Returns a one-time `device_secret`
- Device login endpoint: `POST /api/room_devices/login`
    - Exchanges `device_id` + `device_secret` for a bearer token
- Introspection endpoint: `GET /api/room_devices/whoami`
    - Requires `Authorization: Bearer <token>`

**Required configuration**:
- `VON_ROOM_DEVICE_TOKEN_KEY` (Fernet key) for issuing/verifying device tokens
- `VON_ADMIN_TOKEN` to protect provisioning operations

**Token rotation and revocation**:
- If you rotate `VON_ROOM_DEVICE_TOKEN_KEY`, previously issued room-device bearer tokens will no longer verify (global invalidation). This is the simplest “rotate-to-revoke-everything” mechanism.
- If you need rolling rotation (accept old + new keys during a transition), the token verifier would need to support a key ring (try multiple keys) until the cutover completes.
- The room-device records include a `token_revoked_before` field; the server can treat any token issued at or before that timestamp as revoked (per-device invalidation). There is no dedicated admin endpoint for this yet.

### 8. Backup Tooling Scope and Guardrails

**Context**: `run.ps1 backup` and `scripts/backup_von_db.py` can create full database dumps.

**Risk**:
- Backup operations are equivalent to broad data export.
- Repository-local backup output increases accidental sharing/commit risk.

**Current guardrails**:
- Manual backup action requires explicit opt-in: `VON_ENABLE_BACKUP_ACTION=1`.
- Scheduled daily backups require explicit host opt-in: `VON_ENABLE_DAILY_BACKUP=1`.
- Backup apply mode is blocked for repository-local output unless explicitly overridden with `VON_ALLOW_BACKUP_IN_REPO=1`.
- Scheduled daily backups use the same output-path safety check when enabled.
- Backup root defaults now prefer non-repository paths before any repo-local fallback.
- `.githooks/pre-commit.ps1` blocks staged files under `backups/`.

See `docs/engineering/backup_tooling_security.md` for detailed threat model and operational policy.

**Profile guidance**:

- `VON_ALLOW_BACKUP_IN_REPO=1` is local-dev-only and should never be used in a
  shared SAIL, partner, staging, or production environment.
- Partner/staging/production backups should be encrypted at rest, stored outside
  the repository, retained according to an explicit retention policy, and
  covered by restore drills.

### 9. Open Repository and Contributor Security

Von is public enough that repository-level security matters even while live use
is SAIL-only.

**Current protection**:

- `.env`, `data/`, `logs/`, `backups/`, local RAG stores, and Terraform state
  are ignored by git.
- PR template includes a "No secrets or runtime data committed" checklist item.
- GitHub secret scanning workflow exists via Gitleaks.

**Limitations**:

- No root `SECURITY.md` with a private vulnerability reporting path is currently
  visible in the repository.
- Security contact information in this document is still a placeholder.
- The open-source contribution guide still uses some Bash-first examples even
  though Von's agent/developer default is PowerShell.
- CodeQL, Dependabot, dependency review, OpenSSF Scorecard, and branch
  protection expectations are not documented here.

**Recommendations**:

- Add root `SECURITY.md` covering supported versions, private reporting, safe
  harbour expectations for good-faith testing, and what not to post publicly.
- Enable or document GitHub private vulnerability reporting.
- Add CodeQL/static analysis, Dependabot/dependency review, and OpenSSF
  Scorecard-style checks where available.
- Keep secrets out of issues, PRs, logs, screenshots, telemetry exports, and
  generated artefacts.
- Treat external PRs as untrusted code until reviewed; do not run arbitrary
  contributor code with SAIL secrets.

### 10. AI/Agent-Specific Security References

Von's security model should track mainstream LLM/agent security risks rather
than treating them as ordinary web-app bugs only.

Useful reference baselines:

- OWASP Top 10 for Large Language Model Applications:
  https://owasp.org/www-project-top-10-for-large-language-model-applications/
- OWASP GenAI Security Project:
  https://genai.owasp.org/
- NCSC, "Prompt injection is not SQL injection":
  https://www.ncsc.gov.uk/blog-post/prompt-injection-is-not-sql-injection
- NCSC Guidelines for Secure AI System Development:
  https://www.ncsc.gov.uk/collection/guidelines-secure-ai-system-development/introduction
- GitHub security policy guidance:
  https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/adding-a-security-policy-to-your-repository
- OpenSSF Scorecard:
  https://scorecard.dev/

## Security Checklist for Production Deployment

- [x] **Remove client-provided user_id fallback** in `von_routes.py` ✅ (Dec 2024)
- [x] **Require authentication** for all user-scoped endpoints ✅ (Dec 2024)
- [x] **Inform agent about authentication status** ✅ (Dec 2024)
- [x] **Publish authoritative effective namespace contract** (`docs/engineering/effective_namespace_contract.md`) ✅ (Feb 2026)
- [ ] **Publish root `SECURITY.md`** with supported versions, private reporting channel, and safe disclosure guidance
- [ ] **Define operating-profile policy switches** for local SAIL, shared SAIL, public contributor, partner pilot, and production modes
- [ ] **Create explicit public/read-only knowledge scope** for repo docs/sample corpora without granting private RAG access
- [ ] **Use one authoritative effective namespace resolver** (namespace + user/org components) across generate, MCP, persistence, and sync paths
- [ ] **Emit namespace-component provenance consistently** (`namespace_source`, user component, org component) for auditing and migration to stricter policy
- [ ] **Classify and protect admin/diagnostic endpoints** by public health, user diagnostics, operator diagnostics, and admin-only side effects
- [ ] **Restrict or replace trusted identity headers** (`X-User-Concept-ID`, `X-User-Client-ID`) outside local/trusted automation profiles
- [ ] **Implement audit logging** for RAG access and concept mutations
- [ ] **Add rate limiting** per user/namespace
- [ ] **Configure session timeouts** and rotation
- [ ] **Enable HTTPS** and secure cookie flags
- [ ] **Add database-level RLS** as defense in depth
- [ ] **Keep backup roots outside repository paths** in all production/staging environments
- [ ] **Require encrypted backups** with explicit retention and restore-drill policy for shared SAIL, partner, staging, and production deployments
- [ ] **Add AI/agentic security controls** for prompt injection, untrusted content privilege drop, tool-risk metadata, external MCP boundaries, and excessive agency
- [ ] **Add repository security automation** (CodeQL/static analysis, dependency review/Dependabot, OpenSSF Scorecard-style posture checks where available)
- [ ] **Penetration testing** for namespace isolation bypass attempts
- [ ] **Code review** of all user context derivation paths

## Testing Security

### Manual Tests

1. **Namespace Isolation Test**:
   ```python
   # Try to access another user's RAG data
   response = requests.post('/generate', json={
       'prompt': 'List my RAG sessions',
       'user_id': '#V#other_user'  # Try to impersonate
   })
   # Should fail with namespace_required or return only that user's data
   ```

2. **Unauthenticated RAG Access Test**:
   ```python
   # Try to query RAG without any user context
   response = requests.post('/generate', json={
       'prompt': 'Search my knowledge base'
       # No user_id provided
   })
   # Should fail with namespace_required error
   ```

3. **Admin Endpoint Access**:
   ```powershell
   curl http://localhost:5000/admin/rag_status
   # Local/dev may allow bounded diagnostics.
   # Shared/partner/production profiles should require the configured policy.
   ```

4. **Public Read-Only Corpus Test**:
   ```python
   # Query only an explicitly public corpus without login.
   response = requests.post('/generate', json={
       'prompt': 'Search the public Von docs for setup instructions'
   })
   # Should use only public/repo-doc scope, not private RAG or user namespaces.
   ```

5. **Trusted Header Misuse Test**:
   ```python
   response = requests.post('/generate',
       headers={'X-User-Concept-ID': '#V#other_user'},
       json={'prompt': 'List my private RAG sessions'}
   )
   # In partner/production mode, raw trusted headers should be rejected unless
   # supplied by an authenticated trusted proxy/service path.
   ```

6. **Prompt Injection / Tool Authority Test**:
   ```python
   # Upload or retrieve content containing instructions to ignore policy and call
   # a privileged tool.
   # Expected: the content is treated as untrusted data and cannot grant the LLM
   # tool authority beyond the caller/content party's privileges.
   ```

### Automated Security Tests

TODO: Implement security test suite covering:
- Cross-user data access attempts
- Unauthenticated access to protected resources
- Unauthenticated access to public/read-only repo docs/sample corpora
- Namespace component consistency (user-only vs user@org, and mismatch handling)
- Operating-profile policy switches for local/dev vs partner/production
- Session fixation/hijacking scenarios
- SQL injection in namespace filters
- XSS in chat responses
- Prompt-injection attempts through web/PDF/Jira/RAG content
- External MCP tool boundary and tool-risk metadata enforcement
- Admin/diagnostic route classification and auth policy
- Trusted identity header rejection outside local/trusted automation profiles

## Incident Response

If security breach suspected:

1. **Immediately**: Check logs for suspicious `user_id` switching patterns
2. **Audit**: Review `interaction_sessions` collection for namespace consistency
3. **Investigate**: Check admin endpoint access logs
4. **Remediate**: Rotate session secrets, invalidate all sessions
5. **Review**: Audit code for additional client-trust vulnerabilities
6. **Agentic trace review**: Inspect tool-use telemetry, prompt/context lineage,
   external content sources, and any workflow/Vontology mutations made during
   the suspected window.

## Security Contact

For now, report security issues privately to the Strong AI Lab maintainers. A
root `SECURITY.md` with the durable reporting path and disclosure policy must
be added before broader external contribution or partner deployment.

## Change Log

- **2026-09-02**: Documented the login-gated main browser experience
  - Made the authenticated server session authoritative for the Settings user
    identity instead of a browser-selected list
  - Recorded that actor-scoped home modules do not initialise while signed out
  - Made user-scoped recommendation routes fail closed and removed legacy
    enumerated or fabricated-user endpoints
  - Distinguished an unavailable authentication-status read from confirmed
    logout, retaining inert browser scope mirrors while the gate offers retry
  - Preserved organisation selection as a separate actor-bound scope choice
- **2026-08-22**: Documented model-selectable governed creation scope and
  independent publication-scope controls
  - Kept omitted ordinary-turn creation actor-private while allowing explicit
    organisation or global selection within server-bound identity and live
    semantic authority
  - Recognised exact one-user-plus-one-organisation publication as a canonical
    composite requiring authority over both components
  - Kept user-scoped assertion capabilities available without an organisation,
    while clearing payload organisation claims and using trusted ambient context
  - Made canonical relationship scope inheritance explicit and rejected ignored
    scope fields in favour of the scoped-assertion path
  - Kept legacy aliases, multiplicity, malformed scope, silent downgrade, and
    automatic scope ladders outside the supported boundary
- **2026-08-13**: Added the candidate boundary for canonical ontology
  publication authority
  - Distinguished organisation/global semantic roles from Von operational
    administration
  - Recorded exact short-lived agent delegation and receipt/read-back
    expectations for covered ontology effects
- **2026-07-27**: Distinguished logical read semantics from physical write
  purity and documented the bounded ordinary-turn representation-effect
  aperture
- **2026-07-25**: Replaced categorical write/destructive guardrails with
  delegated-capability and residual-risk guidance
  - Distinguished the actor's maximum capability from semantic action choice
  - Made credible reversibility, read-back, recovery, compensation, and blast
    radius part of control selection
  - Clarified that operation names and model-derived risk labels do not
    determine a universal approval policy
- **2026-07-26**: Updated ordinary MCP access guidance for the direct adaptive
  read path
  - Replaced the retired universal orchestrator description with the standing
    gateway capability projection
  - Documented trusted actor binding and represented Gmail profile injection
  - Distinguished actor-scoped reads from host/deployment/operator surfaces
- **2026-04-24**: Recalibrated security guidance for SAIL-only current use,
  open-repo contributor risk, and future partner/production profiles
  - Added operating profiles and data/capability classes
  - Clarified that validated identity headers are trusted automation/local
    integration only, not production authentication
  - Distinguished public/read-only knowledge access from private user/org RAG
  - Expanded admin/diagnostic endpoint risk beyond read-only RAG status
  - Added AI/agent-specific risks: prompt injection, excessive agency, external
    MCP boundaries, and untrusted-content privilege drop
  - Added open repository security expectations and reference baselines
- **2024-12-02**: Initial security documentation created (JVNAUTOSCI-760)
  - Documented client-provided user_id risk
  - Added namespace requirement to RAG tools
  - Added security warnings to code
- **2026-02-14**: Clarified namespace component security intent
  - Added guidance to treat namespace as structured user+organisation scope
  - Documented phased tightening approach (diagnose now, enforce later)
- **2026-02-23**: Published effective namespace contract (JVNAUTOSCI-1168)
  - Added canonical contract document for requested/session/effective/storage namespaces
  - Linked contract from security guidance for phased enforcement
