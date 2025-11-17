# `src/` Directory Overview

The `src/` tree houses the Von runtime, tooling, and research assets. Use this guide to understand the responsibilities of each area and where to extend functionality.

## backend/
Full server stack for the Vontology platform.
- `server/` Flask app, route handlers, and helpers exposed to the UI and MCP clients.
- `services/` Core domain logic for concepts, annotations, relation elicitation, chat history, similarity, and text values (with focused tests in `services/tests`).
- `db/` Mongo connection management plus repositories for concept, relation, and text-value persistence.
- `integrations/` Bridges to internal/external MCP tools (arXiv proxy, orchestration gateway) and JIRA automation.
- `languagemodels/` Shared LLM interface plus Ollama/OpenAI clients and utilities.
- `mcp_server/` HTTP and stdio MCP servers kept in sync with the internal gateway catalogue.
- `models/`, `prompt/`, `security/`, `utilities/`, `utils/`, and `vontology/` provide schema definitions, prompt templates, access control, maintenance scripts, general helpers, and Vontology-specific utilities respectively.

## frontend/
Currently focuses on the `web/von_interface` Flask UI, with `templates/` for Jinja views and `static/` assets for the embedded terminal-inspired experience.

## knowledge/
Curated, versioned Vontology source material (`knowledge/vontology`) that seeds and documents concept hierarchies for research workflows.

## memories/
Cloud-facing memory back ends (`memories/cloud`), currently geared towards Google Drive synchronisation hooks.

## utilities/
Standalone operational scripts for maintainers, including local database initialisation, Ollama control helpers, remote test harnesses, and cleanup utilities.

## workflows/
Composable workflow definitions (e.g., `onboarding_workflow.py` and `von/main.py`) that orchestrate services, memories, and personas for specific research tasks.

## Supporting files
- `GEMINI.md` captures recent tool-usage learnings for Gemini-related automation.
- `__init__.py` marks the package root so modules under `src/` can be imported using absolute paths.
