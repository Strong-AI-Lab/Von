# Access Control and Namespaces (Vontology + RAG)

This document describes **how access control and namespace isolation currently work in Von**, based on code paths in the repository. It is written for engineering clarity ("what actually happens").

Von is a **research prototype**. The mechanisms here are primarily about preventing accidental cross-user data access in trusted-user scenarios, not hard multi-tenant security.

Related work items:
- JVNAUTOSCI-144 (Epic): parent epic for doc representation + RAG work.
- JVNAUTOSCI-760 (Task): namespaces and access control groundwork (includes multiple subtasks).
- JVNAUTOSCI-836 (Task): refine/clarify the model (effective namespace, document representation, etc.).
- JVNAUTOSCI-1168 (Task): authoritative effective namespace contract + phased enforcement guidance.
- JVNAUTOSCI-1169 (Task): implementation alignment across generate, persistence, and RAG retrieval.

Authoritative namespace contract:
- `docs/engineering/effective_namespace_contract.md`

## Key terms

- **Authenticated user**: A user recognised by the backend (Flask session) as a specific Vontology person/user concept.
- **Effective user**: The user identity the backend actually uses for access control. This is server-authoritative.
- **Organisation**: A Vontology concept representing an organisation; membership and roles may be stored in the Vontology.
- **Namespace**: A string used to partition RAG-indexed content and (in some flows) chat session history. Namespaces are used as a query filter.

## Executive summary (as implemented)

- Vontology concept visibility is filtered by canonical Vontology predicates under `relationships.#V#specific_to_user` and `relationships.#V#specific_to_organisation`.
- Legacy storage keys such as `relationships.specific_to_user` and `relationships.specific_to_org` remain readable only for migration compatibility; new writes should use the canonical predicates.
- Google OAuth identity is bound to a Vontology user only through the narrow
  `#V#hasVonLoginEmail` predicate. Ordinary `#V#has_email` contact facts and
  client/session hints cannot select or create an authenticated actor.
- Effective user identity comes from the **server session** (fallback:
  validated `X-User-Concept-ID` header), not from client JSON.
- RAG tools are **fail-closed** without a namespace.
- RAG query backends (e.g., LlamaIndex) can additionally filter by metadata keys like `user_id` and `organisation_concept_id`, but only if those keys are provided via `permissions_context`.
- Effective namespace resolution is now centralised for `/von/generate` and propagated into persistence and RAG paths, with mismatch diagnostics available for migration hardening.

## 1) Identity: where user context comes from

### Source of truth

The authoritative identity is derived by the backend.

- `src/backend/services/von_user_authentication_service.py` resolves Google
  OAuth email addresses only through `#V#hasVonLoginEmail`.
  - The predicate specialises and entails descriptive `#V#has_email`.
  - The reverse inference is forbidden: `#V#has_email` never grants login.
  - One user may have several explicit login-email values, but an address bound
    to more than one user is rejected as ambiguous.
  - Organisation admins and owners can inspect a current member's narrow login
    bindings through the dedicated internal capability. Binding and removal are
    idempotent, receipted, and canonically read back. A bind request, including
    certification that an existing binding is globally unambiguous, and any
    removal that would change a multi-organisation person's global login
    identity require management authority across all of that person's
    represented organisations (or the separate Von operational-administrator
    role).
  - This capability does not widen generic concept visibility, promote a
    descriptive `#V#has_email` value, disclose a conflicting person's identity,
    or revoke an existing browser session when a binding is removed.
  - An unbound address does not inherit the browser's prior user concept and
    does not auto-create a new Von user.
  - Sessions issued through the old email-resolution path do not carry the
    versioned login-assurance marker and fail closed after the cutover; users
    must authenticate again.
- `src/backend/security/access_control.py` provides `get_effective_user_concept_id()`.
  - Primary: `session["user_concept_id"]` (set on login).
  - Fallback: `X-User-Concept-ID` header, **only if validated** as a person/von_user concept.
  - If neither exists/valid, the request is treated as unauthenticated.

### Important security property

The `/generate` endpoint explicitly does **not** trust client-provided `user_id` for security reasons. Unauthenticated users can still chat, but RAG access is unavailable.

See: docs/engineering/security_considerations.md.

### Operator migration and recovery

The login-email migration normally accepts only an explicit reviewed
allow-list. For a database being upgraded from the pre-cutover runtime, an
explicit compatibility mode copies the unambiguous `#V#has_email` bindings
that the old login reader already treated as authority. This is retrospective
only; later ordinary contact-email assertions remain non-authoritative:

