# Access Control and Namespaces (Vontology + RAG)

This document describes **how access control and namespace isolation currently work in Von**, based on code paths in the repository. It is written for engineering clarity ("what actually happens") and highlights a few known mismatches between intended and implemented behaviour.

Von is a **research prototype**. The mechanisms here are primarily about preventing accidental cross-user data access in trusted-user scenarios, not hard multi-tenant security.

Related work items:
- JVNAUTOSCI-144 (Epic): parent epic for doc representation + RAG work.
- JVNAUTOSCI-760 (Task): namespaces and access control groundwork (includes multiple subtasks).
- JVNAUTOSCI-836 (Task): refine/clarify the model (effective namespace, document representation, etc.).

## Key terms

- **Authenticated user**: A user recognised by the backend (Flask session) as a specific Vontology person/user concept.
- **Effective user**: The user identity the backend actually uses for access control. This is server-authoritative.
- **Organisation**: A Vontology concept representing an organisation; membership and roles may be stored in the Vontology.
- **Namespace**: A string used to partition RAG-indexed content and (in some flows) chat session history. Namespaces are used as a query filter.

## Executive summary (as implemented)

- Vontology concept visibility is filtered by `relationships.specific_to_user` and `relationships.specific_to_org`.
- User identity comes from the **server session** (fallback: validated `X-User-Concept-ID` header), not from client JSON.
- RAG tools are **fail-closed** without a namespace.
- RAG query backends (e.g., LlamaIndex) can additionally filter by metadata keys like `user_id` and `organisation_concept_id`, but only if those keys are provided via `permissions_context`.
- There are currently **multiple namespace sources** (user-only namespace vs user@org namespace) that are not yet fully unified.

## 1) Identity: where user context comes from

### Source of truth

The authoritative identity is derived by the backend.

- `src/backend/security/access_control.py` provides `get_effective_user_concept_id()`.
  - Primary: `session["user_concept_id"]` (set on login).
  - Fallback: `X-User-Concept-ID` header, **only if validated** as a person/von_user concept.
  - If neither exists/valid, the request is treated as unauthenticated.

### Important security property

The `/generate` endpoint explicitly does **not** trust client-provided `user_id` for security reasons. Unauthenticated users can still chat, but RAG access is unavailable.

See: docs/engineering/security_considerations.md.

## 2) Vontology access control (concepts + relationships)

### Visibility model

Vontology concepts are filtered using a visibility query applied to MongoDB queries/pipelines.

- `src/backend/security/access_control.py`:
  - `build_visibility_filter(user_concept_id, organisation_concept_id)` constructs a Mongo query that:
    - allows concepts not scoped to any user/org
    - allows concepts scoped to the authenticated user
    - allows concepts scoped to the selected organisation (if present)
  - `apply_concept_query_filter()` and `apply_pipeline_filter()` apply the filter.

### Relationship sanitisation

Even when a concept is visible, some of its outgoing relationship values may point at concept IDs the user cannot access.

- `sanitize_concept_document()` prunes relationship value lists down to concept IDs that pass access checks.

Practical consequence:
- The UI or tools may see a concept but with fewer relationship targets than exist globally.

### Organisation context

Organisation scoping depends on what the backend considers the active organisation.

- `access_control.py` reads organisation context from the Flask session (e.g., `session["organisation_concept_id"]` in the routes that set it).

## 3) Roles and permissions (RBAC)

### Current status

RBAC is present but **not a complete system** yet.

- `src/backend/security/role_resolver.py` contains a Phase 1 stub mapping (hardcoded user/org → role → permissions).
- `src/backend/services/organisation_membership_service.py` persists membership and roles in the Vontology:
  - membership relationship: `memberOf`
  - role stored via a text relation predicate like `#V#hasRole` with a context containing the organisation ID

Practical consequence:
- Some code paths can read role/permissions from stubs, while the Vontology contains a richer representation that is not consistently used everywhere.

## 4) Namespaces: derivation and intended meaning

### Namespace formats

The namespace service supports:
- user-only namespace: `#V#<user>`
- user@org namespace: `#V#<user>@<org>`

See:
- `src/backend/services/namespace_service.py` (`derive_namespace`, parsing/normalisation/validation helpers).

### How namespaces are set in the session

Organisation selection routes derive and store a composite namespace in session.

- `src/backend/server/routes/von_routes.py` includes endpoints like `/api/session/set_organisation` and `/api/session/context`.
  - These set or return:
    - `session["organisation_concept_id"]`
    - `session["role_in_org"]`
    - `session["namespace"]` (often `#V#user@org`)

### How namespaces are passed into tool execution

The `/generate` route currently derives a namespace from the authenticated user concept ID and passes that into the orchestrator as `user_namespace`.

- Key behavioural point: **this is currently user-only**, and may not incorporate `session["namespace"]` (user@org) even when an organisation is selected.

This is one of the main “model mismatch” items tracked by JVNAUTOSCI-836.

## 5) RAG tools: namespace isolation and metadata filtering

