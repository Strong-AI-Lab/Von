# Von - AI Development Instructions

This project is an AI-agent system for academic research (knowledge management, ontologies).
Follow these instructions to be productive and compliant with project standards.

## 1. Architecture & Boundaries
- **Core Principle**: "Vontology" (ontology graph) is the source of truth, not just the DB.
- **Backend (`src/backend/`)**: Python/Flask application.
  - `vontology/`: Core logic for concepts and relationships.
  - `server/`: Flask routes and API definitions.
  - `integrations/internal_mcp/`: Gateway for MCP tool handling.
- **Frontend (`src/frontend/web/`)**: React/TypeScript application.
- **Data**: MongoDB is the persistence layer (`von_db` or `test_von_db`).

## 2. Development Workflow
- **Shell**: **PowerShell** is mandatory. Do not use Bash syntax (`export`, `ls`, etc.).
- **Package Manager**:
  - Python: `pdm` (e.g., `pdm run pytest`).
  - JS: `npm` (e.g., `npm run test`).
- **Running the App**: `./run.ps1` (starts Flask + frontend).
- **Testing**:
  - Backend: `pdm run pytest tests/backend` (Use `pytest:backend (test db)` task for safety).
  - Frontend: `npm run test:frontend`.
  - **Critical**: Never run tests against accessing the production `von_db`.

## 3. Coding Conventions
- **Language**: Use **New Zealand English** spelling (e.g., "behaviour", "visualise").
- **Vontology-First**:
  - **Read**: Use Vontology services/MCP tools to fetch data.
  - **Write**: Use Vontology services/MCP tools to mutate data.
  - **Avoid**: Direct raw MongoDB calls for ontology data unless writing low-level services.
- **Refactoring**: Implement the "3-strike rule". If code is repeated 3 times, refactor into valid helper in `utils/` or `services/`.
- **Error Handling**: Fail fast with diagnostic messages (expected vs actual).

## 4. Agent Specific Rules
- **Tooling**: Prefer MCP tools (Vontology, Jira) over direct code manipulation for data tasks.
- **Validation**: Check `get_problems` or `pyright` results after editing files.
- **Documentation**: Update `AGENTS.md` if discovering new critical patterns.
- **Git**: Keep changes small. Fast-forward merge into `main` after tests pass.

## 5. Key File Locations
- `src/backend/integrations/internal_mcp/catalogue.py`: Definition of internal tools.
- `AGENTS.md`: Detailed behavioral rules for AI agents.
- `tests/backend/`: Backend test suite (consult for API usage examples).
