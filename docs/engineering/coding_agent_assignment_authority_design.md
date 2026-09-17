# Personal and team coding-agent assignment authority

- **Kind:** Security design proposal
- **Lifecycle:** Draft / proposed; review only
- **Authority:** Advisory; creates no grants, ownership, execution or rollout authority
- **Owner / reviewer:** Michael Witbrock
- **Author:** Codex DGX
- **Evidence date:** 17 September 2026 (refresh of the 12 September draft)
- **Source baseline:** `a6074640f23dd1d714b2872c6f6cb35a94815ec7`
- **Review trigger:** Implementation authorisation or changes to assignment/admission routes
- **Decision surface:** [JVNAUTOSCI-2755](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2755)

## Decision and bounded outcome

Propose an explicit binding for each coding agent and a small set of scoped
assignment grants, evaluated by the existing trusted task services and worker
controller. Personal association, organisation membership and task visibility
are facts distinct from permission to initiate executable work. Michael's
verified personal coding agents would initially have Michael as their sole
human assigner. Other owners' agents and explicitly shared team agents remain
supported, including other repositories.

The fair baseline is the existing DGX operator-bound pilot, including its
trusted-actor dispatch receipts. Retain its repository confinement, receipts
and recovery; replace the remaining legacy metadata-based admission and add
explicit per-agent grants. Do not create a new identity system, universal
workflow, token exchange for ordinary same-server calls, or a general
multi-agent runtime.
Models still interpret requests, distinguish questions from requested work,
select bounded actions and recover mistakes. Server code owns authentication,
grant evaluation and exact scope checks; it does not decide coding strategy.

**Publication boundary:** This proposed design can be merged as documentation;
merge does not accept or activate the policy. Its source audit and scenario
walkthrough support review, not an enforcement claim. Complete live inventory
and delivery of the refreshed private review copy remain controller/operator
handoff work. The earlier private document delivery has a retained receipt;
it must not be mistaken for read-back of this revised body. Missing evidence
below is explicit, not proof that an agent or association is absent.
Implementation requires a subsequent instruction. No enforcement, permission,
worker, database, runtime or deployment change belongs to this design task.

## Evidence and inventory limits

This is a read-only source audit and design review, not a live security
certification. Paths and symbols below refer to the source baseline above.
Existing test bodies were inspected; they were not executed as live acceptance.
Private task text, conversation excerpts, host configuration and receipts are
not reproduced here. The source conversation was unavailable; the supplied
assignment was sufficient to prepare the proposal without inventing dialogue.

