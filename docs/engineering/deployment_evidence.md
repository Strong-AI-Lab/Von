# Build and deployment evidence

- **Kind:** Capability and producer guide
- **Lifecycle:** Active
- **Authority:** API contract and repository release input; repository presence is not live activation
- **Owner:** Von maintainers
- **Last reviewed:** 12 September 2026
- **Review trigger:** Receipt schema, task work products or deployment-controller changes

A source revision identifies code. `#V#software_build` identifies an immutable
artefact (use its content digest or immutable CI artefact ID). A
`#V#software_deployment` identifies one attempt to serve that build in `local`,
`staging` or `production`. `#V#deployment_observation` preserves an attributed
status receipt. These identities are distinct even when the deployment serves a
source checkout directly: give the installation its own immutable build ID and
record the exact source revision separately.

The release input is `deployment_evidence_service.VOCABULARY`. An authenticated
producer explicitly calls `POST /api/tasks/deployment-vocabulary` to create any
missing vocabulary in its normal creation scope. Existing concepts are reused;
this operation neither overwrites definitions nor publishes them globally. Use
canonical ontology publication authority separately when vocabulary or receipts
need broader visibility. A private vocabulary owned by a different actor must
be made available through that route before that actor can ingest receipts.
There is no startup migration, database write script or automatic publication.

## Producer procedure

Use the existing authenticated browser/API session and its active window scope.
The server derives actor and organisation; the payload cannot choose them.
Codex/DGX controllers or CI must use an already authorised session/API adapter;
this feature does not provision credentials, bypass login or grant deployment
permission. The coding worker must return evidence to its controller when its
execution instructions prohibit live writes.

After vocabulary activation, POST this object to `/api/tasks/deployments`:

```json
{
  "build_id": "sha256:<actual artefact digest>",
  "source_revision": "<full source Git SHA>",
  "deployment_id": "dgx-production-<unique attempt ID>",
  "environment": "production",
  "target": "<served service/PWA URL or target identity>",
  "receipt_id": "<unique CI/controller receipt ID>",
  "status": "deployed",
  "deployed_at": "2026-09-12T21:59:00Z",
  "observed_at": "2026-09-12T22:00:00Z",
  "evidence": "<receipt locator, observed revision, checks and outcome>",
  "implements_tasks": ["#V#task_..."],
  "verifies_tasks": []
}
```

Retain the returned concept IDs and canonical read-back. A repeated identical
receipt reuses the same build, deployment, observation and links. Conflicting
content under an existing immutable ID is rejected. After an interrupted or
failed relationship write, retry the same receipt: completed steps are reused.
Identity reuse is scoped to the trusted actor and organisation; two independent
producers do not silently take ownership of each other's records. Controller/CI
retries therefore need a stable producer identity and organisation.

Each status change has a **new receipt ID**, observation time and evidence. It
reuses the deployment ID, build ID, source revision, environment, target and
superseded-attempt reference. Supported statuses are `planned`, `built`,
`deployed`, `verified`, `failed`, `rolled_back` and `superseded`. Planned/built
receipts need not include a deployment time or target. When the target becomes
known it is part of a new deployment identity; do not rewrite a planned
attempt's immutable core. Deployment, verification, rollback and supersession
receipts require a deployment time. Include explicit evidence even for planned
or failed states, so consumers can distinguish a producer report from absence.

`verified` is the producer's evidence-backed observation, not a server-side
health probe or certification. Record the actual served revision and relevant
service/PWA checks, including limitations. A successful code push or process
start alone is insufficient. Ingestion never calls a model, deploys code,
completes a task, selects a product or asserts all linked tasks passed. Per-receipt
task links preserve which work that observation concerned.

History is append-only through this API. Current status is the latest observed
time, so late ingestion of an older receipt does not regress it. Contradictory
statuses/deployment times at the same latest time produce `ambiguous`; submit a
new explicit observation to resolve them. The normal concept store owns access,
creation timestamps and mutation provenance. This ingestion contract is not an
additional immutable-storage boundary against separately authorised generic
concept edits.

## Reads and task work products

- `GET /api/tasks/<task-id>/deployments`: all accessible deployments implementing
  or verifying the task, including multiple builds and attempts.
- `GET /api/tasks/deployments/<deployment-concept-id>`: build identity, source,
  environment, target, status, history and accessible task links.
- `GET /api/tasks/builds/<build-concept-id>/deployments`: attempts for that build,
  with their task links (build-to-task traversal).

`#V#deploys_build`, `#V#implements_task`, `#V#verifies_task`,
`#V#observes_deployment` and `#V#supersedes_deployment` are canonical graph links.
Reverse reads query those same forward edges; no independently updated reverse
list is maintained. Exact structured receipt fields live in the concept's
`attributes.deployment_evidence`; they are not free-text task notes or copied
current-product content. These are actor-visible operational assertions, not
global domain truths.

Task details display builds, attempts, status and receipt history. A verified
attempt offers **Use as current work product**, using the existing
`#V#hascurrentworkproduct` selection route. Opening that product reads its current
structured deployment evidence. A later failure or rollback remains visible;
`ready` on the product response means readable, not successfully deployed.
Other concept products retain their existing text-content behaviour. Links
neither widen visibility nor transfer deployment authority.

## Rollback, redeployment and acceptance

A rollback or redeployment is a **new deployment ID**, even if it serves an
already known build. Supply `supersedes` with the prior deployment concept ID;
it must refer to a different attempt at the same environment and target. Record
the old attempt's rolled-back/superseded observation separately. This preserves
partial outcomes and does not invent a cross-record atomic traffic switch.
A replacement does not automatically prove the old deployment stopped serving.
Unverified deployments remain inspectable and are not offered as verified
product selections.

Candidate tests cover ingestion, reuse, conflicts, traversal, lifecycle,
partial retry, access-filtered reads and the product API. The browser fixture
`tests/browser/taskDeploymentEvidence.cjs` exercises the actual task panel and
CSS at desktop/mobile widths with isolated receipts. It does not prove live
ontology activation or a public deployment.

For representative live acceptance, bind an isolated authenticated candidate
profile to the exact candidate checkout/revision, with a disposable database
and no model generation. Ingest an operator-provided real deployment receipt,
read it through all three traversal APIs, select the verified attempt on an
accessible fixture task, and reopen it after a new unverified/rollback receipt.
Record the candidate SHA, receipt IDs, observed served revision and browser
read-back. Keep deployment credentials outside the checkout. Public Cloudflare
access redirects and browser profiles bound to other checkouts are not evidence
for this candidate. No public deployment is requested by this task.