### Fail-closed namespace requirement

The internal MCP RAG tools (used by the chat orchestrator) require a namespace.

- `src/backend/integrations/internal_mcp/catalogue.py`:
  - RAG list/get/search handlers check for a namespace; if missing, they return an error such as `namespace_required`.
  - list/get typically filter MongoDB `interaction_sessions` by `namespace` (and often `indexing_status`).

### RAG backend filtering

The backend that actually performs semantic search can apply additional filtering.

Example: LlamaIndex backend
- `src/backend/services/rag_backends/llamaindex_backend.py`:
  - If `permissions_context` includes `user_id`, it adds a metadata filter `user_id == ...`.
  - If `permissions_context` includes `organisation_concept_id`, it adds a metadata filter `organisation_concept_id == ...`.

Important implication:
- Namespace filtering alone partitions content, but **metadata filtering is an additional guard** when it is correctly populated.

### Permissions context wiring (known mismatch)

Some internal tool handlers build a `permissions_context` from Flask session values. There is at least one known inconsistency where the code checks `org_id` rather than `organisation_concept_id`.

Practical consequence:
- The LlamaIndex metadata filter may be applied for user_id but not for organisation, even when an organisation is selected.

(Recommendation is in the “Alignment tasks” section below.)

## 6) RAG indexing / sync: what gets stored

Indexed chat sessions are stored in MongoDB (`interaction_sessions`) and are synchronised into the vector store.

- `src/backend/services/rag_sync_service.py`:
  - includes org/role metadata when present (e.g., `organisation_concept_id`, `role_in_org`).
  - has a default namespace in some flows (e.g., `chat_history`) if a session does not have one.

Practical consequence:
- There can be legacy or backfilled sessions with a namespace that does not match the current “effective namespace” derivation rules.
- Backfills exist to populate namespaces for older sessions.

## 7) The “requested vs effective namespace” mental model

It helps to distinguish:

- **Requested namespace**: what the UI or request body might imply (e.g., org selected in local storage).
- **Session namespace**: what backend session endpoints have stored (e.g., `#V#user@org`).
- **Effective namespace**: what the backend actually passes into tool calls (currently often derived from user concept ID only).
- **Storage namespace**: what is persisted on `interaction_sessions.namespace` and used for listing/getting.

Today, these can diverge. JVNAUTOSCI-836’s “effective namespace resolver” work is the right direction: compute one authoritative effective namespace per request and use it consistently for:
- RAG tool invocations
- interaction session writes
- RAG sync

## 8) Recommended alignment tasks (engineering)

These are concrete changes that would reduce confusion and tighten isolation:

1. Make `/generate` use the same effective namespace source as `/api/session/context`.
   - If `session["namespace"]` is present, prefer it over user-only derivation.

2. Standardise session keys for organisation.
   - Ensure internal tool `permissions_context` uses `organisation_concept_id` consistently (not `org_id`).

3. Ensure RAG sync defaults match the effective namespace model.
   - Avoid “chat_history” as a silent default unless it is explicitly the intended global namespace.

4. Document the invariants.
   - “No namespace → no RAG access.”
   - “Namespace used for list/get must match namespace used for indexing/sync.”

## 9) Jira relationship tidy-up (760 / 836 / 144)

Current state (as observed):
- JVNAUTOSCI-760 and JVNAUTOSCI-836 are linked as **Relates**.
- Both are under epic JVNAUTOSCI-144.

Suggested tidy structure:
- Keep both as tasks under JVNAUTOSCI-144 (that part is already clean).
- Replace or supplement “Relates” with a directional dependency that reflects actual execution order:
  - If JVNAUTOSCI-836 is the architectural consolidation needed to finish remaining 760 subtasks cleanly, set **JVNAUTOSCI-760 blocks JVNAUTOSCI-836** or vice versa (choose the direction that matches real sequencing).
  - If 836 is the “model + refactor” that enables reliable org-scoped namespaces, consider: **JVNAUTOSCI-760 blocks JVNAUTOSCI-836** only if 760 must land first; otherwise **JVNAUTOSCI-836 blocks JVNAUTOSCI-760** for any remaining namespace-sensitive subtasks.

Rule of thumb:
- Use **Relates** for thematic association.
- Use **Blocks/Depends on** when it changes what can be shipped next.

## Appendix: primary code touchpoints

- Identity + concept visibility filtering: `src/backend/security/access_control.py`
- Namespace derivation: `src/backend/services/namespace_service.py`
- Stub RBAC: `src/backend/security/role_resolver.py`
- Organisation membership persistence: `src/backend/services/organisation_membership_service.py`
- Chat/generate route + session org selection: `src/backend/server/routes/von_routes.py`
- Internal MCP RAG tools: `src/backend/integrations/internal_mcp/catalogue.py`
- RAG sync: `src/backend/services/rag_sync_service.py`
- RAG backend metadata filtering: `src/backend/services/rag_backends/llamaindex_backend.py`
