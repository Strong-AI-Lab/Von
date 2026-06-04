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

## Evidence Captured

The JSON report records:

- local and server branch/commit/environment evidence;
- browser-test auth login result and authenticated status;
- Gmail profile/OAuth preflight;
- chat session and `/von/generate` submission identifiers;
- task terminal status and result;
- live Thinking-card progress snapshots;
- progress projection fact ids, contract ids, source paths, redaction state,
  and values when not redacted;
- selected workflow evidence;
- a pass verdict or a typed blocker.

Typed blockers include:

- `browser_test_auth_blocker`;
- `gmail_oauth_or_profile_blocker`;
- `task_terminal_state_blocker`;
- `selector_blocker`;
- `thinking_card_projection_blocker`.

For the Gmail/arXiv case, a full pass requires selecting
`#V#zhan_gmail_arxiv_ingestion_workflow` and observing the authored progress
projection facts from `JVNAUTOSCI-2421`. A local run may still be acceptable
evidence for `JVNAUTOSCI-2422` when it emits a precise Gmail profile/OAuth
blocker, because the harness itself has then proven the authenticated replay
path and identified the missing external precondition.
