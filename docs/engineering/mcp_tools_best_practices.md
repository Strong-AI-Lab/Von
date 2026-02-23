# MCP Tools Best Practices Guide

## Quick Decision Tree: When to Use MCP vs Custom Scripts

```
┌─ Need to modify Vontology? ─┐
│                              │
├─ YES → Can this be done in 1-3 MCP calls?
│          ├─ YES  → Use MCP tools ✅
│          └─ NO   → Write bulk script (migration, backfill, conditional logic)
│
└─ NO  → Use appropriate service/utility directly
```

---

## Category 1: Always Use MCP Tools

### 1.1 Adding Text Content to Concepts

**Scenario**: You need to attach text (prompts, descriptions, instructions, notes) to a concept.

**WRONG** (Direct Python script):
```python
from src.backend.services.text_value_service import upsert_text_for_concept

result = upsert_text_for_concept(
    subject_concept_id="#V#my_prompt",
    predicate="hasContent",
    text="My prompt text...",
    lang="en-NZ"
)
```

**RIGHT** (MCP tool):
```json
{
  "tool": "upsert_text_relation",
  "arguments": {
    "concept_id": "#V#my_prompt",
    "predicate": "hasContent",
    "text": "My prompt text...",
    "language": "en-NZ"
  }
}
```

**Why MCP is Better**:
- ✅ No import path issues
- ✅ No module dependency confusion
- ✅ Logged and traceable
- ✅ Works from any client (CLI, IDE, chat)
- ✅ Self-documenting (schema describes parameters)

---

### 1.2 Querying Text Relations

**Scenario**: You need to find what text is attached to a concept.

**WRONG** (Direct MongoDB/service call):
```python
from src.backend.services.text_value_service import get_texts_for_concept

texts = get_texts_for_concept(
    subject_concept_id="#V#my_prompt",
    predicate="hasContent"
)
```

**RIGHT** (MCP tool):
```json
{
  "tool": "get_text_relations",
  "arguments": {
    "concept_id": "#V#my_prompt",
    "predicate": "hasContent",
    "limit": 50
  }
}
```

**Result**:
```json
{
  "concept_id": "#V#my_prompt",
  "relations_found": 2,
  "relations": [
    {
      "predicate": "hasContent",
      "text": "...",
      "language": "en-NZ",
      "relation_id": "abc123"
    }
  ]
}
```

---

### 1.3 Creating and Updating Concepts

**Scenario**: You need to create one or more concepts or modify concept properties.

**WRONG** (Direct service call):
```python
from src.backend.vontology.utils_vontology import create_vontology_concept

result = create_vontology_concept(
    parent_id="#V#thing",
    new_concept_name="my_concept",
    create_as_instance=False
)
```

**RIGHT** (MCP tool):
```json
{
  "tool": "create_concepts",
  "arguments": {
    "parent_id": "#V#thing",
    "concepts": [
      {
        "name": "my_concept",
        "kind": "type",
        "description": "Optional description"
      }
    ]
  }
}
```

**Bulk creation still uses MCP**:
```json
{
  "tool": "create_concepts",
  "arguments": {
    "parent_id": "#V#person",
    "concepts": [
      {"name": "researcher", "kind": "type"},
      {"name": "student", "kind": "type"},
      {"name": "professor", "kind": "type"}
    ]
  }
}
```

---

### 1.4 Adding Names/Aliases (Multilingual)

**Scenario**: You need to add alternative names, translations, or abbreviations to a concept.

**RIGHT** (MCP tool - special case for names):
```json
{
  "tool": "add_names",
  "arguments": {
    "concept_id": "#V#person",
    "names": [
      "human being",
      {"name": "être humain", "language": "fr"},
      {"name": "Human", "language": "en-US"},
      {"name": "H", "language": "en", "name_type": "ABBR"}
    ]
  }
}
```

**Or generic method** (works for any predicate):
```json
{
  "tool": "upsert_text_relation",
  "arguments": {
    "concept_id": "#V#person",
    "predicate": "hasName",
    "text": "human being",
    "language": "en-NZ",
    "context": {"name_type": "NL"}
  }
}
```

