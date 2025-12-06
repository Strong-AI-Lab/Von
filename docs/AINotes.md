# AI Notes

## Current Status
- **Date**: 2025-12-04 (Updated: JVNAUTOSCI-787 Phase 1 Complete)
- **Current Work**: JVNAUTOSCI-787 Phase 1 Implementation (COMPLETE ✅)
- **Branch**: JVNAUTOSCI-787-phase-1-composite-namespace-model-stub-implemen
- **Immediate Focus**: Phase 1 is complete. Pending user decision on:
  1. Integration testing of full Phase 1 flow
  2. Proceed with Phase 2 (UI components and org selector)
  3. Or address any issues/feedback from Phase 1 review

## Phase 1 Implementation Summary (COMPLETE)

### What Was Done
**JVNAUTOSCI-787: Composite User@Org Namespace Model for RAG Isolation**

Implemented complete Phase 1 infrastructure for multi-tenant organisation-scoped RAG content isolation:

#### Deliverable 1: Namespace Service ✅
- File: `src/backend/services/namespace_service.py` (145 lines)
- Purpose: Centralised namespace derivation, parsing, and validation
- Key Functions:
  - `derive_namespace(user_id, org_id, role)` → `#V#user@org` (org-scoped) or `#V#user` (user-only)
  - `parse_namespace(namespace)` → Extracts user_id and org_id
  - `is_org_scoped(namespace)` → Boolean check
  - `get_user_id(namespace)`, `get_org_id(namespace)` → Accessors
  - `normalize_namespace(namespace)` → Standardisation and validation
- Validation: Alphanumeric + underscores only (no spaces, special chars, quotes)
- Testing: 35 comprehensive unit tests (100% pass rate)

#### Deliverable 2: Stub Role Resolver ✅
- File: `src/backend/security/role_resolver.py` (140 lines)
- Purpose: Placeholder role system for Phase 1, designed for Phase 3 database replacement
- Key Functions:
  - `get_user_role(user_id, org_id)` → Returns role (default "member")
  - `get_effective_permissions(role)` → Set of permissions for role
  - `has_permission(user_id, org_id, permission)` → Permission check
  - `get_all_user_organisations(user_id)` → List of orgs user belongs to
- Hardcoded Mappings: `{"michael_witbrock": {"sail": "admin"}}`
- Roles: viewer, member, contributor, admin, owner
- Note: Phase 3 will replace hardcoded mappings with database-backed RBAC

#### Deliverable 3: Session Schema Updates ✅
- Modified: `src/backend/models/concept_models.py`
- Extended `ConceptInteraction` class with:
  - `organisation_concept_id: Optional[str]` → Links interaction to org
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


