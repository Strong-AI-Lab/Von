# Turn Execution Completion Schema Proposal (Draft)

**Status**: Draft  
**Date**: 2026-02-19  
**Scope**: Conversation-turn execution reliability, postcondition verification, completion gating

## 1. Problem Evidence

Two recent turn traces show a consistent failure mode: the system marks a turn as completed without proving that required mutations occurred.

### 1.1 Request `25bc29ec-3890-4e3b-8980-108d5dcaaaf4`
- Prompt preview: `Gill Dobbie was invited but declined. Proceed with the predicates etc`
- `tools_started=0`, `tools_completed=0`
- Orchestrator reached `completed` with `workflow_task=buttonify`
- No mutation execution evidence exists in tool history

### 1.2 Request `9b6c67f8-9e08-4e07-9b71-389347d4a71a`
- Prompt preview: `Those relations were not added. At least, they don't show in the UI for the event`
- Only tool call: forced retry to `search_concepts` (read-only)
- `result_summary`: `Found 0 concepts for 'Those relations were not added least'`
- Turn still reached `completed` with `workflow_task=buttonify`
- No postcondition workflow step blocked completion

### 1.3 Root Gap
- There is no canonical per-turn contract for:
  - required effects (especially implicit KB mutations),
  - postcondition checks,
  - completion gate decision criteria.
- Current diagnostics are rich but unstructured for enforcement.

## 2. Design Goals

1. Represent each turn as a structured execution contract.
2. Encode required effects before execution.
3. Require critic/postcondition checks for mutation effects.
4. Gate completion on verified evidence, not narration output.
5. Make failures queryable across all conversations.

## 3. Canonical Schema (`turn_execution_record.v1`)

### 3.1 JSON shape

```json
{
  "schema_version": "turn_execution_record.v1",
  "request_id": "string",
  "session_id": "string",
  "namespace": "string",
  "user_id": "string",
  "org_id": "string|null",
  "created_at_utc": "ISO-8601",
  "prompt": {
    "preview": "string",
    "sha256": "hex-string",
    "source": "user_message|system_trigger|event_trigger"
  },
  "workflow_selection": {
    "selected_workflow_id": "string",
    "selector_verdict": "string|null",
    "selector_source": "workflow_selector|default|event_binding",
    "workflow_discovery": {
      "candidate_ids": ["string"],
      "excluded_candidate_ids": ["string"]
    }
  },
  "required_effects": [
    {
      "effect_id": "effect_1",
      "intent_origin": "explicit|implicit",
      "effect_type": "kb_mutation|jira_mutation|file_mutation|external_side_effect|read_only_lookup",
      "description": "string",
      "required_tools": ["string"],
      "targets": [
        {
          "kind": "concept|issue|file|url|text",
          "id": "string"
        }
      ],
      "required_predicates": [
        {
          "source_id": "string",
          "predicate": "string",
          "target": "string"
        }
      ],
      "postcondition_required": true,
      "postcondition_strategy": "state_requery|state_diff|ui_projection|not_required",
      "status": "pending|satisfied|not_satisfied|not_executed|inconclusive",
      "status_reason": "string|null"
    }
  ],
  "execution": {
    "tool_invocations": [
      {
        "tool": "string",
        "status": "ok|error|blocked",
        "started_at_utc": "ISO-8601|null",
        "completed_at_utc": "ISO-8601|null",
        "error": "string|null",
        "result_summary": "string|null",
        "payload_fingerprint": "sha256|null"
      }
    ],
    "diagnostic_events": [
      {
        "sequence_no": 1,
        "at_utc": "ISO-8601",
        "stage": "string",
        "phase": "string",
        "status": "string",
        "tool": "string|null",
        "workflow_task": "string|null",
        "result_summary": "string|null"
      }
    ],
    "retry": {
      "attempts": 0,
      "budget": 0,
      "reason": "string|null"
    }
  },
  "postcondition_checks": [
    {
      "check_id": "check_1",
      "effect_id": "effect_1",
      "check_type": "predicate_exists|predicate_absent|entity_exists|ui_projection",
      "check_tool": "string",
      "check_payload": {},
      "observed": {},
      "status": "verified|not_verified|inconclusive|error",
      "evidence": "string|null",
      "error": "string|null"
    }
  ],
  "critic": {
    "enabled": true,
    "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
    "summary": {
      "verified_count": 0,
      "not_verified_count": 0,
      "inconclusive_count": 0,
      "error_count": 0
    }
  },
  "completion_gate": {
    "workflow_id": "#V#turn_completion_gate_workflow",
    "decision": "completed|partial|failed|escalation_required",
    "decision_reason": "string",
    "blocking_effect_ids": ["effect_1"],
    "safe_to_claim_completion": false,
    "requires_follow_up": true
  },
  "final_response": {
    "response_sha256": "hex-string",
    "completion_claim_detected": true,
    "completion_claim_validated": false
  }
}
```