---

### 1.5 Managing Concept Relationships

**Scenario**: You need to add, remove, or query relationships between concepts (instance_of, typeOf, custom predicates).

**Adding relationships**:
```json
{
  "tool": "add_relationship",
  "arguments": {
    "source_id": "#V#john_smith",
    "predicate": "instance_of",
    "target": "#V#person"
  }
}
```

**Removing relationships**:
```json
{
  "tool": "remove_relationship",
  "arguments": {
    "source_id": "#V#john_smith",
    "predicate": "instance_of",
    "target": "#V#person"
  }
}
```

**Querying relationships** (in fetch_concept):
```json
{
  "tool": "fetch_concept",
  "arguments": {
    "concept_id": "#V#john_smith",
    "include_relations_arg1": true,
    "include_relations_any_arg": true
  }
}
```

---

### 1.6 Dynamic MCP Tool Registration from Vontology (Runtime-Safe Path)

Use this when you need a Vontology concept to expose a new MCP tool **without**
adding a bespoke Python `MethodDefinition`.

Current runtime behaviour (JVNAUTOSCI-1250):
- Only `#V#mcp_tool` instances are considered.
- Dynamic registration is **fail-closed** and requires explicit activation markers.
- Dynamic tools can proxy **existing built-in internal MCP tools only**.
- Dynamic tool names cannot override protected built-in names.
- Load outcomes are inspectable via gateway diagnostics (`dynamic_tool_registration`).

Required concept attributes for activation:
- `mcp_tool_name` (string): dynamic method name to expose.
- `dynamic_target_tool_name` (string): existing built-in method name to proxy.
- `dynamic_registration_enabled` (bool): must be `true`.
- `dynamic_registration_approved` (bool): must be `true`.

Optional attributes:
- `dynamic_fixed_payload` (object): fixed arguments enforced at runtime.
- `dynamic_timeout_sec` (number > 0): override timeout for this proxy.
- `dynamic_description` (string): human-readable description.

Example attributes payload:
```json
{
  "mcp_tool_name": "jira_get_issue_safe",
  "dynamic_target_tool_name": "jira_get_issue",
  "dynamic_registration_enabled": true,
  "dynamic_registration_approved": true,
  "dynamic_fixed_payload": {
    "fields": ["summary", "status", "assignee"]
  },
  "dynamic_timeout_sec": 20,
  "dynamic_description": "Safe Jira issue lookup with constrained fields."
}
```

Guardrails to preserve:
- Do not point `dynamic_target_tool_name` at non-existent methods.
- Do not reuse built-in method names in `mcp_tool_name`.
- Keep fixed payload keys schema-compatible with the target tool.
- Keep approval explicit; avoid implicit activation from metadata-only concepts.

---

## Category 2: When to Write Custom Python Scripts

### 2.1 Bulk Operations with Conditional Logic

**Scenario**: You need to:
- Migrate 1000+ concepts from old format to new format
- Create concepts conditionally based on querying existing data
- Perform rollback on failure
- Complex multi-step transformations

**Example: Migration script** (OK to write Python):
```python
# src/backend/utilities/migrate_org_memberships.py
from src.backend.services.organisation_membership_service import create_organisation_membership

for user_id, org_id in stub_mappings.items():
    # Conditional: only migrate if not already done
    existing = get_user_memberships(user_id)
    if org_id not in [m['org_id'] for m in existing]:
        create_organisation_membership(user_id, org_id, "member")
        print(f"Migrated {user_id} → {org_id}")
```

**Why Python here is OK**:
- ✅ Complex conditional logic
- ✅ Bulk operation (1000+ items)
- ✅ Error recovery/rollback needed
- ✅ One-time migration (not repeated)

---

### 2.2 Complex Data Validation Before Creating Resources

**Scenario**: You need to:
- Validate multiple related concepts exist before creating relationships
- Perform consistency checks across multiple resources
- Generate derived data (hashes, computed fields)

