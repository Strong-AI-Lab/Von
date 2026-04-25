# Workflow Model Policy Schema (Draft)

## Purpose
Define a **Vontology-backed model policy** that selects models per workflow stage, with safe fallback chains and backward compatibility (`active_llm` as the default single-model behaviour).

This document supports JVNAUTOSCI-994 (subtask of JVNAUTOSCI-993) and JVNAUTOSCI-998.

## Vontology representation (implemented)
### Core concepts
- `#V#workflow_model_policy` - Type for policy definitions
- `#V#workflow_stage` - Type for stage definitions
- `#V#workflow_stage_configuration` - Type for stage-specific configuration
- `#V#model_capability` - (planned) capability annotations
- `#V#foundation_model` - (planned) model definitions

### Stage instances (created per JVNAUTOSCI-998)
- `#V#planner_stage`
- `#V#tool_call_stage`
- `#V#tool_recovery_stage`
- `#V#classifier_stage`
- `#V#critic_stage`
- `#V#screen_backfill_stage`
- `#V#narration_stage`
- `#V#summariser_stage`
- `#V#buttonify_stage`

### Stage configuration instances
- `#V#default_planner_config`
- `#V#default_tool_call_config`
- `#V#default_tool_recovery_config`
- `#V#default_classifier_config`
- `#V#default_critic_config`
- `#V#default_screen_backfill_config`
- `#V#default_narration_config`
- `#V#default_summariser_config`
- `#V#default_buttonify_config`

### Predicates
- `#V#applies_to_workflow_stage` - Links config → stage
- `#V#has_stage_configuration` - Links policy → configs
- `#V#has_model_policy_json` (text relation) - Legacy JSON storage
- `#V#has_primary_model` (text relation) - Primary model for stage
- `#V#has_fallback_model` (text relation) - Fallback model(s)
- `#V#has_local_only_constraint` (text relation) - Local-only flag
- `#V#has_max_fallback_hops` (text relation) - Global constraint
- `#V#uses_prompt` - Links stage → prompt concept
- `#V#has_capability_tag` - (planned)
- `#V#has_cost_tier` - (planned)
- `#V#has_latency_tier` - (planned)

### Current linked prompts
- `#V#buttonify_stage` → `#V#buttonify_prompt_v1`
- `#V#narration_stage` → `#V#von_chat_narration_prompt_for_witbrock`
- `#V#screen_backfill_stage` → `#V#von_screen_content_prompt_for_witbrock`
- `#V#classifier_stage` → `#V#missing_tool_call_classifier_prompt`
- `#V#tool_recovery_stage` → `#V#tool_call_repair_prompt`

### Required metadata
- policy scope (global, organisation, user, programme/project)
- policy provenance (author, timestamp)
- explicit compatibility note: default policy preserves single-model behaviour via `active_llm`
- override semantics: stage-specific model overrides must be explicit and telemetry-visible

## Stage taxonomy (initial)
- `planner`
- `tool_call`
- `tool_recovery`
- `classifier`
- `critic`
- `screen_backfill`
- `narration`
- `summariser`
- `buttonify`

## Policy JSON schema (draft)

Model references in policy JSON use the format `provider:model_name`.
The special token `active_llm` refers to whatever the orchestrator's active
LLM client is (resolved from user/org settings → environment default).

Default model names are no longer hardcoded.  They are resolved at startup from
environment variables (see `src/backend/languagemodels/model_defaults.py`):

| Variable | Fallback |
|---|---|
| `VON_DEFAULT_OLLAMA_MODEL` | `gemma4:26b` |
| `VON_DEFAULT_OPENAI_MODEL` | `gpt-4.1-mini` |
| `VON_DEFAULT_GEMINI_MODEL` | `gemini-2.0-flash` |

When writing policy JSON, prefer `active_llm` for stages that should follow
the user's primary model selection, and use explicit `provider:model` strings
only when a stage genuinely requires a specific model or provider. Such stage
overrides should be visible in telemetry/UI so they do not look like silent
model drift.