### 3.2 Required enforcement rules

1. Any `required_effects[*].effect_type` ending in `_mutation` must set `postcondition_required=true`.
2. A turn cannot end with `completion_gate.decision="completed"` if any required effect is `not_satisfied`, `not_executed`, `inconclusive`, or missing checks.
3. If a completion claim is detected in assistant text while mutation effects are unresolved, force `decision="escalation_required"` or `decision="partial"`.

## 4. Storage Mapping

### 4.1 Chat history (authoritative per-turn debug)
- Path: `chat_history.history.$.llm_debug_data.turn_execution_record`
- Reason: keeps diagnostics aligned with existing request-level debug payload

### 4.2 Durable workflow instances
- `workflow_instances.inputs.turn_execution_record`:
  immutable contract at workflow start
- `workflow_instances.workflow_data.turn_execution_runtime`:
  mutable in-flight state (effect statuses, interim checks)
- `workflow_instances.outputs.turn_execution_outcome`:
  terminal critic + completion-gate verdict

### 4.3 Query-optimised projection collection
Create `turn_execution_records` (one doc per assistant turn) to avoid expensive array scans in `chat_history`.

Suggested indexes:

```javascript
db.turn_execution_records.createIndex({ request_id: 1 }, { unique: true, name: "request_id_unique" });
db.turn_execution_records.createIndex({ namespace: 1, created_at_utc: -1 }, { name: "namespace_created_desc" });
db.turn_execution_records.createIndex({ "completion_gate.decision": 1, created_at_utc: -1 }, { name: "decision_created_desc" });
db.turn_execution_records.createIndex({ "required_effects.status": 1, created_at_utc: -1 }, { name: "effect_status_created_desc" });
db.turn_execution_records.createIndex({ "workflow_selection.selected_workflow_id": 1, created_at_utc: -1 }, { name: "workflow_created_desc" });
```

## 5. Workflow Design

### 5.1 New workflow concepts created (MCP)

Created as organisation-scoped instances of `#V#durable_workflow`:

- `#V#conversation_turn_execution_workflow`
- `#V#kb_mutation_postcondition_critic_workflow`
- `#V#turn_completion_gate_workflow`

Descriptions have been attached via `hasDescription` text relations (`en-NZ`).

### 5.2 Proposed responsibilities

### `#V#conversation_turn_execution_workflow`
- derive `required_effects`
- execute tool plan
- hand off to critic workflow
- invoke completion gate workflow

### `#V#kb_mutation_postcondition_critic_workflow`
- evaluate postcondition checks for each mutation effect
- classify each effect: `verified|not_verified|inconclusive|error`

### `#V#turn_completion_gate_workflow`
- compute final decision from effect statuses + critic evidence
- block unverified completion claims

### 5.3 Event binding recommendation

Do not bind these new workflows yet. First implement executable definitions and action handlers, then bind:

- `message.direct_created` -> `#V#conversation_turn_execution_workflow`

The existing `message.direct_created` binding to `#V#chat_assistant_workflow` should be migrated only after parity tests pass.

## 6. MCP Failure-Mining Tooling

### 6.1 Implemented tools

1. `turn_execution_list` (implemented)
- Filters: namespace, date range (`from_utc`, `to_utc`), completion decision, workflow_id, `requires_follow_up`, prompt substring.
- Backed by `turn_execution_records`.

2. `turn_execution_get` (implemented)
- Fetches full structured record by `request_id` (`session_id` alias accepted).

3. `turn_execution_search_failures` (implemented)
- Returns filtered turn records with deterministic failure-mode classification and aggregate counts.
- Adds recommendations for workflow/critic/gate hardening based on observed patterns.

4. `turn_execution_build_benchmark` (implemented)
- Produces reproducible corpus-level metrics, seeded replay cases, and capability-gap signals.
- Intended for ongoing reliability benchmarking and regression tracking.

### 6.2 Why this was needed

- `search_knowledge_base` is text-centric and not deterministic for structured failure triage.
- Reliability analysis requires deterministic filtering and aggregation over execution fields.

## 7. Immediate Implementation Sequence

1. Add writer that emits `turn_execution_record.v1` into `llm_debug_data`.
2. Add `turn_execution_records` projection and indexes.
3. Implement code-backed actions for critic and completion gate.
4. Register executable workflow definitions for the three new workflow IDs.
5. Add regression tests using the two request patterns above:
   - no-tool completion
   - read-only tool on mutation-required prompt