**Example: Validation script**:
```python
# Check if person AND organisation exist before creating membership
person = get_concept_by_concept_id(person_id)
org = get_concept_by_concept_id(org_id)

if not person or not org:
    raise ValueError(f"Cannot create membership: missing concept")

# Only then create the relationship via MCP or direct call
create_organisation_membership(person_id, org_id)
```

---

### 2.3 Reading Large Datasets for Analysis

**Scenario**: You need to:
- Export ontology data for analysis
- Generate statistics about concepts
- Build reports on usage patterns

**Example: Analysis script**:
```python
# Collect stats on all concepts (too much for MCP pagination)
all_nodes = get_all_vontology_nodes_with_details()

stats = {
    "total": len(all_nodes),
    "by_kind": defaultdict(int)
}
for node in all_nodes:
    stats["by_kind"][node.get("computed_kind")] += 1

print(json.dumps(stats, indent=2))
```

---

## Category 3: Never Do This

### ❌ DON'T: Use Python scripts for simple single operations

```python
# ❌ WRONG - This is a throwaway script that should be an MCP call
from src.backend.services.text_value_service import upsert_text_for_concept

upsert_text_for_concept(
    subject_concept_id="#V#my_concept",
    predicate="hasContent",
    text="My content..."
)
```

### ❌ DON'T: Directly manipulate MongoDB

```python
# ❌ WRONG - Bypasses all validation and logging
from src.backend.db.mongo_client import mongo_client

db = mongo_client['von_db']
db['vontology_nodes'].update_one(
    {"concept_id": "#V#x"},
    {"$set": {"custom_field": "value"}}
)
```

### ❌ DON'T: Use HTTP endpoints when MCP tools are available

```bash
# ❌ WRONG - Prefer MCP (stdio) for agent workflows in VS Code.
# HTTP routes are for Von's web server/admin endpoints, not MCP tool calls.
curl -X POST http://localhost:5000/vontology/api/vontology/create_concept \
  -H "Content-Type: application/json" \
  -d '{"parent_id": "#V#thing", "new_concept_name": "example"}'
```

### ❌ DON'T: Import internal service modules in agent workflows

```python
# ❌ WRONG - Tight coupling to implementation
from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.services.concept_service import get_concept_by_concept_id
from src.backend.db.repositories.concepts_repository import ConceptsRepository
```

---

## Reference: All Vontology MCP Tools

### Text Relations (Primary Category)

| Tool | Purpose | Use When |
|------|---------|----------|
| `upsert_text_relation` | Add/update ANY text (hasContent, hasDescription, hasNote, custom) | Adding text to concept |
| `get_text_relations` | Query text relations by concept/predicate/language | Finding stored text |
| `update_text_relation` | Modify text content by relation ID | Editing existing text |
| `delete_text_relation` | Remove text relations | Deleting stored text |

### Concept Management

| Tool | Purpose | Use When |
|------|---------|----------|
| `create_concepts` | Create one or more concepts | Adding new types, instances, predicates |
| `fetch_concept` | Get full concept details with relations | Understanding concept structure |
| `search_concepts` | Find concepts by query/kind/type | Searching ontology |
| `add_names` | Add multilingual names/aliases | Creating translations and abbreviations |

### Concept Relationships

| Tool | Purpose | Use When |
|------|---------|----------|
| `add_relationship` | Create concept-to-concept links (instance_of, typeOf, custom) | Linking concepts together |
| `remove_relationship` | Delete concept-to-concept links | Fixing incorrect relationships |
| `find_subconcepts` | Get direct children of a concept | Navigating hierarchy |

### Advanced Operations

| Tool | Purpose | Use When |
|------|---------|----------|
| `extract_annotations` | Extract concept mentions from text | Auto-linking concepts to documents |
| `delete_concept` | Remove a concept (with relation cleanup) | Removing outdated concepts |
| `merge_concepts` | Combine two concepts into one | Consolidating duplicates |
| `update_concept` | Modify specific concept fields | Fixing concept properties |

### Search & Knowledge

| Tool | Purpose | Use When |
|------|---------|----------|
| `search_knowledge_base` | Search internal RAG index | Finding indexed documents |
| `search_web` | Search the internet via Tavily | Finding external information |
| `search_arxiv` | Search scholarly papers | Finding academic sources |

