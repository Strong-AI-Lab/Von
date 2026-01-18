# Workflow Model Policy Schema (Draft)

## Purpose
Define a **Vontology-backed model policy** that selects models per workflow stage, with safe fallback chains and backward compatibility (single-model default).

This document supports JVNAUTOSCI-994 (subtask of JVNAUTOSCI-993).

## Vontology representation (proposed)
### Core concepts
- `#V#workflow_model_policy`
- `#V#workflow_stage`
- `#V#model_capability`
- `#V#foundation_model`

### Predicates
- `#V#applies_to_workflow_stage`
- `#V#has_model_policy_json` (text relation)
- `#V#has_fallback_model`
- `#V#has_capability_tag`
- `#V#has_cost_tier`
- `#V#has_latency_tier`

### Required metadata
- policy scope (global, organisation, user, programme/project)
- policy provenance (author, timestamp)
- explicit compatibility note: default policy preserves single-model behaviour

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
```json
{
  "policy_id": "#V#default_workflow_model_policy",
  "scope": "global",
  "inherit_from": null,
  "stages": {
    "planner": {
      "primary": "active_llm",
      "fallback": ["ollama:granite3.3:2b"],
      "constraints": {"local_only": false}
    },
    "tool_call": {
      "primary": "active_llm",
      "fallback": ["ollama:granite3.3:2b"],
      "constraints": {"local_only": false}
    },
    "tool_recovery": {
      "primary": "ollama:granite3.3:2b",
      "fallback": ["active_llm"],
      "constraints": {"local_only": true}
    },
    "classifier": {
      "primary": "ollama:granite3.3:2b",
      "fallback": ["active_llm"],
      "constraints": {"local_only": true}
    },
    "critic": {
      "primary": "active_llm",
      "fallback": [],
      "constraints": {"local_only": false}
    },
    "screen_backfill": {
      "primary": "active_llm",
      "fallback": ["ollama:granite3.3:2b"],
      "constraints": {"local_only": false}
    },
    "narration": {
      "primary": "active_llm",
      "fallback": ["ollama:granite3.3:2b"],
      "constraints": {"local_only": false}
    },
    "summariser": {
      "primary": "active_llm",
      "fallback": ["ollama:granite3.3:2b"],
      "constraints": {"local_only": false}
    },
    "buttonify": {
      "primary": "ollama:granite3.3:2b",
      "fallback": ["active_llm"],
      "constraints": {"local_only": true}
    }
  },
  "constraints": {
    "local_only_stages": ["tool_recovery", "classifier", "buttonify"],
    "max_fallback_hops": 2
  },
  "compatibility": {
    "single_model_default": true,
    "notes": "Default policy keeps current single-model behaviour until policy resolution is implemented."
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
The first policy instance **must preserve current behaviour** by mapping all stages to the active LLM, with optional local-only fallbacks for low-risk classifier stages. No runtime behaviour changes are required until the orchestrator consumes this policy.

## Next steps
- Resolve canonical Vontology IDs for the above concepts/predicates.
- Store the policy JSON as a text relation on `#V#workflow_model_policy`.
- Add a brief note in the Conversation Turn Workflow doc describing the mapping (see [docs/Conversation Turn Workdlow.md](docs/Conversation%20Turn%20Workdlow.md)).
