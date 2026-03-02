# KB-Clone Benchmark Harness

`JVNAUTOSCI-1294` introduces an isolated benchmark harness for agentic conversational evaluation runs.

## Purpose

The harness runs scripted conversational scenarios against a temporary clone of a source KB database, records structured evaluation outputs, archives pre/post KB snapshots, and then performs guarded teardown.

## Lifecycle

1. Clone source DB into an isolated benchmark clone DB.
2. Inject dedicated benchmark user and organisation concepts (via Vontology MCP `create_concepts`).
3. Run scripted turns via Von API route `/von/generate` (real call path).
4. Capture per-turn outcomes, timings, tool-invocation traces, and failures.
5. Export deterministic compressed snapshots:
1. `pre_run_snapshot.tar.gz` (after identity injection, before turns)
2. `post_run_snapshot.tar.gz` (after scenario execution)
6. Verify archive checksums and readability.
7. Teardown policy:
1. Default: delete clone DB after successful archive verification.
2. If `retain_on_failure=true` and scenario fails: retain clone for debugging.
3. If archive verification fails: retain clone (never silently delete).
4. If teardown attempt fails: run status is marked incomplete with diagnostics.

## Safety Guardrails

- Source DB `von_db` is blocked.
- By default, source DB must look isolated (`test_*`, `benchmark_*`, `*_test`, `*_benchmark`).
- Use `--allow-non-test-db` only when you explicitly understand the risk.

## Running

```powershell
python scripts/run_kb_clone_benchmark.py `
  --scenario docs/engineering/kb_clone_benchmark_scenario.example.jsonc `
  --output-dir artifacts/benchmark_runs
```

Optional flags:

- `--source-db-name <name>`
- `--clone-db-prefix <prefix>`
- `--retain-on-failure`
- `--allow-non-test-db`
- `--mongo-uri <uri>`

## Run Bundle Output

Each run writes:

- `manifest.json`: run metadata, workflow/model configuration snapshot, teardown status.
- `metrics.json`: per-run/per-turn outputs and aggregate reliability metrics.
- `archives/pre_run_snapshot.tar.gz`
- `archives/post_run_snapshot.tar.gz`

Aggregate reliability metrics include:

- `pass_at_k`
- `completion_consistency`
- `mean_turn_latency_ms`

## Scenario Schema (pragmatic)

Required:

- `scenario_id: str`
- `turns: list[object]` with each turn containing `prompt: str`

Common optional fields:

- `source_db_name: str`
- `mode: \"single\" | \"reliability\"`
- `runs: int`
- `retry_budget: int`
- `model: str`
- `retain_on_failure: bool`
- `identity.user_name: str`
- `identity.organisation_name: str`
- `turn.expected_substrings: list[str]`
- `turn.forbidden_substrings: list[str]`
- `turn.expected_regex: str`
- `turn.retry_budget: int`
