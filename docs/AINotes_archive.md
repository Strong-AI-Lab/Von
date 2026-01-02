# AI Notes Archive

Archived from `docs/AINotes.md` on 2026-01-02.

---

# AI Notes

## Agent Reminder (Config Lookup)

If you need values from `.env`, prefer the allowlisted helper script instead of reading the file directly:

- Script: `utilities/get_safe_env.py`
- Example: `pdm run python utilities/get_safe_env.py VON_DB_NAME`

It only prints allowlisted keys and redacts secret-like values (tokens, passwords, etc.).

## Current Status
  1. Integration testing of full Phase 1 flow
  2. Proceed with Phase 2 (UI components and org selector)
  3. Or address any issues/feedback from Phase 1 review

---

## What Was Done
**JVNAUTOSCI-789: UI Integration for Organisation and Role Selection**

Implemented Phase 2 infrastructure for user-facing organisation switching and persistent membership model:

#### Deliverable 1: Organisation Membership Service ✅
- File: `src/backend/services/organisation_membership_service.py` (410 lines)
- Purpose: Vontology-based membership management with memberOf predicates
- Key Functions:
  - `create_organisation_membership()` → Creates memberOf relationship + hasRole text relation
  - `get_user_memberships()` → Retrieves user's organisations and roles
  - `get_organisation_members()` → Lists org members with optional role filtering
  - `update_user_role()` → Updates user's role in organisation
  - `remove_organisation_membership()` → Removes user from organisation
- Data Model: Uses memberOf edge relationships + hasRole text relations with org context
- Access Control: Integrates with access_control system for permission checking
- Testing: 20 comprehensive unit tests (100% pass rate) covering all functions and error paths

#### Deliverable 2: Backend API Endpoints ✅
- Modified: `src/backend/server/routes/von_routes.py`
- Endpoint 1: `POST /api/session/set_organisation`
  - Switches user's organisation context in Flask session

  ### What Was Done
  - `role_in_org: Optional[str]` → User's role in org (from resolver)
  - `namespace: Optional[str]` → Derived composite namespace
- Backward Compatible: All fields optional, existing code unaffected
- Updated: `src/backend/services/concept_service.py::start_interaction_session()`
  - Now derives composite namespace using namespace_service
  - Reads org_id and role_in_org from Flask session
  - Falls back gracefully to user-only namespace if no org context

#### Deliverable 4: Access Control Extensions ✅
- Modified: `src/backend/security/access_control.py`
- Enhanced `build_visibility_filter()` function:
  - Now reads `organisation_concept_id` from Flask session
  - Applies org-specific visibility filters (filters concepts by user AND org)
  - Log message: "Including org-specific concepts for org={user_org_id}"
  - Maintains backward compatibility with user-only sessions

#### Deliverable 5: RAG Metadata Integration ✅
- Modified: `src/backend/services/chat_history_service.py`
  - Added `get_session_context()` helper
  - `add_message_to_history()` now includes org metadata in RAG documents
  - Metadata: user_id, session_id, role, timestamp, type, organisation_concept_id, role_in_org
- Modified: `src/backend/services/rag_sync_service.py`
  - `collect_indexed_sessions()` fetches org fields from interaction_sessions
  - `sync_one_session()` preserves org metadata when syncing to RAG
- Modified: `src/backend/services/rag_backends/llamaindex_backend.py`
  - `query()` method now supports organisation_concept_id filtering
  - MetadataFilters include both user_id AND organisation_concept_id
  - Implements org-scoped access control at RAG retrieval level
- Modified: `src/backend/integrations/internal_mcp/catalogue.py`
  - `_search_knowledge_base()` now passes permissions_context with org_id to RAG query
- Modified: `src/backend/mcp_server/mcp_stdio_server.py`
  - search_knowledge_base handler now includes org context
- Modified: `src/backend/mcp_server/mcp_server.py`
  - search_knowledge_base endpoint now includes session-based org context to queries

### Test Results
- **Namespace Service Tests**: 35/35 PASSED ✅
  - Derivation (9 tests)
  - Parsing (8 tests)
  - Helpers (9 tests)
  - Edge cases (9 tests)
- **All Backend Tests**: 62 passed, 3 skipped ✅
  - No regressions
  - Chat history segmentation fixed (sys.path issue)
  - RAG service tests all passing

### Git Commits
1. `a8445b0`: Phase 1 Composite Namespace Model + Stub Resolver + Tests (597 insertions)
2. `f15ea96`: Phase 1 RAG Metadata Integration for Org-Scoped Content (130 insertions)
3. `9723a97`: Fix test_chat_history_segmentation.py import path (8 insertions)

