# Authenticated Browser Workflow Replay Harness

`scripts/run_authenticated_browser_workflow_replay.py` provides a reusable
real-path replay harness for browser-backed workflow acceptance.

It uses existing Von support surfaces:

- the isolated agent-test backend, normally started on macOS/Linux with
  `./run.sh restart -AgentTest -HealthTimeoutSec 180`;
- the local-only browser-test login endpoint
  `/von/api/auth/browser-test-login`;
- the normal chat submission endpoint `/von/generate`;
- background task polling via `/von/api/task/status/<task_id>` and
  `/von/api/task/result/<task_id>`;
- Thinking-card progress polling via `/von/progress/<request_id>`.

The harness does not add workflow policy to Python. Workflow-specific
expectations are replay-case data and assertions over emitted telemetry and
progress facts.

Before submitting `/von/generate`, the harness now records and enforces the
operational preconditions that make the replay user-equivalent:

- Mongo read/write health via `/admin/db/health?probe=rw`;
- effective session user/org context via `/von/api/session/context`;
- target user/org model readiness via `/api/settings/llm/info`, including
  selected Ollama model availability when the target model is local;
- workflow capability-index availability via
  `/api/workflows/capability-index/status`;
- Gmail OAuth/profile readiness only for replay cases that actually require
  Gmail.

If one of these preconditions is unavailable, the report is still useful
evidence, but it is classified as a typed blocker and the harness does not
submit a misleading turn by default.

For replay runs the login request explicitly sets `refresh_fixture=false`.
Browser-test fixture preparation is real represented data and can be useful for
browser user-view checks, but it is not part of the auth precondition for a
workflow selector replay. Keeping it separate prevents login from silently
triggering fixture writes, recommendation-workflow events, or capability-index
rebuilds before the replay reaches `/von/generate`.

## Gmail/arXiv 2421 Case

To run the motivating `JVNAUTOSCI-2421` acceptance prompt:

```sh
VON_BROWSER_TEST_AUTH_ENABLED=1 ./run.sh restart -AgentTest -HealthTimeoutSec 180
./.venv/bin/python scripts/run_authenticated_browser_workflow_replay.py \
  --case gmail-arxiv-2421 \
  --thinking-card-mode debug \
  --output-json tmp/jvnautosci-2422-gmail-arxiv-replay.json
```

If a Gmail profile should be forced, pass `--gmail-profile <profile_id>`.
Otherwise the harness reads `/api/settings/` and uses the server default Gmail
profile when one is configured.

By default the harness stops before `/von/generate` when the Gmail preflight
already proves that no configured profile or token set is available. Pass
`--run-despite-gmail-preflight-blocker` only when deliberately gathering
downstream selector or workflow evidence despite that known external blocker.

The harness also stops before `/von/generate` when the workflow capability index
status endpoint reports that represented workflow discovery is unavailable.
That is a hard replay precondition: selector evidence captured while the
authoritative discovery surface is missing is not user-equivalent evidence. The
preflight waits through a bounded transient build/reload window before blocking,
and records the sampled status snapshots in the JSON report.
Pass `--run-despite-workflow-capability-preflight-blocker` only for diagnostic
experiments that should still be classified as blocked by that precondition.

For exact user-turn investigations, use an ad-hoc prompt plus explicit target
context instead of forcing the nearest named replay case. Example:

```sh
./.venv/bin/python scripts/run_authenticated_browser_workflow_replay.py \
  --base-url http://localhost:5001 \
  --allow-non-agent-test-server \
  --case jvnautosci-2560-exact-arxiv \
  --prompt 'Please ingest https://arxiv.org/abs/2406.15341 into Von, including the normal download/file-copy path if available, then read back the represented paper concept, file-copy, and blob evidence so I can see what was stored.' \
  --expected-workflow-id '#V#arxiv_paper_representation_workflow' \
  --target-user-concept-id '#V#michael_witbrock' \
  --target-organisation-concept-id '#V#university_of_auckland_strong_ai_lab' \
  --thinking-card-mode debug \
  --output-json tmp/jvnautosci-2560-exact-arxiv-replay.json
```

## Evidence Captured

The JSON report records:

- local and server branch/commit/environment evidence;
- browser-test auth login result and authenticated status;
- target user/org session context;
- Mongo read/write preflight;
- effective model readiness preflight;
- workflow capability index preflight, including user-visible blocker state;
- Gmail profile/OAuth preflight when relevant;
- chat session and `/von/generate` submission identifiers;
- task terminal status and result;
- live Thinking-card progress snapshots;
- progress projection fact ids, contract ids, source paths, redaction state,
  and values when not redacted;
- selected workflow evidence;
- a pass verdict or a typed blocker.

Typed blockers include:

- `browser_test_auth_blocker`;
- `target_session_context_blocker`;
- `database_runtime_blocker`;
- `llm_runtime_blocker`;
- `workflow_capability_index_blocker`;
- `gmail_oauth_or_profile_blocker`;
- `context_build_blocker`;
- `workflow_action_terminal_state_blocker`;
- `task_terminal_state_blocker`;
- `selector_or_dispatch_blocker`;
- `thinking_card_projection_blocker`.

For the Gmail/arXiv case, a full pass requires selecting
`#V#zhan_gmail_arxiv_ingestion_workflow` and observing the authored progress
projection facts from `JVNAUTOSCI-2421`. A local run may still be acceptable
evidence for `JVNAUTOSCI-2422` when it emits a precise Gmail profile/OAuth
blocker, because the harness itself has then proven the authenticated replay
path and identified the missing external precondition.

## Multi-turn outcome rebaselines

The companion `run_live_multi_turn_followup_replay.py` and its JSON bank retain
useful task families independently of older routing and telemetry designs.
The bank's `assessment_scope: outcome` keeps expected workflow identity and
observed action failures in `integration_checks`, separate from outcome checks.
A competent alternative route or successful recovery does not by itself fail
the user job. Callers explicitly testing workflow integration can retain the
legacy default `workflow_integration` scope. Completion-gate rejection still
fails the outcome checks.

The shipped bank requires a separate source-grounded review. Its automated
verdict is therefore `inconclusive` when its checks pass but that review is
outstanding, and `fail` when a checked requirement fails. Preserve both the
machine result and the review disposition; do not rewrite inconclusive machine
results as automated passes. Keyword or distinct-ID coverage alone cannot prove
that the intended targets were accurately described or that effects persisted.
For the dynamic two-issue Jira case, two distinct issue keys are necessary;
review must additionally confirm the requested issue and its immediate
predecessor against Jira, and assess each summary.

Legacy obligation-suppression probes remain available for historical integration
replays. An absent ledger or decision is unavailable evidence, not a successful
negative check. The current outcome bank no longer requires the retired ledger
shape. Retire obsolete implementation expectations rather than useful jobs;
retain failed current jobs in the results.

`visible_answer` is retained as a compatibility field. Its source is a server
turn record or task result; it does **not** prove browser delivery. Reports name
that surface and report `browser_delivery: not_observed`. A completed transport
without an answer fails, and saved prose cannot establish that the UI displayed
it. Browser acceptance needs a separate actual browser observation. Likewise,
browser-test fixture authentication does not establish normal owner authority
for integrations such as Jira.

For a bounded rebaseline, freeze a small selection of existing jobs, the model,
entry surface, effect scope and review criteria before submission. Use fresh
sessions for independent families, preserve context within each family, and
reconcile pending requests before any retry. Verify source facts and canonical
read-back for effects, record model identity and release, and report pass,
failure, blocked or unassessed outcomes separately. A single representative
sample is not a reliability rate, a model ranking or broad certification.
