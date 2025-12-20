# Vontology MCP Text Relations Tools - Design Document

## Problem Statement

During JVNAUTOSCI-800, we needed to add a `hasContent` text relation to a prompt concept. The current MCP server has an `add_names` tool (specialized for `hasName` relations only), but lacks general-purpose text relation tools. This forced us into:

- **Throwaway Python scripts** with import/path issues
- **Inline Python one-liners** with parameter name confusion
- **Direct MongoDB queries** bypassing service layer
- **Trial-and-error with HTTP endpoints** that returned 404

**What should have been simple**: "Add this text as hasContent to concept X"
**What it became**: 45+ minutes of debugging import errors, hanging processes, and MongoDB connection issues.

## Root Cause

The MCP server exposes `add_names()` (wraps `upsert_text_for_concept` with hardcoded `predicate="hasName"`), but doesn't expose the underlying `text_value_service` functions that handle **all text relations**:

- `upsert_text_for_concept()` - Add/update ANY text relation
- `get_texts_for_concept()` - Retrieve text relations by predicate
- `update_text_relation_text()` - Modify existing text content
- `delete_text_relation_by_predicate_and_text()` - Remove specific text relations

## Proposed Solution: General-Purpose Text Relation Tools

Add 4 new MCP tools that expose the complete text_value_service API:

### 1. `upsert_text_relation` (Replaces ad-hoc scripts)

**Purpose**: Add or update ANY text relation (hasContent, hasDescription, hasNote, custom predicates, etc.)

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "concept_id": {
      "type": "string",
      "description": "The concept to attach text to (e.g., '#V#my_concept')"
    },
    "predicate": {
      "type": "string",
      "description": "The text relation predicate (e.g., 'hasContent', 'hasDescription', 'hasNote')"
    },
    "text": {
      "type": "string",
      "description": "The text content to attach"
    },
    "language": {
      "type": "string",
      "default": "en-NZ",
      "description": "Language code (ISO 639-1/BCP 47): en-NZ (default), en-US, fr, de, mi, zh, etc."
    },
    "context": {
      "type": "object",
      "description": "Optional metadata (e.g., {'text_type': 'NL', 'author': 'system'})"
    }
  },
  "required": ["concept_id", "predicate", "text"]
}
```

**Output**:
```json
{
  "success": true,
  "text_value_id": "694665bc61d19992de831b52",
  "relation_id": "694665bc61d19992de831b53",
  "relation_created": true,
  "predicate": "hasContent",
  "text_preview": "Analyze this LLM response and determine..."
}
```

**What it eliminates**:
- ❌ Creating `utilities/add_detector_content.py` throwaway scripts
- ❌ Debugging import paths and parameter names
- ❌ Inline Python with syntax escaping nightmares
- ✅ Simple MCP call: `upsert_text_relation('#V#prompt', 'hasContent', 'Prompt text...')`

---

### 2. `get_text_relations` (Replaces direct MongoDB queries)

**Purpose**: Retrieve text relations for a concept, optionally filtered by predicate/language

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "concept_id": {
      "type": "string",
      "description": "The concept to query text relations for"
    },
    "predicate": {
      "type": "string",
      "description": "Optional: filter by predicate (e.g., 'hasContent', 'hasName')"
    },
    "language": {
      "type": "string",
      "description": "Optional: filter by language code (e.g., 'en-NZ', 'fr')"
    },
    "limit": {
      "type": "integer",
      "default": 50,
      "description": "Maximum number of results to return"
    }
  },
  "required": ["concept_id"]
}
```

**Output**:
```json
{
  "concept_id": "#V#my_prompt",
  "relations_found": 2,
  "relations": [
    {
      "predicate": "hasContent",
      "text": "This is the prompt content...",
      "language": "en-NZ",
      "text_value_id": "abc123",
      "relation_id": "def456",
      "context": {"text_type": "NL"}
    },
    {
      "predicate": "hasDescription",
      "text": "A helpful prompt for...",
      "language": "en-NZ",
      "text_value_id": "ghi789",
      "relation_id": "jkl012",
      "context": {}
    }
  ]
}
```

**What it eliminates**:
- ❌ `pdm run python -c "from src.backend.services.text_value_service import get_texts_for_concept; ..."`
- ❌ Direct MongoDB queries via mongo_client
- ✅ Simple: `get_text_relations('#V#prompt', predicate='hasContent')`

