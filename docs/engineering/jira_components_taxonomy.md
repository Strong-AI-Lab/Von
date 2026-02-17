# Jira Components Taxonomy for Von

This file defines a candidate Jira `Components` list for Von work. It is designed to match real subsystem boundaries in the current codebase and Von's product purpose (ontology-first agentic chat, workflows, and integrations).

## Suggested Naming Convention
- Use short, stable component names with title case.
- Prefer one primary component per issue.
- Add a second component only when work clearly spans two subsystems.

## Candidate Components

| Component | Scope | Code Anchors |
| --- | --- | --- |
| Vontology Core | Concept graph lifecycle, predicate/type semantics, canonical IDs | `src/backend/services/concept_service.py`, `src/backend/vontology/utils_vontology.py` |
| Vontology Text Relations | Names/descriptions/content text relation CRUD and policies | `src/backend/services/text_value_service.py`, `src/backend/services/concept_relation_service.py` |
| Vontology Search & Embeddings | Concept search, resolve-by-name, embedding index behaviour | `src/backend/services/concept_search_service.py`, `src/backend/services/concept_embedding_service.py` |
| RAG & Retrieval | Indexing, retrieval, provenance, namespace-aware queries | `src/backend/services/rag_service.py`, `src/backend/services/rag_sync_service.py` |
| Internal MCP Framework | Internal gateway, transport, tool catalogue runtime | `src/backend/integrations/internal_mcp/catalogue.py`, `src/backend/integrations/internal_mcp/gateway.py` |
| MCP Server Surface & Contracts | Stdio tool exposure, manifest parity, input/output schemas | `src/backend/mcp_server/mcp_stdio_server.py`, `src/backend/mcp_server/vontology_mcp.json` |
| Chat Orchestrator | LLM/tool loop, tool-call recovery, turn execution behaviour | `src/backend/integrations/internal_mcp/orchestrator.py` |
| Prompting & Model Policy | Prompt templates, model registry/policy selection | `src/backend/services/prompt_template_service.py`, `src/backend/services/model_registry_service.py` |
| Renderer Applicability & Profiles | Renderer applicability logic, profile resolution, metadata | `src/backend/services/renderer_applicability_service.py`, `src/backend/services/renderer_applicability_vontology_service.py` |
| Display Elements & Presenter Protocol | Render plan, screen/spoken channel shaping, display contracts | `src/backend/services/display_elements_service.py`, `src/backend/server/routes/von_routes.py` |
| Frontend Chat UX | Main chat interaction model and rendering behaviour | `src/frontend/web/von_interface/static/js/chatTab.js` |
| Frontend Debug UX | Debug panels, provenance display, developer-facing UX affordances | `src/frontend/web/von_interface/static/js/components/messagePanel.js`, `src/frontend/web/von_interface/static/js/components/promptCartoucheOverlay.js` |
| Conversation-Turn Workflows | In-turn workflow selection and execution | `src/backend/workflows/workflow_selector.py`, `src/backend/workflows/engine.py` |
| Durable Workflows & Scheduler | Persistent workflow instances, schedules, worker execution | `src/backend/workflows/durable/scheduler.py`, `src/backend/workflows/durable/worker.py` |
| Auth, Session & Namespace | Login/session identity plumbing and namespace derivation | `src/backend/server/routes/auth_routes.py`, `src/backend/services/namespace_service.py` |
| Access Control & Write Guardrails | Write-safety policy, authz checks, guarded operations | `src/backend/workflows/write_tool_policy.py`, `src/backend/server/routes/admin_routes.py` |
| Task & Organisation | Task domain model, organisation membership, task routes | `src/backend/services/task_management_service.py`, `src/backend/services/organisation_membership_service.py` |
| Jira Integration | Jira proxy tools, schema mapping, Jira-specific reliability | `src/backend/integrations/internal_mcp/jira_proxy_mcp.py`, `src/backend/mcp_server/mcp_server.py` |
| Gmail Integration | Gmail OAuth, token handling, message tools | `src/backend/services/agent_gmail_oauth_service.py`, `src/backend/server/routes/agent_gmail_oauth_routes.py` |
| arXiv & Web Research | arXiv metadata/download and web extraction/search pathways | `src/backend/integrations/internal_mcp/arxiv_proxy_mcp.py`, `src/backend/integrations/internal_mcp/search_proxy_mcp.py` |
| Speech & Audio | TTS/speech ingestion and related route behaviour | `src/backend/server/routes/speech_routes.py` |
| Observability & Tracing | Workflow traces, debug metadata, diagnostics surfaces | `src/backend/workflows/trace_store.py`, `src/backend/server/routes/von_routes.py` |
| Data Safety & Migration | Migration tooling, safety guards, backfill utilities | `src/backend/utilities/migrate_workflow_mapping_specs.py`, `src/backend/db/mongo_client.py` |
| Developer Tooling & CI | Test harness behaviour, hooks, lint/type gates, automation | `.githooks`, `pytest.ini`, `pyproject.toml` |

## Default Starter Set (if you want fewer components initially)
- Vontology Core
- RAG & Retrieval
- Internal MCP Framework
- Chat Orchestrator
- Display Elements & Presenter Protocol
- Frontend Chat UX
- Durable Workflows & Scheduler
- Auth, Session & Namespace
- Task & Organisation
- Jira Integration