### Design Decisions
1. **Namespace Format**: `#V#{user_id}@{org_id}` (simple, parseable, consistent with #V# naming)
2. **Role Storage**: In metadata/session, not in namespace (allows role changes without namespace change)
3. **Graceful Degradation**: Sessions without org context fall back to user-only namespace
4. **Query Filtering**: AND logic (both user AND org must match for org-scoped queries)
5. **Backward Compatibility**: All new fields optional, no breaking changes
6. **Test Pattern**: sys.path injection in test file (handles pytest context issues)

### Phase 1 Scope (From JVNAUTOSCI-787)
- ✅ Composite namespace derivation with user@org format
- ✅ Namespace parsing and validation utilities
- ✅ Stub role resolver with hardcoded mappings
- ✅ Session model updates for org and role tracking
- ✅ Access control extensions for org-specific visibility
- ✅ RAG metadata inclusion (org_id and role_in_org)
- ✅ RAG query filtering by organisation
- ✅ Comprehensive test coverage (35 namespace tests)
- ✅ All backend tests passing (62 passed, 3 skipped)

### Known Limitations & Phase 3 Tasks
- Role mappings currently hardcoded (Phase 3: Database-backed RBAC)
- No UI for organisation selection (Phase 2: UI components)
- No database model for organisation memberships (Phase 2: Vontology integration)
- RAG filtering basic AND logic (Future: More complex permission rules)

### What's Next
**Phase 2**: UI and Vontology Integration
- Create UI components for organisation selector
- Implement `/api/session/set_organisation` endpoint
- Create Vontology-based organisation model
- Document organisation membership workflows

**Phase 3**: Production-Ready RBAC
- Replace hardcoded role mappings with database
- Implement granular permission model
- Add role management UI
- Add audit logging for role changes

## Previous Status (Before Phase 1)
- **Date**: 2025-12-04
- **Recent Activity**:
  - Added quote and space validation to concept name generation (JVNAUTOSCI-760)
  - Created reusable `validate_concept_name_for_id()` helper function in utils_vontology.py
  - Updated linkification regex to remove spaces from allowed character set (consistent with validation)
  - Concept IDs now disallow: quotes (reserved for text boundaries), spaces (use underscores/hyphens)

## Todo
- [ ] Integration testing of full Phase 1 flow (namespace → access control → RAG queries)
- [ ] Phase 2 implementation: UI components for org selector
- [ ] Phase 2 implementation: Organisation membership Vontology model
- [ ] Phase 3 implementation: Database-backed RBAC system

## Technical Debt & Refactoring Notes

### Type Safety Enforcement (CRITICAL - Identified December 2025)

**Problem:** Multiple instances of `# type: ignore` comments appeared during JVNAUTOSCI-800 cleanup, particularly in scenarios like wrapper pattern usage (`_RestrictedGateway`). These ignores are a symptom of insufficient type enforcement in the codebase.

**Root Cause:** Some architectural patterns (wrapper classes, duck typing) and legacy code don't leverage proper typing:
- Wrapper classes that delegate to underlying implementations without using Protocol or abstract base classes
- Functions accepting loosely-typed arguments (Any, dict) when more specific types would be appropriate
- Missing TypedDict or dataclass definitions for structured data
- Inconsistent use of Optional/Union vs None coercion

**Recommendation:** Establish a **type enforcement strategy** for the refactoring backlog:
1. **Enable strict type checking** in Pyright configuration (`pyrightConfig: {"typeCheckingMode": "strict"}` in pyrightconfig.json)
2. **Use Protocol/ABC** for wrapper patterns instead of relying on duck typing and type ignores
3. **Define explicit types** for complex dictionaries (TypedDict or dataclasses)
4. **Replace Any types** with specific union types or protocols where feasible
5. **Document intentional type mismatches** with inline comments (not just `# type: ignore`)

**Example Fix Pattern:**
- **Before:** `gateway: Any` → cast to InternalMCPGateway → needs `# type: ignore[arg-type]`
- **After:** Define `ManagedGateway` Protocol, implement wrapper against Protocol → type safe, no ignores needed

**Affected Files Requiring Review:**
- `src/backend/mcp_server/mcp_stdio_server.py` (line 1238: _RestrictedGateway wrapper)
- `src/backend/services/rag_service.py` (dict-based operations)
- `src/backend/integrations/internal_mcp/catalogue.py` (Tool definitions with flexible schemas)
- `src/backend/services/chat_auxiliary_prompt_service.py` (context dict handling)

**Priority:** Medium - not blocking, but should be addressed in next major refactoring cycle to improve IDE support and prevent subtle type-related bugs.
