# Replay programme: deployment and first operational witness

- **Kind:** Dated operational evidence record
- **Lifecycle:** Frozen
- **Authority:** Evidence only; current repair selection lives in [2722](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2722), programme allocation in [2721](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2721)
- **Owner:** Michael Witbrock
- **Evidence date:** 5 September 2026 UTC
- **Review trigger:** New release, actor/resource binding or repeated paired replay
- **Protocol:** [Role convergence test programme](role_convergence_test_programme.md)

## Deployment and represented activation

The user authorised deployment. Before deployment, port 5001 reported old build
`613e729d49d8.dirty`; its startup-seed receipts were missing. Canonical
`./run.sh deploy-main` installed clean `16e4fc79089e8480237e22bc3021874ced5f2cd4`
in detached `Von-runtime-main`. The launcher verified server PID 48688, RAG
worker 49352, concept-index worker 49367, current startup-seed receipts and
running durable worker/scheduler. An initial healthy HTTP response preceded
durable readiness; it was not treated as completed deployment.

The selected digest family was also reconciled through its canonical bootstrap
service, without force. It recognised the reviewed version-7 authority digest,
published version 8 and created the new continuity prompt. Subsequent canonical
read-back found `initialise_scope` and exact prompt text matching the release
(SHA-256 `a39941a5bae51044298308ee5be9e675b68faa0da4f937a2e680fd18101dfbc5`).
The old authored prompt was not overwritten. Existing startup purity drift
was reported as advisory; no baseline was reset to hide it.

## First witness and model observations

The existing 2568 Jira family was selected on actual `/von/generate`, using
normal localhost browser-test session issuance. The actor was the existing
Zhan browser-test identity in SAIL. This proves a test path, not Michael's
personal profile or prospective adoption. The intended first answer was the
newest Jira Task's key, summary and status, grounded in a current read.

| Attempt | Actual result | Interpretation |
|---|---|---|
| GPT-5.5 | `model_not_enabled` before reasoning | Setup failure; provider availability did not establish actor eligibility |
| Terra | Completed after four successful `turn_capabilities` calls, but said read-only Jira search was unavailable and gave no issue answer | Useful-answer failure; no evidence of expired Jira authentication |
| Luna caller | Later canonical result correctly named 2721 as newest at query time, after launching the lab-status digest workflow | Answer reached, but not a clean read-only or Luna-only success: the child used Terra and persisted a digest |

The [bounded evidence extract](../generated/role_programme_replay_2026-09-05.json)
retains request/instance identifiers, actual model-call identities and usage,
catalogue fingerprints, answers, the write receipt and source-artifact hashes.
Private raw task/history/instance material remains in local operator evidence.
These are individual developmental observations, not success-rate estimates.
The live Jira source was not frozen, and planning updates were concurrent.

Michael clarified that Luna is his ordinary Von model and requested interest
in Terra/Luna differences. The programme therefore uses Luna as principal and
paired Terra trials on selected cases. This first Luna attempt demonstrates why
the child model must also be recorded: its outer calls used Luna, while two
digest calls used the test actor's active Terra selection. It cannot establish
that Luna alone solved a case Terra could not.

The Luna observer returned pending/inconclusive and did not execute the remaining
three follow-ups. A subsequent authenticated read found the first turn completed
and retrieved its actual result. No new competing task was submitted to replace
that turn. The collector's interim result and later canonical result are both
retained; the four-turn family is not passed.

## Connector versus capability authority

The live Terra trace reported `gateway_present=true`, `gateway_enabled=true`
and a populated tool catalogue. Its capability-discovery calls succeeded.
The deployed catalogue explicitly excludes `jira_search`, `jira_get_issue` and
`jira_get_comments` from ordinary-turn projection with
`deployment_global_account`.

Both the connected Von MCP and a trusted-operator gateway invocation from the
deployed checkout successfully read 2721. The latter used that checkout's
configured Jira proxy and returned no authentication error. This operator probe
does not grant ordinary users access, but distinguishes working connectivity
and credentials from the separate capability projection. Reauthentication was
not indicated; no credential, model-pool or Rovo configuration was changed.

Luna found a different route: the existing represented digest capability could
read Jira. Its durable instance `0204da15-3c9a-498a-b79f-01299f91a691` reached
canonical `completed` and exact text verification. Its actual write targeted
the browser-test actor's existing `#V#operational_reliability_status_digest`.
The receipt records a succeeded text upsert and one replaced relation. The
prior content projection exposed only the product description, so it cannot
establish the previous content bytes or support a speculative exact rollback.
No claim is made that this was zero-write execution or that nothing pre-existed.

This is a concrete reason for unique test products, pre-state snapshots and
effect inspection in the next cycle. It is also a reason to make authorised
Jira reads directly available through the correct resource boundary instead of
rewarding a workaround with unrelated durable work. Do not infer that all
authenticated users should receive the deployment account's access.

## Scope of the handoff

The test-programme document and existing replay-input correction passed all
19 tests in `tests/test_run_live_multi_turn_followup_replay.py`; document links
and diff checks passed. This supports the programme/input change, not a repaired
Jira capability or role certification.

The full witness and real-role claim remain open. The observations identify a
specific integration decision and a mixed-model/effect confound to remove
before interpreting model differences. The next implementation decision belongs
in 2722; this dated record does not prescribe a second repair plan.
