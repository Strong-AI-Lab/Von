# Authorised Jira read replay — 5 September 2026

- **Kind:** Bounded acceptance evidence
- **Lifecycle:** Dated record
- **Decision surface:** [JVNAUTOSCI-2722](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2722)
- **Programme:** [JVNAUTOSCI-2721](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2721)
- **Machine-readable observations:** [selected receipts](../generated/jira_authorised_read_2026-09-05.json)

## Claim and baseline

The configured Jira account's authenticated Von owner can obtain a current
issue and the newest project Task through ordinary chat without importing
tasks or rewriting a digest. Other users cannot borrow that account merely
by supplying the owner's identifier, namespace or account selector.

The preceding deployed baseline, `16e4fc79089e8480237e22bc3021874ced5f2cd4`,
excluded ordinary Jira reads as `deployment_global_account`. The earlier
Terra trial returned a non-answer. A Luna caller reached Jira through a
Terra digest child that replaced an existing text relation. Those observations
and exact identities remain on the linked issue. Successful operator reads
distinguished that capability/authority mismatch from expired credentials.

The candidate makes the three existing read tools available to the unique
owner resolved through the configured account's protected Von login-email
binding. The ordinary model chooses its query. The actual Jira proxy checks
the actor and account again, including direct workflow calls. This uses the
existing identity surface; it adds no workflow, semantic routing rule, token
ceremony or broad user grant. The explicit operator migration CLI binds its
operator provenance at its entry point, leaving its shared runner actor-bound.
The current authority rule lives in the [security guide](security_considerations.md).

## Normal authenticated path

Four fresh Chrome conversations used Michael's existing normal login and
SAIL organisation on candidate port 5002. No browser-test login, manually
injected credential or test actor supplied the positive path. The candidate
used the normal Atlas data, authentication and Jira services. Its isolated
runtime marker and disabled background consumers prevented duplicate shared
maintenance; those settings did not replace the turn's binding or retrieval.

The candidate base was `7dd93457695f97deb64369c04aa10cc7a1ddfe40`, with the
three runtime source hashes retained in the selected receipts. Runtime start
was `2026-09-05T11:58:27.550179Z`. Luna remained the saved primary; Terra used
the allowed browser model override. No Sol request was issued.

The exact prompts were:

1. “Look up JVNAUTOSCI-2722 in Jira and give its current summary and status.”
2. “What is the most recently created Task in the JVNAUTOSCI Jira project?
   Give its key, summary and status.”

| Model | Case | Source read | Visible completion | Recorded turn time | Estimated USD |
| --- | --- | --- | --- | --- | --- |
| Luna | Exact issue | `jira_get_issue` | Correct, 58 s | 53.970 s | 0.005123 |
| Luna | Newest Task | `jira_search` | Correct, 45 s | 40.868 s | 0.005296 |
| Terra | Exact issue | `jira_get_issue` | Correct, 53 s | 48.883 s | 0.049255 |
| Terra | Newest Task | `jira_search` | Correct, 42 s | 38.308 s | 0.052629 |

Every answer identified `JVNAUTOSCI-2722`, its current summary and `In Progress`.
The recency queries constrained issue type to Task and sorted Jira `created`
descending, with one result. Canonical persisted Jira responses agreed with
the visible answers. Each record had four provider-observed calls to its
requested model, two capability-discovery calls and one Jira read. Each Jira
response carried the authenticated owner's `governed_login_email_owner`
receipt. Terminal state was `completed`. There was no workflow launch,
import, reconciliation or Jira mutation invocation in these turns; ordinary
conversation persistence still occurred.

The record includes request/session identifiers, history positions, source
response hashes, effective queries, model identities and selected receipts.
Full private traces and expiring telemetry access envelopes are not published.
The UI duration includes work outside the recorded turn interval. Costs are
Von's estimates from reported usage, not invoices; order and caching were not
randomised. These cases show no Terra-only success and support no general
model ranking. Latency remains substantial for these small requests.

## Boundary and regression evidence

The live governed identity resolved an owner binding for Michael and none
for the second actor. With the real configured proxy and identity lookup,
forged owner/namespace/selector input from that actor returned
`jira_resource_not_authorised`; missing trusted actor context returned
`authenticated_actor_context_required`. A direct proxy call in the second
actor's workflow context was also denied. An RPC sentinel recorded zero
external calls in those negative probes. This proves the selected live
binding boundary, not a second positive login route.

Targeted tests cover all three ordinary read tools, server-only projection,
forged request fields, owner and non-owner workflow calls, changed account
configuration, missing/reassigned/ambiguous identity, separate Jira 401
results and explicit operator CLI compatibility. The final run was:

```sh
pdm run pytest tests/backend/test_jira_read_authority.py \
  tests/backend/test_internal_mcp_jira_tools.py \
  tests/backend/test_von_generate_workflow_instances.py \
  tests/backend/test_internal_mcp_catalogue_builds.py -q \
  -k 'not canonical_outcome_report_uses_natural_fact_grounded_projection'
```

Result: **124 passed, 1 deselected**. The deselected narration-report test
failed in the broader run and independently on unchanged main `7dd93457`
(`turn_not_deliverable` versus expected `not_required`). It does not exercise
Jira and was not repaired or relabelled as passing. Diff checks passed.

The evidence supports this owner-read capability. It does not establish
multi-user Jira delegation, the complete four-turn follow-up family, broader
role competence or learned transfer. The continuing programme and release
decision remain on the linked Jira issues.