```sh
python -m src.backend.utilities.migrate_von_login_email_bindings \
  --binding '#V#person_id=person@example.org'

python -m src.backend.utilities.migrate_von_login_email_bindings \
  --binding '#V#person_id=person@example.org' --apply --approved

python -m src.backend.utilities.migrate_von_login_email_bindings \
  --preserve-pre-cutover-authority

python -m src.backend.utilities.migrate_von_login_email_bindings \
  --preserve-pre-cutover-authority --apply --approved
```

Run the relevant command without `--apply --approved` before applying. If it
reports a missing user or an address already bound to another user, correct
that exact conflict and rerun the dry run; do not work around it by adding a
generic email relation.

The organisation-membership migration likewise performs a dry run by default.
Its pre-cutover compatibility mode copies every legacy membership pair that the
old reader accepted, retaining the old `member` default where no role was
stored. If it reports conflicting legacy roles, resolve each pair explicitly
with the exact `--role-override` form shown by `--help`, for example:

```sh
python -m src.backend.utilities.migrate_von_organisation_membership_predicates \
  --preserve-pre-cutover-authority \
  --role-override '#V#person_id=#V#organisation_id=owner'

python -m src.backend.utilities.migrate_von_organisation_membership_predicates \
  --preserve-pre-cutover-authority \
  --role-override '#V#person_id=#V#organisation_id=owner' \
  --apply --approved
```

The selected role must already occur in the legacy evidence for that exact
user/organisation pair. Unrelated overrides and invented roles fail closed.

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

### Workflow authority, discovery, and process-global caches

Workflow definitions and their discovery metadata are Vontology-governed
concept data, so the same visibility rules apply to them. A process-global
workflow registry, capability index, or discovery memo is a performance surface,
not an access authority.

For actor-scoped workflow operations:

- bind and validate the authenticated actor or canonical namespace before any
  memo-cache lookup or capability result projection;
- treat memoised and durable-carried workflow-discovery payloads as evidence,
  not authority: re-project candidate, routing, and readiness metadata through
  current actor visibility on every reuse, and fail closed if that visibility
  authority is unavailable;
- filter workflow IDs before purpose, description, routing, executability, or
  readiness metadata is returned;
- load the complete workflow graph through the actor-scoped Vontology loader,
  even if another actor has already warmed a shared registry entry (including
  an entry registered by just-in-time discovery rather than normal startup);
- keep actor-scoped definitions request-local rather than promoting them into an
  unpartitioned process-global registry; and
- carry the persisted user, organisation, and namespace into background durable
  worker definition loading and nested workflow resolution;
- treat workflow-launch `user_id`, `org_id`, and `namespace` values as claims,
  not authentication authority: authenticated or parent-workflow actor context
  wins, contradictory claims fail closed, and payload-only fallback is allowed
  only on an explicitly trusted local-operator gateway; and
- apply the same actor envelope to workflow instances, schedules, streams,
  traces, use episodes, prediction summaries, and Studio detail reads. Legacy
  user-only telemetry cannot prove present organisation ownership and therefore
  fails closed on an organisation-scoped read. A stream's subscription-time
  visible-ID set is only an upper bound: current visibility is checked again
  before each event so revocation takes effect without reconnecting.

Apply one coherent visibility envelope to a restricted workflow root and every
restricted graph node needed to execute it. A visible root with inaccessible
required steps is an incomplete graph, while a restricted root whose metadata
is emitted from a global index is an information leak. Both should fail closed
with typed, metadata-free diagnostics for actors outside the intended scope.

Process-global workflow control-plane records need stronger authority than
workflow visibility. Event bindings currently have no tenant owner and one
binding affects every actor, while materialisation/parity/health reports expose
global environment and registry state. Their internal MCP surfaces are therefore
trusted-operator-only. Do not infer permission to inspect or mutate them merely
because an authenticated actor can execute the referenced workflow. An eventual
actor-managed event-binding feature must first add represented ownership and
tenant-scoped persistence/runtime lookup.

The same temporary operator boundary applies to experiment-run control records
and raw turn/chat diagnostic MCP reads. Those stores do not yet enforce one
complete actor-ownership contract at every read and mutation boundary, and they
can contain restricted workflow identifiers. Raw stdio payload identity is not
authority. Actor-owned access should be restored only with canonical persisted
user, organisation, and namespace ownership plus current workflow-visibility
projection; trusted operational certification remains available through the
explicit operator gateway.

