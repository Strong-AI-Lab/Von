# Workflow System Audit — 2026-02-07

## Summary

This audit reviews the recent workflow system changes in the context of Jira tasks
and Confluence design documents, with focus on Vontology-first compliance and
correct functioning of background/rumination and conversation-turn workflows.

## Code Changes Made During This Session

### 1. Recursive Subtype Discovery (`vontology_loader.py`)
- Added `_get_recursive_subtypes()` — BFS traversal to find all transitive subtypes
  of `#V#durable_workflow` and `#V#workflow`
- `discover_workflow_ids()` now finds workflows typed as subtypes-of-subtypes
- This fixed the discovery of `#V#sail_phd_student_onboarding_workflow` which was
  typed as a subtype of `#V#workflow` (indirect path through the type hierarchy)

### 2. Skip-Duplicate Logic (`registry_factory.py`)
- `build_durable_workflow_registry()` now checks `registered_ids` before loading
  Vontology workflows, preventing `ValueError` on duplicate registration
- Cleaned up verbose exploratory comments (8 lines of LLM reasoning traces) and
  removed a stray `pass` statement

### 3. Async Handler Bug Fix (`considerations_workflow.py`)
- `_handle_process_batch` was declared `async def` but `ActionRegistry.execute()`
  calls handlers synchronously via `spec.handler(request)`
- This caused the handler to return an unawaited coroutine object instead of
  executing its body — effectively a silent no-op
- Fixed by converting to synchronous `def` with improved docstring

### 4. Vontology Concept Bootstrap
- Created 7 workflow concepts in the database as instances of `#V#durable_workflow`:
  - `#V#chat_assistant_workflow`
  - `#V#chat_narration_workflow`
  - `#V#generate_considerations_workflow`
  - `#V#missing_tool_call_workflow`
  - `#V#rag_text_relation_sync_workflow`
  - `#V#todo_refresh_workflow`
  - `#V#write_tool_policy_workflow`
- These were missing from Vontology, causing "non-existent" cartouches in the UI

## Architecture: Two Execution Domains

### Conversation-Turn Workflows (synchronous, in-process)

Run inline during chat turns via `WorkflowExecutor` in the MCP orchestrator.
Each orchestrator instance builds its own `ActionRegistry`.

| Workflow | Purpose | Status |
|---|---|---|
| `#V#write_tool_policy_workflow` | Gate write-tools per prompt | Active |
| `#V#missing_tool_call_workflow` | Recover missing tool calls | Active |
| `#V#chat_narration_workflow` | TTS narration | Active |
| `#V#chat_assistant_workflow` | No-op placeholder | Placeholder |
| `#V#todo_refresh_workflow` | Refresh to-dos | All no-ops |

### Durable/Background Workflows (persistent, checkpointed)

Run via `DurableWorkflowExecutor` in a background worker thread.
Separate `ActionRegistry` with durable-specific handlers.

| Workflow | Purpose | Status |
|---|---|---|
| `#V#rag_text_relation_sync_workflow` | Batch RAG sync | Implemented |
| `#V#generate_considerations_workflow` | Auto-generate considerations | Implemented (async bug now fixed) |

## Vontology-First Assessment

### What's Good
- All workflows have Vontology concept identities (instance of `#V#durable_workflow`)
- `vontology_loader.py` can load full process-graph definitions from the ontology
- `discover_workflows_for_turn()` searches Vontology for contextually relevant workflows
- The `#V#sail_phd_student_onboarding_workflow` demonstrates full Vontology-native
  workflow definition (steps, transitions, actions all as concepts)

### Gaps
1. **Dual-source architecture**: Workflow *behaviour* (states, transitions, actions)
   lives in Python code, not in the ontology. The ontology only stores
   identity/typing for code-defined workflows.
2. **Code takes precedence**: `build_durable_workflow_registry()` registers code
   workflows first, then skips matching Vontology workflows.
3. **Action handler discovery**: No mechanism to associate Vontology `invokesAction`
   references with code implementations.
4. **Discovery is informational**: `discover_workflows_for_turn()` results appear
   in debug metadata only — no auto-execution.

### Recommended Evolution
1. Store workflow step/transition structure in Vontology
2. Register action handlers by canonical IDs matching Vontology `invokesAction` targets
3. Eventually invert precedence: Vontology definitions override code definitions

## Known Issues

1. **Two separate ActionRegistry instances** — no handler sharing between domains
2. **`todo_refresh_workflow` fully stubbed** — all 5 actions are no-ops
3. **`chat_assistant_workflow` is a no-op** — single terminal state placeholder
4. **Mixed MCP listing** — `workflow_list_definitions` shows all workflows including
   those that can't be executed by the durable worker
5. **Durable system disabled by default** — requires `VON_DURABLE_WORKFLOWS_ENABLE=1`
6. **Pre-existing pyright errors** in `considerations_workflow.py` (None-safety,
   `max_tokens` parameter)

## Jira Task Status

| Issue | Summary | Status | Notes |
|---|---|---|---|
| JVNAUTOSCI-803 | Design and implement LLM Workflows | In Progress | Parent epic |
| JVNAUTOSCI-1054 | MCP Agentic Workflow Enhancement | To Do | |
| JVNAUTOSCI-1075 | Durable Workflow System | Done (Phase 1-3) | Phase 4-5 pending |
| JVNAUTOSCI-1076 | Workflow discovery | Done | |
| JVNAUTOSCI-1077 | Student Onboarding Workflow | Done | |
| JVNAUTOSCI-1082 | Vontology-driven scheduling | To Do | |
| JVNAUTOSCI-1083 | MCP Tools for Workflow Management | In Progress | 12 tools implemented |

## Confluence Pages Updated

- **Von Agentic Workflows Design** (page 87949313) — pending update with
  architecture status, bootstrapped concepts table, known gaps, recent fixes
- **How to design and use Von WorkFlows** (page 96862209) — no changes needed
  (already covers design principles and lifecycle)