---

### 3. `update_text_relation` (Replaces manual update scripts)

**Purpose**: Modify the text content of an existing text relation by relation ID

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "concept_id": {
      "type": "string",
      "description": "The concept owning the text relation"
    },
    "relation_id": {
      "type": "string",
      "description": "The ID of the text relation to update"
    },
    "new_text": {
      "type": "string",
      "description": "The new text content"
    },
    "new_language": {
      "type": "string",
      "description": "Optional: update the language code"
    },
    "new_context": {
      "type": "object",
      "description": "Optional: update the context metadata"
    }
  },
  "required": ["concept_id", "relation_id", "new_text"]
}
```

**Output**:
```json
{
  "success": true,
  "relation_id": "def456",
  "old_text_preview": "Old prompt content...",
  "new_text_preview": "Updated prompt content...",
  "text_value_id": "abc123"
}
```

**What it eliminates**:
- ❌ Manual find → update → verify cycles
- ✅ Direct update by relation ID

---

### 4. `delete_text_relation` (Completes CRUD operations)

**Purpose**: Delete a specific text relation by predicate and text match, or by relation ID

**Input Schema**:
```json
{
  "type": "object",
  "properties": {
    "concept_id": {
      "type": "string",
      "description": "The concept owning the text relation"
    },
    "relation_id": {
      "type": "string",
      "description": "Optional: delete by relation ID (preferred method)"
    },
    "predicate": {
      "type": "string",
      "description": "Optional: delete by predicate + text match"
    },
    "text": {
      "type": "string",
      "description": "Optional: exact text to match (used with predicate)"
    }
  },
  "required": ["concept_id"]
}
```

**Output**:
```json
{
  "success": true,
  "deleted_relation_id": "def456",
  "deleted_text_preview": "Old content...",
  "text_value_cleaned_up": true
}
```

---

## Migration Path for `add_names`

**Current**: `add_names` is a specialized wrapper around `upsert_text_for_concept(predicate="hasName")`

**Options**:

1. **Keep both** (recommended):
   - Keep `add_names` for ergonomic bulk name operations
   - Add `upsert_text_relation` for general use
   - `add_names` internally calls `upsert_text_for_concept` (no duplication)

2. **Deprecate `add_names`**:
   - Mark as deprecated in manifest
   - Redirect to `upsert_text_relation` with `predicate="hasName"`
   - Remove in future version

**Recommendation**: Option 1. `add_names` has specific semantics (bulk operations, name type validation) that justify keeping it as a convenience tool.

---

## Implementation Checklist

### Phase 1: Core Tools (Immediate - JVNAUTOSCI-800)
- [ ] Add `upsert_text_relation` handler to `mcp_stdio_server.py`
- [ ] Add `upsert_text_relation` tool definition to manifest
- [ ] Add `get_text_relations` handler
- [ ] Add `get_text_relations` tool definition
- [ ] Update internal MCP catalogue with new tools
- [ ] Add HTTP routes to `mcp_server.py` (for parity)

### Phase 2: Complete CRUD (Follow-up)
- [ ] Add `update_text_relation` handler
- [ ] Add `update_text_relation` tool definition
- [ ] Add `delete_text_relation` handler
- [ ] Add `delete_text_relation` tool definition
- [ ] Update all three MCP implementations (stdio, HTTP, internal)

### Phase 3: Testing & Documentation
- [ ] Add unit tests for each new tool
- [ ] Add integration tests for text relation workflows
- [ ] Update MCP server documentation
- [ ] Add examples to USER_GUIDE.md
- [ ] Update AGENTS.md with new capabilities

---

## Example Usage Scenarios

### Scenario 1: Add prompt content (JVNAUTOSCI-800 use case)

**Before** (45 minutes of pain):
```python
# utilities/add_detector_content.py
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
from src.backend.services.text_value_service import upsert_text_for_concept
# ... 20 more lines ...
```

**After** (30 seconds):
```
mcp_vontology_upsert_text_relation(
  concept_id="#V#missing_tool_call_detection_prompt",
  predicate="hasContent",
  text="Analyze this LLM response and determine...",
  language="en-NZ"
)
```

---

### Scenario 2: Retrieve and verify prompt content

**Before**:
```bash
pdm run python -c "from src.backend.services.text_value_service import get_texts_for_concept; result = get_texts_for_concept('#V#prompt', 'hasContent'); print(result)"
```

**After**:
```
mcp_vontology_get_text_relations(
  concept_id="#V#missing_tool_call_detection_prompt",
  predicate="hasContent"
)
```

---

### Scenario 3: Add multilingual descriptions

**Before**: Multiple script executions or inline Python escaping hell

**After**:
```
# English description
upsert_text_relation('#V#eu_member_state', 'hasDescription',
  'A country that is a member of the European Union', language='en-NZ')