| Evidence | Observed source behaviour and limit |
| --- | --- |
| [Worker](../../scripts/codex_von_worker.py), `authorised_task`, `Von.inputs`, `tick`, `apply_deployment` | Pickup checks configured assignee/organisation and, unless supervision is enabled, report recipient. Legacy non-federated tasks with matching creator/organisation and no dispatch record still pass; other tasks need `dispatch_requested` plus a matching current dispatch receipt. Inputs filter comment authors and reply senders. Finish/deployment re-read task conditions; no general per-agent grant service or continuous running-grant recheck is established. |
| [Inbox](../../scripts/codex_von_inbox.py), `allowed_message`, `context_for`, follow-up persistence | Sender, recipient and organisation are filtered; replies can reopen eligible tasks and retain message IDs. These are controller checks on canonical message projections, not permission for arbitrary message authors to execute work. |
| [MCP catalogue](../../src/backend/integrations/internal_mcp/catalogue.py), `_task_create`, `_task_continuation_assignee_allowed`, `_task_update_fields` | Ordinary creation binds actor/organisation and checks coding-agent type plus same-organisation membership. Actor-bound continuation checks control of the task. Neither is an explicit personal/team assignment grant. |
| Same catalogue, `_task_assign`; [task service](../../src/backend/services/task_management_service.py), `create_task`, `assign_task`, `update_task_fields` | Canonical deliberate assignment now records dispatch provenance; native creation does so when the trusted current actor matches the creator. Task-project home guards reject writes at an inactive/non-local home. The field editor still exposes creator/assignee metadata; neither visibility nor home ownership supplies a per-agent grant. Gateway/profile reachability and live ACLs need separate verification. |
| [Dispatch authority](../../src/backend/services/task_dispatch_authority_service.py), `record_assignment`, `is_authorised_assignment`, `revoke_assignment` | Operational, non-exported receipts bind a trusted session/in-process actor to task, assignee, project and organisation; legacy identity headers cannot issue them. Worker comparison requires its configured delegator. Revocation retains a record, preventing fallback to legacy creator admission. Receipts do not bind the instruction body/revision, repository/action grant or team membership, and recording follows assignment persistence rather than an atomic grant decision. |
| [Task REST routes](../../src/backend/server/routes/task_routes.py), `_get_current_user_concept_id`, `create_task_route`, `update_task_route` | Creation derives creator from trusted session context and rejects the legacy-header actor source. Assignee comes from the request. Updates forward the actor and supplied fields to the service. Route visibility is not an agent grant. |
| [Message REST routes](../../src/backend/server/routes/message_routes.py), send route; catalogue `_message_send_direct` | Sender comes from actor context; participant/organisation checks concern communication. Preserve those checks independently from executable-instruction admission. |
| [Access control](../../src/backend/security/access_control.py); [membership governance](../../src/backend/services/organisation_membership_governance_service.py) | Reuse effective actor, visibility and represented membership. Membership management has trusted-actor checks, operational role permissions and receipts. Membership-management or ontology-publication authority is not automatically personal-agent grant-management authority. |
| [Task execution submission](../../src/backend/services/task_execution_submission_service.py), `_resolve_task_execution_conversation` | The UI/scheduled Von execution path requires the Von system assignee and creator-owned conversation. It is distinct from external DGX pickup; do not assume it launches VS Code or bypasses that distinction. |
| [Identity bootstrap](../../src/backend/services/coding_agent_identity_bootstrap_service.py) | Declares `#V#coding_agent` and `#V#github_copilot_instance`. Bootstrap source is not evidence that this instance is live, independently owned, or a Codex clone. No bootstrap was run. |
| [Worker runbook](codex_dgx_worker.md#interactive-coding-agent-messages), [Mac retry handoff](codex_dgx_worker.md#dgx-and-mac-retry-policy-handoff) | Documents separate DGX and VS Code identities and an operator-owned Mac task adapter with retained attempts, a launch boundary and lock. VS Code is no longer accurately described as necessarily reporting-only. This source does not identify the live Mac adapter's principal, active installation or clones. |
| [Retry policy](../../scripts/codex_von_retry.py), `admission`; [supervision](../../scripts/codex_von_supervision.py), `reconcile`, `record_repair`, `repair_comments` | Explicit delegator retries and verified attempt-bound repair observations can admit continuation after current task checks. The supervisor controller can create repair tasks on the source delegator's behalf and attest a repair. Reporting routes, repair provenance and source/attempt checks are distinct from a general target-agent assignment grant. Completing a repair task alone does not resume its source. |
| JVNAUTOSCI-2750 changes `9a7f2a735`, `d56f2a7b8`, retained through the refreshed baseline | Conversational recovery, execution-preference reads and coherent worker/backend releases remain in source. The supplied controller runtime projection identifies one release for worker and task-service modules; see the bounded runtime observation below. Activation work remains separate. |
| [Coordination proposal](multi_agent_coordination_design.md), relevant background to JVNAUTOSCI-1965 | Reuse explicit actor handoffs and context lineage only. Its orchestration and capability-negotiation programme is not a prerequisite. Live issue wording/status could not be re-read. |
| [Creation/recovery tests](../../tests/backend/test_coding_task_creation_recovery.py), [worker tests](../../tests/backend/test_codex_von_worker.py), [inbox tests](../../tests/backend/test_codex_von_inbox.py), [dispatch tests](../../tests/backend/test_task_dispatch_authority_service.py) | Existing isolated fixtures cover pilot paths, trusted dispatch actors, revocation and local-home admission. They do not establish normal production identity issuance, personal/team grants or current runtime installation. |

The 17 September worker context explicitly disables Von MCP and reserves
canonical reads/effects to the controller. Its supplied canonical task lookup
reports the existing private product as `ready`; a retained operator handoff
records successful full-content and recipient read-back of the original draft.
Those observations resolve the old claim that no private delivery existed,
but do not provide the document bytes or read-back of this refresh. The Jira
connector again returned reauthentication required, so live issue comments,
links and project-writer state could not be verified. No credentials, private
database, operator configuration or unrelated conversations were read.

The supplied controller projection, observed at 13:50:02 UTC on 17 September,
binds this DGX run to Michael, the SAIL organisation and its fixed source
repository, and identifies release `ed5c5cc60651a70ce453acc84f9964ff7aaee30c`
for both worker and backend task-service modules. The referenced source files
exist and were hashed; the projected PID was not visible through this sandbox's
`/proc`. This is controller-provided runtime evidence plus file inspection, not
an independent live process/import probe or a public served-revision check.
Private paths, hashes and delivery receipts remain in the controller handoff.
DGX and Mac destinations must be inspected separately; a missing record in one
must not cause identity creation in the other.

### Initial agent / assigner / repository matrix

These rows separate known identifiers from verified registrations. They are
proposed initial policy, not a live grant inventory.

| Agent or candidate | Evidence / execution surface | Association and initial assigners | Repository and action envelope |
| --- | --- | --- | --- |
| `#V#codex_dgx` | This execution and supplied controller runtime projection identify an active scheduled DGX consumer and coherent worker/backend release. Canonical registration/alias and sponsor records were not independently enumerated. | Supplied operator-bound delegator is `#V#michael_witbrock`; proposed personal binding and sole initial human grant remain subject to verified issuance. SAIL membership alone supplies no additional assigner. | Supplied fixed Von repository and versioned runtime roots; verify canonical repository identity before grant issuance. Publication/deployment remain separate task/effect authority. |
| `#V#codex_vscode` | Runbook identifies interactive/reporting identity and separately documents a Mac task adapter. Whether that installed consumer uses this exact principal, and its current liveness, require operator read-back. | Proposed Michael personal association, subject to registration/operator verification. Michael alone for verified eligible executable surfaces. | Each verified workspace/repository binding. Label verified interactive and scheduled modes separately; neither assert reporting-only status nor queue to an unverified consumer. |
| Actual registered VS Code clones/instances, if any | No clone IDs or active instance inventory available. Names such as “VS clone” do not establish aliases or new principals. | Associate verified Michael instances only; independent owners must remain separate. A shared principal needs distinct trusted installation IDs, not invented user IDs. | Inventory each installation and repository. No inheritance from a display-name match or copied checkout. |
| `#V#github_copilot_instance` | Source bootstrap candidate only; runtime status, owner and assignment-consumption mode unknown. | Unresolved; no takeover and no proposed Michael grant without evidence. | Unknown. Preserve existing state; verify separately if live. |
| Another person's personal agent (scenario, no invented live ID) | Future or independently discovered registration | Its verified owner can explicitly grant themselves/others its scoped assignment authority. This conveys no access to Michael's agents. | Its separately authorised repository, including a non-Von repository. |
| Team agent (scenario, no invented live ID) | Explicit team association and registered consumer | Two named humans or a narrowly identified team/role grant; an ungranted colleague has no authority. | Explicit repository/project and action scopes selected by the team's authorised binding manager. |

For a complete inventory, an authorised operator should read agent-type
instances, aliases and instance relations from the actual DGX and Mac Von
destinations, then reconcile them with installation registrations and recent
controller execution/report receipts. Record agent principal, human principal,
sponsor, organisation, runtime identifier, consumer mode, repository ID,
observed revision/date and liveness evidence. Enumerate all pages and record
query scope; inaccessible registrations remain unknown. An identity with only
messages is not thereby an active executor. Retain this operational inventory
privately with the task; publish only approved, non-sensitive conclusions.

## Smallest explicit authority model

Use existing concept identities and trusted actor services. Represent durable
association and grants through one canonical governed service; keep execution
receipts in the existing task/controller evidence stores. Extend the existing
dispatch receipt rather than creating a competing task-admission record: add
the accepted instruction revision and grant/scope decision it currently lacks.
Its operational storage may remain outside Vontology while durable bindings
and grants use the selected protected represented surface. The following are
logical fields, not a proposed new universal schema or permission to create
concepts now.

| Record | Minimum content and meaning |
| --- | --- |
| Agent binding | Agent principal; sponsoring person or team; explicit grant manager(s); registered execution installations and consumer mode; eligible canonical repository IDs/action ceiling; version and active state. Organisation is context, not the owner by implication. |
| Assignment grant | Grant ID; agent/binding ID; subject human or explicit team/role ID; permitted repositories and optional projects; action scope; issuer and trusted issuance provenance; version and active/revoked state. Optional expiry only for temporary access. |
| Accepted instruction / run provenance | Trusted initiating principal, executing proxy if any, source event/request ID and revision, agent/installation, exact task/instruction revision, selected repository/actions, grant decision/version and acceptance time. Link to existing receipts; do not copy private source text into broad discovery. |

Effective authority is the intersection of the assigner's current grant, the
agent/installation ceiling, the selected task instruction and independently
available resource/effect authority. A project reference may narrow repository
scope; it cannot silently retarget a grant when project metadata changes.
Canonicalise repository identities through trusted configuration, not a model
URL or remote name. Normalise local paths under the registered workspace and
reject symlink/path escapes. Supporting another repository requires its own
explicit binding and resource access, not a universal Von-only ban.

Direct human grants suffice for personal agents. Team agents can use direct
grants for a small stable team; use a team/role grant when an already governed
membership set is the intended authority. Merely joining the agent's
organisation is insufficient. The grant manager must understand that anyone
authorised to change a granted team's membership can affect its assigner set.
Do not grant a broad organisation role when that administrative consequence is
not intended. Avoid a new team service if direct grants meet the actual need.

### Issuance and management, without self-granting

For existing personal installations, the trusted installation operator verifies
the authenticated person's existing identity and control of that installation,
then records the person as sponsor and grant manager using the bounded
registration route. This is an explicit bootstrap decision, not a deduction
from an editable `owned_by` relation, display name, e-mail text, or an agent's
self-report. For new personal agents, the normal authenticated registration
route can bind the registering person to a newly provisioned installation;
claiming an existing installation still requires operator verification.

For an organisation-owned team agent, the existing represented operational
owner/role-management authority for that organisation can approve its initial
team binding and named managers. Record the actual authority checked. This
does not give organisation admins control of personal agents merely sharing
that organisation, nor does semantic ontology authority confer coding grants.
The exact existing registration endpoint is an implementation decision: this
audit found identity bootstrap code, not a complete owner-verification service.

Thereafter only binding grant managers may grant, narrow or revoke assignment
authority within the installation ceiling. Assigners cannot edit grants,
appoint managers or transfer ownership merely because they can assign work.
Manager changes require an existing manager's authenticated decision or the
same bounded, audited operator recovery used for initial binding. Preserve
recoverable prior versions; prevent ordinary edits from removing the last
manager without an explicit recovery/transfer decision. A host operator remains
part of the trusted computing base; this design does not defend against root.

Reserve authority-bearing binding/grant fields at the canonical write boundary,
including generic concept/relationship/text mutation routes. Editing a visible
concept must not create a grant. Use the existing membership-governance pattern
for actor binding, guarded writes and receipts, without reusing unrelated
membership or publication permissions as assignment permission.

### Normal trusted path

1. Operator verifies the existing Michael identity and control of the DGX
   installation; the registration service binds them and records the receipt.
   Michael, authenticated normally, issues his own explicit scoped assignment
   grant as the verified binding manager. Both effects are read back.
2. Michael creates a coding task via the authenticated UI or an actor-bound
   Von turn. The server supplies the initiating actor; the model supplies only
   task intent and target. The task service checks task access and the explicit
   grant before saving an executable assignment and its accepted revision.
3. The existing scheduler/controller authenticates as the registered agent
   installation. It rechecks the accepted instruction and current grant, then
   launches within the fixed repository/action ceiling. It never authenticates
   Michael by reading `created_by_concept_id` from mutable task fields.
4. Source context is fetched only through its independent audience checks.
   Missing context allows useful work from adequate task text; a materially
   necessary missing fact prompts a focused question.
5. Results retain provenance and go to recipients independently authorised to
   see them. Publication or deployment needs its own existing authority and
   meaningful pre-effect recheck. A successful assignment receipt is not a
   deployment receipt.

For an interactive VS Code agent, apply the same assignment decision at the
verified interactive request/admission adapter. If it only reports to Von,
display that fact and use its authenticated operator interaction; do not imply
that a Von assignment has launched it.

## All executable entry and continuation routes

Put one assignment decision at the shared task/admission service boundary,
called by each adapter. Protect the accepted instruction revision against
ordinary content edits; a worker must not consume arbitrary newer task text as
authorised instructions. Atomic validation/persistence or a version-checked
receipt plus non-executable pending state must prevent partial admission.

| Route | Proposed decision and observable result |
| --- | --- |
| Task create, assign, update, reassign, subtask creation, bulk/import paths | Before changing an executable assignment, require trusted actor plus target grant and scope. Reassignment also requires authority over the existing task/assignment; the target grant alone cannot steal a task. Denial leaves the prior assignment intact; failed creation leaves no executable assignment. An optional unassigned draft requires an explicit accurate result. |
| General REST/MCP, chat tool calls and low-level relation/field edits | All reach the same boundary. Do not rely on an optional payload flag such as actor-bound continuation. Generic writes cannot manufacture an accepted instruction/grant receipt. Creator edits remain historical metadata, never authentication. |
| Task title, description, repository, project, model/action or work-product changes | Existing edit permissions govern communication. Only an authorised revision can change executable intent. Material expansions require a current grant/scope decision; other authors' edits remain suggestions/data. Cost/model preferences stay within separately configured execution policy. |
| Direct messages, inbox replies and comments | Preserve authenticated authorship and ordinary communication. The model may interpret a request, but server/controller admission must check that event's actual initiator before resuming or expanding work. A comment/message, quote, invitation or task ID alone cannot reopen execution. |
| Queued retries, requeue, schedule occurrence, crash recovery | Recheck current grant, binding, membership, instruction revision and resource ceiling at the next launch. A prior success or idempotency key cannot revive revoked authority. Reconciliation may read existing receipts without rerunning effects. |
| Supervisor repair creation and verified-repair continuation | Preserve the existing source/attempt-bound repair checks. Before assigning the repair, check the source delegator's grant for the target agent and the controller's bounded proxy authority. A reporting/supervisor relation alone supplies neither. Before source continuation, recheck its current grant and accepted instruction; a verified repair proves recovery, not renewed assignment authority. Human supervisors receive a task/message, never a coding process. |
| Running follow-up or redirect | Admit a separate instruction revision with its own trusted event/actor, then checkpoint before consuming it. Recheck all contributing queued instructions; concatenation must not obscure which author/grant supports each. |
| Acting on behalf of a human | Same-turn trusted Von tool calls retain the human as initiating actor and Von as proxy; a payload naming Michael is insufficient. A separately acting/sessionless proxy needs an existing exact delegation bound to principal, agent, task/revision, scope and validity, revalidated on use. Do not let an assignment grant imply transitive delegation. |
| Publish, merge, deploy, credential/resource access | Separate effect authority remains required. The assignment service cannot enlarge it. Recheck current instruction/grant before new material external effects; rollout is outside this design. |

A denied request returns a typed reason and the unchanged/non-executable state,
with actor-appropriate next steps. Do not falsely display the agent as queued.
For revocation, retain historical assignment with an explicit paused/revoked
execution state and no launch eligibility, rather than deleting useful work or
claiming the task was completed. Ordinary status questions can still be answered
within audience permissions without granting assignment authority.

The specific prevention need is unauthorised use of another person's repository,
execution resources or publication capabilities. Source shows that same-org
assignment eligibility and mutable creator-based pickup are distinct checks;
the general editor can alter metadata consumed by the latter. This establishes
a trust-boundary dependency that a multi-user grant design must remove, though
live exploitability was not tested. A prompt-only instruction or UI restriction
cannot reliably confine those entry points; post-hoc rollback cannot undo
private source disclosure or already-issued external effects. Restrict exact
admission/effect authority, not harmless messages or semantic problem-solving.

## Revocation, races and bounded recovery

Check authority at acceptance, dequeue/retry, instruction revision changes,
and before publication/deployment or other material external effects. Team
membership and grant changes invalidate cached admission decisions. Couple
grant/version validation with admission under the existing single-controller
serialisation where possible; otherwise use a conditional revision check and
reconcile a lost race. A cached historical receipt alone is not a current grant.

Running work needs a controller checkpoint/cancellation signal that does not
depend on the coding model volunteering to stop. On observed revocation, stop
accepting instructions and starting further tools/effects, preserve partial
files and receipts, and pause at the next recoverable boundary. An operation
already submitted may finish; record the result and reconcile it before any
retry. Pre-authorise only minimal checkpoint/report cleanup to the original
permitted audience. Do not silently finish the full coding assignment, publish
partial work, revert committed changes, or discard useful output.

A long model or shell call creates a revocation-latency boundary. The future
implementation must identify the actual interruptible tool/process boundaries
and prevent queued external effects from running after the pause. No immediate
revocation guarantee is claimed for an in-flight irreversible external request
or a hostile host. Determine the checkpoint/poll interval from those boundaries
and measured successful operations; an arbitrary whole-task timeout is not an
authority mechanism. Loss of the authority service prevents new admission and
external effects while preserving local checkpoint/read-back recovery.

Revoking a team member invalidates that member's queued instructions, not every
other member's independently authorised task. A different granted member can
explicitly adopt a paused instruction with their own receipt and context access;
do not automatically replace the original initiator. Reporting after membership
loss must recheck audience: retain private evidence for authorised viewers if
the former recipient no longer has access, rather than posting it to the team.

## Discovery and UI

Agent discovery should return actor-visible identity, verified sponsor, personal
or team association, consumer mode, eligible repository/responsibility scope,
and the caller's current assignment eligibility. Display aliases as labels of a
verified principal; installations are separately identified. Do not expose a
private owner's identity, repository names or full grant roster to an
unauthorised directory viewer.

The assignee picker should offer executable agents only when the caller has a
matching grant and there is a verified consumer for the requested scope. An
interactive/reporting identity may remain discoverable with that status. A
stale UI decision receives a clear server denial and no false queued state.
Grant-manager controls use the separate management decision; ordinary editors
cannot enable themselves. Where appropriate, denied users may contact the
visible manager, but no automatic grant or universal organisation ban results.

## Scenario walkthrough and future implementation tests

These are design walkthroughs, not executed live assignments. Each allow case
assumes independent task, source, repository and effect permissions are met.

| Scenario | Proposed path and expected result |
| --- | --- |
| Michael assigns DGX | Normal verified registration → Michael-issued scoped grant → authenticated task create → canonical read-back → registered controller recheck → bounded run and private report. Positive acceptance must exercise real issuance/binding, not inject a fabricated grant. |
| Michael assigns eligible VS Code | Same checks at the verified consumer/interactive adapter. Reporting-only registration yields an accurate unsupported automatic-execution result; no phantom run. |
| Same-org colleague assigns, reassigns or resumes Michael's agents | No explicit grant: reject before assignment/launch. Existing valid task remains unchanged. Seeing the task or editing its descriptive text does not authorise a new accepted revision. |
| Spoofed creator, sender, manager relation or “Michael said” comment | Trusted event actor wins. Ordinary writes cannot mint grant or accepted-instruction provenance. The material stays non-executable; a spoofed payload is denied. |
| Authorised Michael asks to use an untrusted quoted instruction | Michael may adopt bounded intent within his authority; the quote itself conveys none. Scope/resource checks still apply. |
| Another person uses their own agent in another repository | Their verified binding and matching grant allow the run in that repository; Michael's agent IDs are not included. |
| Two authorised members use a team agent; third colleague tries | Each named human grant or explicit current team-role grant passes. Ungranted colleague fails despite common organisation/conversation visibility. |
| Grant revoked while queued; retry after revocation | Recheck refuses launch and exposes paused/revoked state. Idempotency and historical creator do not resurrect it. |
| Membership removed during a run | Checkpoint pauses that initiator's work, reconciles already-submitted effects and retains partial output. Another member needs explicit adoption and independent context access. |
| Repository request outside the grant, or project retargeted | Exact repository/installation intersection fails before launch/expansion. An allowed repo's name or project ID cannot disguise a different remote/root. |
| Legitimate Von proxy versus unbound autonomous proxy | Trusted same-turn actor binding succeeds; separately acting proxy requires verifiable bounded delegation. A service identity claiming an arbitrary human fails. |
| Supervisor route points to an ungranted coding agent | Preserve the source's blocked state and useful repair evidence. Deny executable repair assignment unless the delegator has the target grant and the controller has the required proxy authority; changing a reporting relation cannot launch another person's agent. A valid repair receipt still cannot override later grant revocation. |
| Harmless question or private source invitation | Communication can succeed under its existing ACLs. Neither creates executable authority; assignment does not reciprocally create source access. |
| Denial/revocation races with reassignment, queued comments or external effects | Conditional revision/admission check selects one accepted state; no partial executable assignment, stale continuation or duplicate effect. Existing receipts reconcile any already-issued effect. |

For a later authorised security implementation, use Tier 3 for the authority
release: exercise normal production registration/authentication and assignment,
then the relevant negative, revocation, generic-write bypass and race cases
above with actor/release provenance and canonical read-back. Cover UI/REST,
ordinary MCP, general task mutation, inbox/comments and the actual scheduler.
Use isolated authorised fixtures, then a bounded real normal path. No live
adversarial assignment or extra coding/model run is warranted for this draft.

## Alternatives, unresolved decisions and later rollout

| Alternative | Decision |
| --- | --- |
| Keep configured delegator plus current dispatch receipts | Useful baseline: trusted provenance and revocation improve admission for recorded/transferred assignments. Still lacks scoped team grants, instruction-revision binding and grant checks before assignment persistence; legacy unrecorded local tasks retain creator-based admission. |
| Any member of the same organisation may assign | Reject for personal agents; shared visibility does not grant use of a person's coding resources. An explicit team grant can intentionally choose a governed membership set. |
| Hard-code Michael and Von everywhere | Reject: satisfies one initial configuration by breaking independent owners and repositories. |
| Per-agent bindings with direct grants and optional explicit team/role subjects | Selected: durable identity/scope is inspectable; a small shared service handles exact decisions while models retain adaptive judgement. |
| Universal delegation tokens or new workflow/lease machinery for this proposal | Defer. Only a demonstrated separately acting/sessionless boundary needs exact delegation. Reuse existing task-project home and durable-workflow ownership controls where applicable; those solve placement/exclusivity, not permission to use an agent. Do not expand the separate federation programme. |

Before implementation authorisation, resolve the live agent/clone inventory,
actual VS Code consumer mode, canonical repository bindings and sponsor evidence.
Confirm the available registration/manager bootstrap route and whether any
team actually needs role-based assignment instead of two direct grants. Select
the canonical protected grant representation after checking live vocabulary;
do not invent parallel predicates. Determine reachable general mutation paths
and the real controller checkpoint/effect boundary. These unknowns limit an
enforcement claim, but need not cause a broader identity redesign.

After a separate instruction to implement:

1. Capture private inventory and current grants/bindings through canonical
   services, with source dates and recovery receipts. Confirm the live project
   writer before selecting the tracking surface. Do not take over independent
   agents or change other projects' authority.
2. Add the shared protected grant/binding decision and accepted-instruction
   provenance to existing task services and dispatch receipts; wire every
   reachable mutation and worker admission route, including supervisor repairs.
   Keep reporting/context access separate.
3. Add actor-visible discovery/manager controls and real consumer adapters.
   Reuse the verified Mac adapter where present; describe an installation as
   reporting-only only when the inventory actually establishes that mode.
4. Run the bounded authority acceptance above. Freeze the candidate once normal
   authorised use and relevant denial/revocation/recovery paths are supported;
   unrelated defects do not expand the release gate.
5. Under separately authorised activation, seed only verified initial bindings
   and grants. Reconcile existing queued tasks without treating historical
   creator fields as proof; obtain a trusted adoption receipt where provenance
   is missing. Preserve in-flight work at a controller boundary.
6. Activate coherently, read back active module/binding/grant versions, and
   retain rollback. Code rollback must not restore revoked grants or revert to
   broad assignment authority: pause affected admission if the older worker
   cannot honour the protected records. Any data migration needs its own scope
   and recovery plan.

## Private review delivery contract

The merged repository document remains a proposed review copy. Completion
additionally requires a private canonical Von document/plan concept containing
the **complete readable design**,
clearly Draft/proposed, with source commit provenance and these unresolved
questions. It must not be an executable workflow or a prompt activation.

The authorised controller/operator must inspect the exact native task's
existing products/history, update the existing document via canonical content
services, and set `current_work_product_concept_id` without destroying prior
products. The retained original delivery is continuity, not grounds to create
a duplicate document or to claim the revised text has been read back. Preserve
the intended private audience; organisation sharing alone is not a substitute
for the requested visibility. Use the existing
[task work-product service](../../src/backend/services/task_work_product_service.py):
Michael's actor-effective `hasContent` projection must resolve to exactly one
non-empty readable body and the task product must resolve as `ready`. Preserve
content provenance/hash and read-back receipts; do not select an arbitrary
latest row if the projection is ambiguous.

The controller, using this coding agent's registered identity, must send the
requested direct message with the exact document concept pointer, native task
and Jira references, proposal summary and explicit “implementation awaits
subsequent instruction” boundary. Verify persistence by canonical recipient
read-back. Link the durable review artefact on Jira when its authenticated
connection is available. Only that verified document-and-message delivery
satisfies the requested handoff; a PR, host file or completion capsule alone
does not. The assigned worker session must not bypass its prohibition on shell
Von mutations or self-sent messages to manufacture these receipts.