Longer term, stage matching should move toward capability-oriented references
resolved through the model registry (for example “cheap reliable classifier”
or similar capability/profile metadata) rather than hard-coded concrete model
IDs. The explicit `provider:model` form remains the current concrete mechanism,
not the desired end-state for learned or capability-based model selection.
For the actor-critic learning loop that should produce and retire those
capability certifications from real conversation rollouts, see
`docs/engineering/automated_policy_learning_design.md`, especially the
`JVNAUTOSCI-2090` model-use learning section.

```json
{
  "policy_id": "#V#default_workflow_model_policy",
  "scope": "global",
  "inherit_from": null,
  "stages": {
    "planner": {
      "primary": "active_llm",
      "fallback": ["ollama:default"],
      "constraints": {"local_only": false}
    },
    "tool_call": {
      "primary": "active_llm",
      "fallback": ["ollama:default"],
      "constraints": {"local_only": false}
    },
    "tool_recovery": {
      "primary": "active_llm",
      "fallback": [],
      "constraints": {"local_only": false}
    },
    "classifier": {
      "primary": "active_llm",
      "fallback": [],
      "constraints": {"local_only": false}
    },
    "critic": {
      "primary": "active_llm",
      "fallback": [],
      "constraints": {"local_only": false}
    },
    "screen_backfill": {
      "primary": "active_llm",
      "fallback": ["ollama:default"],
      "constraints": {"local_only": false}
    },
    "narration": {
      "primary": "active_llm",
      "fallback": ["ollama:default"],
      "constraints": {"local_only": false}
    },
    "summariser": {
      "primary": "active_llm",
      "fallback": ["ollama:default"],
      "constraints": {"local_only": false}
    },
    "buttonify": {
      "primary": "active_llm",
      "fallback": [],
      "constraints": {"local_only": false}
    }
  },
  "constraints": {
    "local_only_stages": [],
    "max_fallback_hops": 2
  },
  "compatibility": {
    "single_model_default": true,
    "notes": "Default policy follows the active selected LLM for all stages unless an explicit stage override is authored."
  }
}
```

Example explicit stage override (deliberate and telemetry-visible):

```json
{
  "stages": {
    "classifier": {
      "primary": "openai:gpt-4o-mini",
      "fallback": ["active_llm"],
      "constraints": {"local_only": false}
    }
  }
}
```

## Model registry JSON (draft)
```json
{
  "model_id": "gpt-5.2-chat-latest",
  "provider": "openai",
  "base_url": null,
  "locality": "external",
  "capabilities": ["reasoning", "tool_calling", "summarisation"],
  "limits": {
    "max_context_tokens": 128000,
    "max_output_tokens": 4096
  },
  "cost_tier": "high",
  "latency_tier": "medium"
}
```

## Compatibility note
The default policy instance **must preserve current behaviour** by mapping all stages to `active_llm` unless an explicit stage override is authored. If an override exists, runtime telemetry should make that override obvious rather than presenting it as an unexplained mismatch against the selected model.

## Implementation status (JVNAUTOSCI-998)
Completed:
- ✅ Resolved canonical Vontology IDs for concepts/predicates
- ✅ Created stage instances and configurations in Vontology
- ✅ Stored model/fallback/constraint relations on stage configs
- ✅ Linked stages to prompt concepts where applicable
- ✅ Added graph-based policy resolver (`workflow_policy_graph_service.py`)
- ✅ Integrated graph resolver into orchestrator with JSON fallback
- ✅ Added diagnostic endpoint `/admin/policy_comparison`

Remaining:
- Add tests for graph resolver
- Update Conversation Turn Workflow doc (see [docs/Conversation Turn Workdlow.md](docs/Conversation%20Turn%20Workdlow.md))
- Once graph parity is confirmed via diagnostics, consider deprecating JSON

## Diagnostic endpoint
`GET /admin/policy_comparison?policy_id=#V#default_workflow_model_policy`

Returns a comparison report between JSON and graph representations, showing mismatches.

