# Effective Namespace Contract (User + Organisation)

This document defines the authoritative contract for effective namespace handling in Von.
It is the canonical reference for namespace semantics across generate, persistence, and RAG retrieval/indexing paths.

Status:
- Normative contract approved for implementation and migration planning (JVNAUTOSCI-1168).
- Current implementation alignment work landed in JVNAUTOSCI-1169.

## 1) Scope and intent

This contract exists to keep user/org isolation predictable while allowing phased hardening.

Scope:
- Request-time namespace resolution.
- Namespace normalisation/parsing rules.
- Propagation fields required across services/tools.
- Mismatch policy modes (`diagnose/warn` vs `reject mismatch`).

Out of scope:
- Database-level RLS/ACL design.
- Full RBAC/authorisation model.
- Backfill policy for every historical record.

## 2) Canonical terms

- Requested namespace:
  - Namespace supplied by caller context (UI/tool payload/session hints).
- Session namespace:
  - Namespace stored in effective backend context (`window_session` or Flask session).
- Effective namespace:
  - Single namespace selected by server-side resolver for the current operation.
- Storage namespace:
  - Namespace persisted on indexed artefacts and used for list/get filters.

## 3) Namespace format and normalisation rules

Canonical forms:
- User-only: `#V#<user_slug>`
- User+org: `#V#<user_slug>@<org_slug>`

Required rules:
1. Prefix must be canonicalised to `#V#`.
2. User and org slugs must be derived via shared helpers, not ad-hoc string concatenation.
3. Namespace parsing/validation should use `namespace_service` (`derive_namespace`, `parse_namespace`, `normalize_namespace`) where feasible.
4. Org scope must be preserved when present; no silent downgrade from `#V#user@org` to `#V#user` in downstream persistence/RAG paths.

Practical derivation rule for concept IDs:
- Strip `#V#` prefix.
- Remove trailing scope suffixes where relevant (`@...`, `+...`) before slugging.
- Slug to `[a-z0-9_]+`.

## 4) Authoritative resolution contract

### 4.1 Generate path (`/von/generate`)

Resolver inputs (ordered candidates):
1. `effective_context.namespace`
2. derived from `effective_context.organisation_id`
3. `flask_session.namespace`
4. derived from `flask_session.organisation_concept_id` / `org_id`
5. derived from `user_concept_id` only

Selection rule:
- Prefer org-scoped candidates if any are available.
- Otherwise select the first valid candidate.

Diagnostics required:
- `namespace_report.namespace`
- `namespace_report.namespace_source`
- `namespace_report.mismatch_detected`
- `namespace_report.candidates`
- `namespace_report.effective_context_source`
- `namespace_report.effective_context_namespace`
- `namespace_report.session_namespace`

### 4.2 RAG MCP path (`catalogue.py`)

Resolver inputs:
1. Explicit `namespace` payload (normalised)
2. Derived namespace from user/org hints (`user_concept_id`/`user_id` + `organisation_concept_id`/`org_id`)
3. `VON_DEFAULT_NAMESPACE` fallback (local/dev convenience)

Conflict rule:
- If explicit and derived namespaces disagree, this is a mismatch condition.

Required provenance fields:
- `namespace`
- `namespace_source`
- `namespace_resolution_note`
- `namespace_mismatch`
- `derived_user_concept_id`
- `derived_organisation_concept_id`

## 5) Propagation contract across services/tools

The following fields should be propagated together where practical:
- `namespace`
- `namespace_source`
- `user_concept_id`
- `organisation_concept_id`

Recommended additional context:
- `role_in_org`
- request/session correlation IDs

Persistence alignment requirement:
- The namespace selected for request execution must be the namespace used for:
  - chat history turn persistence
  - interaction-session persistence
  - RAG sync/index metadata
  - RAG list/get/search filters

## 6) Policy modes (phased enforcement)

### Experimental mode (`diagnose/warn`)

Purpose:
- Maximise observability while reducing accidental behavioural changes.

Behaviour:
- Continue execution for non-critical mismatch states.
- Emit mismatch diagnostics and logs.
- Preserve selected effective namespace and provenance fields.

### Strict mode (`reject mismatch`)

Purpose:
- Enforce hard isolation guarantees where ambiguity is not acceptable.

Behaviour:
- Fail closed on mismatch between explicit and derived namespace contexts.
- Return explicit machine-readable errors (`namespace_mismatch`, `namespace_required`).
- Do not execute user-scoped operation on mismatch.

### Current application matrix (2026-02-23)

- `/von/generate` resolution:
  - Experimental-style mismatch handling (diagnostics and warnings).
- `chat_history_service.add_message_to_history`:
  - Uses resolved namespace/org/role overrides; does not introduce a second conflicting resolver.
- RAG MCP retrieval/indexing tools:
  - Strict mismatch rejection for explicit-vs-derived conflicts.

## 7) Invariants

1. No authenticated namespace context means no user-scoped RAG access.
2. Namespace provenance must be observable (`namespace_source` at minimum).
3. User/org component context must not be discarded when present.
4. Effective namespace used for retrieval must match namespace used for persistence/indexing within the same execution path.
5. Namespace derivation must remain server-authoritative (do not trust raw client identity payloads).

## 8) Non-goals

1. Enforcing strict mode on every endpoint immediately.
2. Retrofitting every historical record before shipping incremental improvements.
3. Encoding role semantics into namespace strings.
4. Replacing broader access-control mechanisms with namespace alone.

## 9) Migration path

Phase A (completed):
- Align generate/persistence/RAG handling around one effective namespace selection path (JVNAUTOSCI-1169).
- Add resolver provenance and mismatch diagnostics.

Phase B (this contract, completed):
- Publish this authoritative contract and link it from security/access-control docs (JVNAUTOSCI-1168).
- Make enforcement modes and invariants explicit.

Phase C (planned):
- Introduce one explicit policy switch for mode selection (experimental vs strict) across remaining namespace-sensitive entry points.
- Expand strict mismatch rejection consistently where required by risk level.

Phase D (planned):
- Reduce legacy fallback behaviour where not needed.
- Add targeted audits/backfills for legacy namespace inconsistencies.

## 10) Primary implementation touchpoints

- `src/backend/services/namespace_service.py`
- `src/backend/services/window_session_context_service.py`
- `src/backend/server/routes/von_routes.py` (`_resolve_generate_namespace_context`)
- `src/backend/services/chat_history_service.py` (`add_message_to_history`)
- `src/backend/integrations/internal_mcp/catalogue.py` (`_resolve_rag_namespace_from_kwargs`, `_rag_namespace_resolution_error`)

