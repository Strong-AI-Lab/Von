# Pytest Lane Strategy

`JVNAUTOSCI-1400` establishes the authoritative backend pytest workflow for
Von. The backend suite is now large enough that a single monolithic
`pdm run pytest tests/backend` invocation is not the normal interactive
validation path.

## Canonical Commands

Audit the current inventory and lane sizes:

```powershell
pdm run python scripts/pytest_lanes.py list
```

Recommend targeted validation from the current branch diff:

```powershell
pdm run python scripts/pytest_lanes.py recommend --git-diff origin/main --risk normal
```

Run one deterministic lane:

```powershell
pdm run python scripts/pytest_lanes.py run-lane backend-mcp
```

Run an explicit targeted set of test files:

```powershell
pdm run python scripts/pytest_lanes.py run-targets tests/backend/test_internal_mcp_catalogue_builds.py tests/backend/test_mcp_manifest_parity.py
```

Print the shardable aggregate full-coverage plan:

```powershell
pdm run python scripts/pytest_lanes.py aggregate-plan
```

The runner defaults `VON_DB_NAME` to `test_von_db` when it is unset and
refuses to run if `VON_DB_NAME=von_db`.

## Aggregate Lanes

These lanes are the canonical shard sequence for broader automated backend
coverage:

| Lane | Purpose |
| --- | --- |
| `backend-core` | Services, utilities, models, and general backend regressions |
| `backend-routes` | Flask routes, endpoints, and request-surface regressions |
| `backend-mcp` | Internal MCP, stdio server, orchestrator, and tool wiring |
| `backend-workflows` | Workflow execution, scheduling, transitions, and durable state |
| `infra` | Deployment and infrastructure pytest coverage |

`manual` remains separate and is excluded from automated aggregate coverage by
default.

## Overlay Suites

These are deterministic markers for escalation rather than aggregate shards:

| Overlay | Purpose |
| --- | --- |
| `backend-integration` | Broader call-path and end-to-end style coverage |
| `backend-external-like` | Jira, Gmail, GitHub, arXiv, stdio, deployment, and similar boundaries |
| `backend-heavy` | Large or slower suites that should be run deliberately |

## Changed-Area Mapping

Use `recommend --git-diff` when possible. If you need to reason manually, use
this mapping:

| Changed area | Primary lane | Common escalation |
| --- | --- | --- |
| `src/backend/server/routes/**` | `backend-routes` | `backend-integration`, then `backend-heavy` for higher-risk route work |
| `src/backend/integrations/internal_mcp/**` or `src/backend/mcp_server/**` | `backend-mcp` | `backend-heavy`, `backend-external-like` |
| `src/backend/workflows/**` | `backend-workflows` | `backend-heavy`, `backend-integration` |
| `src/backend/integrations/**` (non-MCP) | `backend-core` | `backend-external-like` |
| `src/backend/services/**`, `src/backend/utils/**`, `src/backend/db/**`, `src/backend/vontology/**`, most other backend Python | `backend-core` | lane-specific overlays only when the change crosses broader boundaries |

## Validation Policy

- Default interactive validation is the targeted impacted set plus the relevant
  deterministic lane or overlay.
- “Full backend coverage” means running the aggregate lane sequence from
  `aggregate-plan`, not claiming that one monolithic pytest command was run.
- Do not claim a full pytest run unless the relevant aggregate lanes were
  actually executed.
- Manual suites stay manual unless a task specifically requires them.