---

## Common Patterns & Examples

### Pattern 1: Add Configuration Prompt to Concept

```python
# Intent: Store a prompt in the Vontology as concept metadata

mcp_upsert_text_relation(
    concept_id="#V#my_detector_action",
    predicate="hasContent",
    text="""Analyze this LLM response and determine if...

    Answer with ONLY yes or no:
    - yes: Model said it would perform action but didn't emit tool call
    - no: Model either included tool call or didn't promise action""",
    language="en-NZ",
    context={"purpose": "detector_prompt"}
)
```

### Pattern 2: Store Configuration in Multiple Languages

```python
# Intent: Store the same text in multiple languages

mcp_upsert_text_relation(
    concept_id="#V#greeting_prompt",
    predicate="hasContent",
    text="Hello, how can I assist?",
    language="en-NZ"
)

mcp_upsert_text_relation(
    concept_id="#V#greeting_prompt",
    predicate="hasContent",
    text="Bonjour, comment puis-je vous aider?",
    language="fr"
)
```

### Pattern 3: Create Concept Hierarchy

```python
# Intent: Create a hierarchy of concepts

mcp_create_concepts(
    parent_id="#V#thing",
    concepts=[
        {"name": "animal", "kind": "type"},
        {"name": "person", "kind": "type"}
    ]
)
# Returns: {"results": [{"concept_id": "#V#animal"}, {"concept_id": "#V#person"}]}

mcp_create_concepts(
    parent_id="#V#person",
    concepts=[
        {"name": "researcher", "kind": "type"},
        {"name": "student", "kind": "type"}
    ]
)
```

### Pattern 4: Set Up Relationships Between Concepts

```python
# Intent: Create an instance and link it to a type

mcp_create_concepts(
    parent_id="#V#person",
    concepts=[{"name": "john_smith", "kind": "instance"}]
)

mcp_add_relationship(
    source_id="#V#john_smith",
    predicate="instance_of",
    target="#V#researcher"
)
```

---

## Troubleshooting MCP Tool Issues

### Issue: Tool not found / Unknown tool

**Causes**:
1. Tool name is wrong (check `vontology_mcp.json` for spelling)
2. MCP server not running
3. Wrong MCP endpoint (HTTP vs stdio)

**Fix**:
1. Check [vontology_mcp.json](../../src/backend/mcp_server/vontology_mcp.json) for tool list
2. Verify Von web server running: `curl http://localhost:5000/health`
3. Use correct interface: stdio for VS Code (see `.vscode/mcp.json`), HTTP only for Von web routes/admin endpoints

### Issue: Parameter validation failed

**Cause**: Required parameter missing or wrong type

**Fix**: Check tool schema in `mcp_stdio_server.py`:
- Required parameters marked with `required: [...]`
- `concept_id` format: `#V#concept_name` (not filesystem path)
- `predicate`: String (not object)

### Issue: Concept not found

**Cause**: `concept_id` doesn't exist in Vontology

**Fix**:
1. Verify ID format: `#V#` prefix required
2. Search first: `search_concepts(query="concept_name")`
3. Check spelling of concept_id

### Issue: Text relation empty

**Cause**: Added text with wrong predicate or language

**Fix**:
1. Query all relations: `get_text_relations(concept_id, limit=100)` (no filters)
2. Check language: Default is `en-NZ`, case-sensitive
3. Verify predicate: `hasContent` vs `hasDescription` vs custom

---

## Summary: The Golden Rule

### ✅ Use MCP Tools IF:
- Operation completes in 1-3 MCP calls
- No complex conditional logic needed
- No bulk processing required
- No data validation across multiple resources
- It's a **standard Vontology operation**

### ✅ Use Python Scripts IF:
- Bulk operation (100+ items)
- Complex conditional logic required
- Multi-step transformation with rollback
- Data validation across multiple resources
- One-time migration or backfill
- Extracting/analysing large datasets

### 🎯 Key Principle
**MCP is the standard interface.** Treat custom Python as the exception, not the default. If you're writing throwaway scripts to interact with Vontology, you're probably missing an MCP tool.