#### Rollout requirements for actor-scoped workflow authority

Deployments enabling these boundaries must invalidate or require
re-authentication of sessions created before the canonical actor-context fix.
An old session can otherwise retain an identity or organisation claim that was
established under weaker account-switch semantics.

At the HTTP boundary, the ingress proxy must strip client-supplied identity and
operator headers and set any permitted replacements itself after authentication.
Application support for a validated identity header is not evidence that an
internet client may assert that header directly. Keep the operator token out of
browser-delivered configuration and rotate it through the deployment secret
path.

Workflow Studio remains a trusted-pilot authoring surface until represented
editor/owner permissions and immediate membership-revocation invalidation are
implemented. Workflow visibility alone grants read/execution eligibility; it
must not be treated as a general authoring role.

## 3) Roles and permissions (RBAC)

### Visibility is not semantic publication authority

Concept visibility answers whether a caller may discover or read a concept. It
does not authorise canonical publication, retraction, identity consolidation,
or a change to its user/organisation/global publication scope. Those effects
need a separate semantic-authority decision against the relevant publication
context; `#V#von_administrator` is operational administration, not global
semantic authority. The candidate boundary and its delegation/receipt
requirements are recorded in
[Ontology publication authority](ontology_publication_authority.md).

### Current status

RBAC is present but **not a complete system** yet.

- `src/backend/security/role_resolver.py` contains a Phase 1 stub mapping (hardcoded user/org → role → permissions).
- `src/backend/services/organisation_membership_service.py` persists operational
  membership and roles in the Vontology:
  - authority-bearing membership relationship: `#V#memberOfVonOrg`
  - operational role: `#V#hasVonOrgRole`, with context containing the exact
    organisation ID
  - `#V#memberOfVonOrg` specialises and entails the descriptive
    `#V#memberOf` predicate; generic `memberOf`, `member_of_organisation`, and
    `hasRole` assertions never grant Von access or an operational role

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

The `/generate` route resolves one effective namespace per request and passes it into orchestrator/tool paths as `user_namespace`.

- Resolution combines effective window/flask context plus derived fallbacks.
- Org-scoped namespaces are preferred when org context is available.
- Resolver diagnostics (`namespace_report`) capture candidate sources and mismatch signals.

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

### Permissions context wiring

Internal tool handlers build `permissions_context` from effective request/session values.

Practical consequence:
- Metadata filtering can include both `user_id` and `organisation_concept_id` when those fields are propagated consistently.

Residual risk:
- Any path that bypasses effective-context propagation can still reduce organisation-level filtering quality; keep namespace provenance visible for triage.

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
- **Effective namespace**: what the backend resolves and uses for the current request.
- **Storage namespace**: what is persisted on `interaction_sessions.namespace` and used for listing/getting.

These can still diverge in legacy and edge-case flows. The contract direction is: compute one authoritative effective namespace per request and use it consistently for:
- RAG tool invocations
- interaction session writes
- RAG sync

Canonical details are defined in:
- `docs/engineering/effective_namespace_contract.md`

## 8) Alignment status and remaining work (engineering)

Completed:
1. `/generate` effective namespace alignment with session/window context, plus mismatch diagnostics (JVNAUTOSCI-1169).
2. Propagation into chat history persistence and RAG retrieval/indexing paths for consistent namespace use (JVNAUTOSCI-1169).
3. Authoritative contract publication and migration plan (JVNAUTOSCI-1168).

Remaining:
1. Continue removing legacy namespace defaults where user-scoped context is required.
2. Expand strict mismatch enforcement to additional high-risk entry points once diagnostics indicate safe rollout.
3. Keep provenance stamping comprehensive for persisted artefacts and tool outputs.

Preferred provenance fields:
  - When Von persists objects that may later be surfaced to an agent or UI (e.g., indexed sessions, RAG chunks, summaries, tool outputs, sync markers), include lightweight provenance fields.
  - Suggested minimum set:
    - `item_kind` (what type of thing this is)
    - `source_system` (what created it: DB collection, tool, pipeline, backend)
    - `namespace` and `namespace_source` (how/why it was scoped)
  - Rationale: this will matter when we add **projection of persisted objects into a virtual part of the Vontology** so the system can do coherent introspection (“what do you have stored and why?”) without confusing chat history, KA sessions, and retrieved RAG content.

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