# French description
upsert_text_relation('#V#eu_member_state', 'hasDescription',
  'Un pays membre de l\'Union européenne', language='fr')

# German description
upsert_text_relation('#V#eu_member_state', 'hasDescription',
  'Ein Land, das Mitglied der Europäischen Union ist', language='de')
```

---

## Security Considerations

- ✅ **Access control**: All tools use `can_access_concept()` checks (existing pattern)
- ✅ **Namespace isolation**: Respects existing namespace security boundaries
- ✅ **Audit trail**: All operations create timestamped database records
- ⚠️ **Text content limits**: Add validation for text length (prevent abuse)
- ⚠️ **Predicate validation**: Consider allowlist for valid predicates (hasContent, hasDescription, hasName, hasNote, etc.)

---

## Performance Considerations

- **Bulk operations**: `upsert_text_relation` handles one relation at a time (unlike `add_names` which batches)
- **Future optimization**: Add `upsert_text_relations_batch` for bulk operations if needed
- **Caching**: Text values are content-addressed (fingerprint-based deduplication works)

---

## Alternative Rejected Approaches

### ❌ "Just improve the docs/examples"
- Problem: Still requires throwaway scripts, import debugging, parameter mapping
- Verdict: Doesn't solve root cause (missing MCP tool)

### ❌ "Add more HTTP endpoints"
- Problem: Three separate implementations to maintain (HTTP, stdio, internal)
- Verdict: MCP is the right abstraction layer - expand it, don't bypass it

### ❌ "Make add_names accept any predicate"
- Problem: Semantics change (names vs content), breaks existing usage
- Verdict: Better to add dedicated tools with clear purpose

---

## Success Metrics

**Immediate (Post-Implementation)**:
- ✅ Adding text relations takes <30 seconds instead of 45+ minutes
- ✅ Zero throwaway utility scripts needed for text operations
- ✅ No inline Python one-liners with escaping issues

**Long-term (3 months)**:
- ✅ >80% of text relation operations use MCP tools instead of scripts
- ✅ Zero import/path debugging issues in JIRA tickets
- ✅ Text relation CRUD documented with examples

---

## Related Issues

- **JVNAUTOSCI-800**: Fix MCP orchestrator execution flow (prompted this design)
- **JVNAUTOSCI-303**: Internal MCP synchronization (context for tool parity requirements)

---

## Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| Add 4 new tools vs extend existing | Clear separation of concerns, avoid breaking changes |
| Keep `add_names` separate | Specialized semantics justify dedicated tool |
| Use same signatures as `text_value_service` | Direct mapping = easier maintenance |
| Language defaults to "en-NZ" | Project standard (NZ English) |
| Support optional context metadata | Future-proof for provenance, authorship, etc. |
| Implement in all 3 MCP servers | Consistency prevents JVNAUTOSCI-303-style bugs |

---

## Appendix: text_value_service API Reference

**Functions to expose**:

```python
def upsert_text_for_concept(
    subject_concept_id: str,
    predicate: str,
    text: str,
    lang: str = "en",
    provenance: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]

def get_texts_for_concept(
    subject_concept_id: str,
    predicate: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]

def update_text_relation_text(
    subject_concept_id: str,
    relation_id: str,
    new_text: str,
    new_lang: Optional[str] = None,
    new_context: Optional[Dict[str, Any]] = None,
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]

def delete_text_relation_by_predicate_and_text(
    subject_concept_id: str,
    predicate: str,
    text: str,
    lang: Optional[str] = None,
) -> Dict[str, Any]

def delete_text_relation(
    subject_concept_id: str,
    relation_id: str
) -> Dict[str, Any]
```

---

**Author**: AI Agent (Copilot)
**Date**: 2025-12-20
**Status**: Proposed
**Reviewers**: @witbrock
