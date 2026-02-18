# Security Considerations for Von (Research Prototype)

## Critical Security Context

**Von is currently a RESEARCH PROTOTYPE designed for single-user or trusted-user scenarios.** It is NOT production-ready for multi-tenant or adversarial environments.

## Known Security Limitations

### 1. Authentication Required (SECURE - Implemented December 2024)

**Location**: `src/backend/server/routes/von_routes.py` - `/generate` endpoint

**Status**: ✅ **SECURE** - Client-provided user IDs are now **rejected**

**Implementation**: The system requires proper authentication via:
1. **Server-side session** (populated during login flow via `/api/auth/google/login`)
2. **Validated headers** (`X-User-Concept-ID`) with person concept verification

**Behaviour**:
```python
# SECURITY: Do NOT trust client-provided user_id - require proper authentication
user_concept_id = get_effective_user_concept_id()
if not user_concept_id:
    # Client-provided user_id is IGNORED for security
    # RAG tools will return "namespace_required" error
```

**Unauthenticated User Experience**:
- ✅ Can still use chat interface (no RAG access)
- ✅ Agent is **informed** about authentication status
- ✅ Agent explains RAG tools require login
- ✅ No access to user-scoped data (RAG, sessions, history)
- ✅ Clear error messages guide user to log in

**Agent Transparency**:
The agent receives authentication status in system prompt:
- **Authenticated**: "🔐 AUTHENTICATION STATUS: Authenticated (namespace: #V#user)"
- **Unauthenticated**: "⚠️ AUTHENTICATION STATUS: NOT AUTHENTICATED - RAG tools unavailable"

### 2. Namespace-Based Isolation

**Approach**: User data isolation implemented via `namespace` field filtering in database queries.

**Current Protection**:
- All RAG tools (`rag_list_indexed`, `rag_get_item`, `search_knowledge_base`) **require** namespace parameter
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

**Intent**: Namespace is not only a storage partition key. It is intended to become a primary access-safety primitive and will interact with future microtheory/theory-inclusion work.

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

**Tightening path (later)**:
- During the current experimental phase, prefer visibility and diagnostics for non-critical mismatches over aggressive hard failures.
- Keep one authoritative effective-namespace resolver and one validation pathway so stricter enforcement can be enabled later with minimal refactoring.
- Design new handlers so switching from “diagnose mismatch” to “reject mismatch” is a policy change, not a codebase-wide rewrite.

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

### 4. MCP Tool Access Control

**Current**: Internal MCP tools injectable with user namespace via orchestrator.

**Protection**:
- Orchestrator injects `user_namespace` into tool payloads if authenticated
- RAG tools validate namespace presence before query execution
- No namespace → error response (fail closed)

**Limitations**:
- External MCP servers (arXiv, future integrations) may not respect namespace
- No rate limiting on tool invocations
- No audit trail of tool access by user

**Recommendations**:
- Add rate limiting per user/namespace
- Implement audit logging for sensitive tool calls (RAG access, concept mutations)
- Clearly document which MCP tools are namespace-aware vs global

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

### 6. Admin Endpoints

**Endpoints**: `/admin/rag_status`, `/admin/rag_integrity`, `/admin/rag_sync`

**Current**: Accept `?namespace=` query parameter but no authentication required.

**Risk**: Any client can query admin endpoints to discover system state.

**Mitigation**:
- These are read-only status endpoints (limited risk)
- Should add admin authentication before production deployment

**Recommendations**:
- Add `@require_admin` decorator to admin endpoints
- Implement admin role checks via `access_control.py`
- Consider moving to dedicated admin API with separate authentication

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
- Backup apply mode is blocked for repository-local output unless explicitly overridden with `VON_ALLOW_BACKUP_IN_REPO=1`.
- Scheduled daily backups use the same output-path safety check.
- Backup root defaults now prefer non-repository paths before any repo-local fallback.
- `.githooks/pre-commit.ps1` blocks staged files under `backups/`.

See `docs/engineering/backup_tooling_security.md` for detailed threat model and operational policy.

## Security Checklist for Production Deployment

- [x] **Remove client-provided user_id fallback** in `von_routes.py` ✅ (Dec 2024)
- [x] **Require authentication** for all user-scoped endpoints ✅ (Dec 2024)
- [x] **Inform agent about authentication status** ✅ (Dec 2024)
- [ ] **Use one authoritative effective namespace resolver** (namespace + user/org components) across generate, MCP, persistence, and sync paths
- [ ] **Emit namespace-component provenance consistently** (`namespace_source`, user component, org component) for auditing and migration to stricter policy
- [ ] **Add admin authentication** to `/admin/*` endpoints
- [ ] **Implement audit logging** for RAG access and concept mutations
- [ ] **Add rate limiting** per user/namespace
- [ ] **Configure session timeouts** and rotation
- [ ] **Enable HTTPS** and secure cookie flags
- [ ] **Add database-level RLS** as defense in depth
- [ ] **Keep backup roots outside repository paths** in all production/staging environments
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
   ```bash
   curl http://localhost:5000/admin/rag_status
   # Should require admin authentication in production
   ```

### Automated Security Tests

TODO: Implement security test suite covering:
- Cross-user data access attempts
- Unauthenticated access to protected resources
- Namespace component consistency (user-only vs user@org, and mismatch handling)
- Session fixation/hijacking scenarios
- SQL injection in namespace filters
- XSS in chat responses

## Incident Response

If security breach suspected:

1. **Immediately**: Check logs for suspicious `user_id` switching patterns
2. **Audit**: Review `interaction_sessions` collection for namespace consistency
3. **Investigate**: Check admin endpoint access logs
4. **Remediate**: Rotate session secrets, invalidate all sessions
5. **Review**: Audit code for additional client-trust vulnerabilities

## Security Contact

For security issues, contact: [Add security contact information]

## Change Log

- **2024-12-02**: Initial security documentation created (JVNAUTOSCI-760)
  - Documented client-provided user_id risk
  - Added namespace requirement to RAG tools
  - Added security warnings to code
- **2026-02-14**: Clarified namespace component security intent
  - Added guidance to treat namespace as structured user+organisation scope
  - Documented phased tightening approach (diagnose now, enforce later)
