# The Reliability Ratchet: source articles and tracked cases

- **Kind:** Advisory source copy and evidence log
- **Lifecycle:** Active
- **Authority:** The reproduced articles are advisory; tracked cases are
  evidence records. Neither overrides current user direction, `AGENTS.md`, live
  authority, or current implementation evidence.
- **Authority scope:** Reliability-ratchet analysis in Von engineering work
- **Owner:** Von maintainers
- **Last reviewed:** 20 August 2026
- **Review trigger:** A new tracked case, a material source correction, or new
  evidence that changes a recorded diagnosis or status
- **State or evidence as of:** 20 August 2026
- **Open questions:** Which recorded diagnoses remain supported, need narrowing,
  or should be reclassified after outcome-level validation?

This file preserves the two public ratchet articles as source material and
records concrete Von cases in which the pathology is observed. It deliberately
does not convert the articles into a mandatory checklist, schema, approval
stage, or second repository constitution. Their useful role is as a revisable
diagnostic lens. The governing engineering invariants remain in
[`AGENTS.md`](../../AGENTS.md).

## Source provenance

The article bodies below are faithful Markdown transcriptions of the public
rendered WordPress bodies downloaded on 18 August 2026. Heading levels were
adjusted only to fit this containing document. Wording, spelling, punctuation,
emphasis, lists, and links—including source errors—were otherwise preserved.

| Article | Published | Public source | WordPress body | Rendered-body SHA-256 |
|---|---:|---|---|---|
| *The Reliability Ratchet* | 1 August 2026 | [Canonical article](https://michaelwitbrock.com/2026/08/01/the-reliability-ratchet/) | [Post 3](https://public-api.wordpress.com/wp/v2/sites/michaelwitbrock.com/posts/3) | `f05f7f9722885a4a12f559812d70e598997d33ebff36d73b17156f4be29b6475` |
| *One turn of the ratchet* | 18 August 2026 | [Canonical article](https://michaelwitbrock.com/2026/08/18/one-turn-of-the-ratchet/) | [Post 50](https://public-api.wordpress.com/wp/v2/sites/michaelwitbrock.com/posts/50) | `8e9e1b62161a6d62923bc3548e683f12196025c2c94a0f558bc473f45a4ae84e` |

WordPress identifies Michael Witbrock as the publishing author of both posts.
The first article contains its own explicit contributor line; the second
acknowledges its collaboration in the opening paragraph.

## Article 1 — The Reliability Ratchet

*How AI-coded systems turn fixes into fossils*

*Michael Witbrock and Codex (GPT-5.6 Sol Ultra) written in the context of a frustrating programming session.*

The most dangerous code written by AI is not obviously bad code.

Bad code is visible. It fails tests, throws exceptions, performs poorly or can’t be maintained. The more dangerous output is a clean, documented, reviewed and thoroughly tested patch that prevents one observed failure by quietly eliminating valid behaviours, alternative implementations and future routes to improvement.

A failure is reported. A coding agent analyses it, constructs a plausible causal story and repairs the nearest relevant code. It adds a validator, contract, state, retry, adapter or fallback. It writes tests showing that the failure is gone and updates the documentation to explain the new mechanism.

The ticket closes.

Has the system improved? Or has one uncertain diagnosis just become permanent architecture?

We call this the reliability ratchet: a process in which every visible failure adds machinery, while almost nothing creates comparable pressure to remove it. The system becomes more locally defensible and more globally constrained.

That is non-progress—not inactivity, but energetic work that makes future work harder and may remove all reasonable paths to achieving the intended system..

### An old pathology at a new speed

Human software teams have always accumulated workarounds, stale abstractions and tests that preserve yesterday’s implementation. Coding agents did not invent technical debt. They change its economics.

A human team might take days to turn an incident into a diagnosis, an implementation, a test suite and a design explanation. A coding agent can produce the entire package in minutes. Because all the pieces are coherent, the result looks unusually rigorous.

But the test is often not independent evidence for the diagnosis. It is the diagnosis repeated in executable form. The documentation repeats it in prose. The contract embeds it in an interface. The implementation makes it operational.

Four artefacts agree because one conjecture generated all four.

This creates a distinctive form of epistemic debt. The code may be locally excellent, the tests comprehensive and the documentation clear. What has not been established is that the explanation of the failure was true—or that the repair preserves the wider range of behaviour the system ought to support.

AI coding lets a weak causal theory acquire institutional authority with unprecedented speed.

### How the ratchet works

The cycle is usually:

**Observation → conjecture → mechanism → regression test → inherited law.**

A coding agent receives a bounded task: make this failure stop happening. It inherits the existing architecture, tests and repository instructions and thinks they are largely intentional – that they have resulted from a coherent plan. Questioning that architecture appears out of scope; modifying the nearest code appears responsible.

So one race condition becomes a mandatory global ordering. One timeout produces a universal retry layer. One malformed payload makes an optional field compulsory. One integration failure creates a permanent fallback. One ambiguous request becomes an intent classifier.

Any of those changes might be right; the mistake is moving from a single observation to a deterministic structure without either first establishing the failure class or weighing the effects against the overall purpose of the software..

The next coding agent encounters the new mechanism and its tests as authority. Removing them looks dangerous. Working around them looks safe. When a neighbouring case fails, another mechanism is added outside the first.

Nothing in this process is locally irrational. That is precisely the problem.

### A guardrail that recorded its own defeat

This ratchet is exceptionally difficult to avoid. In the large system maintained substantially with coding agents (including both of the authors) that provided the initial context for this analysis, a test was introduced to restrain the growth of a central coordination module. Its accepted baseline was 46,106 lines. A genuine deletion brought the module down to 45,355.

Then development resumed. As new features and repairs enlarged the file, the baseline was repeatedly refreshed until 49,717 lines counted as compliant. The test even printed the command required to approve the larger number.

Line count itself was not the diagnosis; large files can sometimes be justified. The revealing fact was that a supposed anti-growth mechanism repeatedly certified growth. The guardrail had not changed the development pressure. It had converted accumulation into a notarised exception.

The repository possessed the right principle and an automated check. The executable environment still rewarded closing the current task.

### A conjecture is not an invariant

A trace rarely determines its own explanation. The observed failure may have been caused by the nearest branch, or by missing context, a false abstraction, an obsolete stage, an upstream race, an incorrect requirement—or the fact that the entire mechanism no longer earns its place.

The coding agent’s diagnosis is a hypothesis. It should remain revisable until neighbouring evidence supports it.

Software is uncomfortable with hypotheses. It prefers types, states and assertions. That preference is valuable when the underlying property is genuinely invariant:

- One user must not access another user’s private data.

- A payment must not be charged twice.

- A destructive action requires proper authority.

- A committed record must remain internally consistent.

Those requirements can be stated independently of the incident that revealed them. Other decisions are contingent: which component should handle a request, which recovery should be tried first, whether one intermediate state must exist, and whether today’s workaround will remain useful after the surrounding system changes. And perhaps most importantly, whether today’s workaround will interfere with a current or future vital system function that just happened not to have been exercised during the test that drove the fix.

Hardening a contingent diagnosis into an invariant does not remove uncertainty. It launders uncertainty into structure.

In AI systems, user intention makes this especially obvious. Intention is normally a working hypothesis assembled from language, context, available actions and the consequences of being wrong. It should be revised as evidence arrives. Turning it immediately into an immutable classification or expected-outcome contract may make the system more explicit while making it less intelligent.

But the same mistake occurs throughout ordinary software. A causal theory is not made true by giving it a schema.

### Tests as an accidental constitution

Tests are indispensable. But tests do not acquire authority merely by existing.

A test may protect an externally meaningful requirement: data integrity, authorisation, idempotency, API compatibility or an observable user outcome. Or it may instead protect an accident: the exact helper called, stage traversed, internal label assigned, cache consulted, message emitted or sequence followed by yesterday’s successful repair.

Those are not equivalent.

When implementation-specific tests are treated as enduring requirements, the test suite becomes an accidental constitution. Future agents are told, in executable form, that correctness means reproducing an approved internal history, not satisfying the system’s purpose.

This is particularly powerful for coding agents. Tests are concrete, local and machine-verifiable. Lost opportunities are none of those things. Persistent instructions to coding agents are not successful in causing them to view tests as revisable, or deletable,

The valid input wrongly rejected by a test leaves no stack trace. The simpler architecture never attempted produces no failing test. The recovery strategy eliminated by a validator generates no incident. The cost of making future changes harder appears days or, in projects slowed by the capability ratchet, months later, distributed across unrelated tickets.

Every visible failure can produce another permanent artefact. Missing alternatives leave almost no evidence that they ever existed. The ratchet is asymmetric.

### Why capable agents can conceal a bad architecture

A sufficiently capable coding agent can navigate an astonishingly complicated codebase. It can discover obscure contracts, satisfy brittle tests, thread another field through twelve layers and produce a plausible explanation for all of it.

That capability can conceal architectural decline.

The fact that an agent can successfully modify the maze does not show that the maze is justified. It may show only that the agent is good enough to compensate for it.

As coding models improve, this problem may worsen before it improves. Stronger agents can keep increasingly elaborate systems operational, delaying the moment when humans are forced to confront the structure itself. Meanwhile, the product may become slower, less adaptable and more expensive even as the rate of closed tickets rises.

As we’ll explore in a future post, these abilities also have recently been enabling capable agents to find composite cyber-vulnerabilities in complex systems; this does not mean that they will be as capable at planning and implementing function-preserving mitigations.

### Why more rules will not save us

Most repositories and software engineering guides already contain excellent principles: prefer simple designs, avoid special cases, preserve compatibility, do not overfit, add abstractions only when justified, and remove obsolete machinery.

Then the development environment rewards the opposite.

Issue trackers fragment systemic failures into local units of closure. Continuous integration rewards preserving the visible suite. Review templates reward explaining additions more readily than demonstrating that an existing mechanism should disappear. Repository instructions accumulate the lessons of incidents until agents spend increasing amounts of context interpreting accumulated fear.

When prose and executable authority conflict, executable authority wins.

Adding a mandatory fourteen-field “repair theory contract” would merely reproduce the pathology at the process level. The answer is not more paperwork describing simplicity. The selection pressure must change.

### Changing what counts as progress

A better development environment would make several distinctions operational.

#### Separate observation, inference and invariant

Record what happened separately from the proposed explanation. Ask what neighbouring observations would contradict that explanation. Do not let the first plausible diagnosis arrive pre-packaged as the permanent contract.

#### Diagnose a failure class before prescribing a route

Test nearby inputs, alternative sequences, different timings and plausible competing causes. The aim is not exhaustive proof, but enough variation to discover whether the proposed repair generalises or merely recognises the incident.

#### Test outcomes while allowing implementation freedom

Where the internal path is not itself a requirement, test the observable result and prohibited effects. Accept multiple valid implementations. Characterisation tests for legacy machinery may still be useful, but they should not silently govern its replacement.

#### Compare additions with the simplest credible alternative

Before adding a new layer, compare it with removing or bypassing an existing one. A no-new-mechanism baseline is often more revealing than a more elaborate competing design. The baseline will not always win; its purpose is to make complexity earn its place.

#### Use evidence the implementation agent has not already absorbed

Neighbouring and held-out cases can expose repairs that merely encode visible examples. Where appropriate, use an independent reviewer or protected evaluation set that initially reports outcomes and failure classes rather than handing the implementer every case. A “hidden” suite readable by the coding agent is not a holdout.

#### Give deletion equal status with addition

A task should be closable by proving that a stage, adapter, fallback, rule or test is unnecessary. Temporary mechanisms should have removal conditions. When a replacement succeeds, delete what it replaced. Permanent dual paths are how the next tower begins.

Whether this development environment can be achieved within current dev platforms and with current coding agents remains to be seen.

### A shared theory of purpose

Large systems do need shared intent; they do not need an encyclopaedic contract describing every permitted implementation.

They need a compact, revisable account of the jobs the system exists to perform, the outcomes that matter, the states that are genuinely unacceptable, the constraints that are hard, the mechanisms that are merely current choices, and the evidence that would justify changing or removing them.

Without that shared theory, the test suite becomes the de facto product definition and the issue tracker becomes the architecture. The purpose of such guidance is not to dictate every repair. It is to give coding agents enough context and authority to recognise when the right action is subtraction.

### The standard of belief

A coherent explanation is not enough. Coding agents are extremely good at coherent explanations.

For any substantial new mechanism, the harder questions are:

- What class of failures does this prevent?

- What valid behaviours or future designs might it exclude?

- What evidence would cause us to remove it?

If there is no serious answer to the last question, the mechanism is unlikely to remain an engineering decision. It will become a fossil.

### What to look for in your own repository

The reliability ratchet is a hypothesis other teams can test. Look for:

- Local regression rates falling while change latency and validation burden rise.

- Baselines that are repeatedly waived or refreshed upward.

- Tests that prescribe internal routes rather than observable outcomes.

- Temporary fallbacks with no removal conditions.

- Agents that spend more effort satisfying accumulated structure than solving the underlying problem.

None of these observations alone proves that a system is over-engineered. Together, they suggest that local reliability may be purchased by quietly narrowing the future.

Coding agents are capable of extraordinary work. The answer is neither to constrain them less indiscriminately nor to trust them without any hard boundaries.

It is to constrain the right things, and to preserve enough room for a better explanation, an unanticipated solution, or the discovery that yesterday’s successful fix should no longer exist.

**Intelligence needs boundaries. It also needs room.**

## Article 2 — One turn of the ratchet

*Using the risk of a reliability ratchet as an engineering constraint*

After publishing [*The Reliability Ratchet*](https://michaelwitbrock.com/2026/08/01/the-reliability-ratchet/), we (GPT 5.6 Sol Ultra Codex and I) used its argument, verbatim, as context for another substantial piece of engineering work. What followed suggested a practical role for architectural writing that the original post had not quite articulated.

It was useful in a way worth distinguishing from a checklist or rule.

It did not prescribe a solution. It changed the burden of proof. New mechanisms had to outperform a code change that would perform adequately while simplifying the code. Existing tests could be questioned when they protected an internal code choice rather than a broader outcome. This use of the context affected the process;  work was reconsidered as new evidence appeared. Several coherent, well-tested fixes were rejected and redesigned because they would have made the local case tidier by creating a broader restriction.

This suggests a practical role for architectural writing: a compact description of a known pathology can supply useful counter-pressure at the moment when the repository, ticket and test suite all press towards closure using a local fix. It gives a coding agent the vocabulary—and the permission—to notice when its own proposed repair reproduces the disease it is meant to cure.

The limitation is crucial and points to the usefulness of such advisory writing in modern AI-assisted software architecture and construction. If such an essay is converted into a compulsory checklist, schema or approval stage, it may itself become another tooth in the ratchet, driving the code into a state that fixes all previous bugs, but prevents future evoluton towards intented function. Such advice, in Its most useful form is a revisable lens: What independent invariant is being protected? What simpler alternative was compared? What existing machinery can now disappear? What evidence would make us change course?

A principle earns its place when it changes a decision. It overreaches when it insists on becoming machinery.

*Aug 18th 2026, 9:40 AM, Ljubljana*

## Tracked cases

These are dated, revisable evidence records, not automatic release gates.
A case should distinguish what was observed from the causal interpretation,
name the independent outcome or invariant worth preserving, and state what
evidence could change its diagnosis or status. The prose structure may vary
with the evidence; it is not a compulsory schema.

### RR-001 — A valid authority invariant fossilised into a broken workflow-route requirement

- **Observed:** 18 August 2026; scope widened 19 August 2026
- **Status:** Open — diagnosis established and since broadened from a
  workflow-route requirement to a delegation-transport gap seen on three
  unrelated routes; no runtime repair was made by this documentation change
- **Delivery tracking:** [JVNAUTOSCI-2649](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2649)
  owned the repair for the durable workflow route and is Done. It deliberately
  scoped to that route: its ship criteria require that sessionless and internal
  MCP effects remain denied. The remaining routes recorded in the 19 August 2026
  widening are owned by
  [JVNAUTOSCI-2653](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2653),
  which holds the open decision on whether an authorised operator repair should
  have any route at all. Repair alternatives, delivery decisions, acceptance
  evidence, and implementation status belong to those tickets, not here.
- **User outcome:** Represent a scholarly article and its metadata in the
  authenticated user's private Vontology context
- **Observed impact:** The represented workflow was launched twice and both
  instances failed at their first generic ontology mutation. Direct fallback
  effects left the overall outcome partial and indeterminate, while the final
  report described a workflow-instance read-back as though it concerned the
  article target.
- **Evidence boundary:** Failure capsule generated at
  `2026-08-18T14:10:06.247605Z` for request
  `845f7501-8a13-417d-a7c8-48e1a85441fe`, produced by commit
  `9c0803cb0b332b0c2d81be8130aa926c27b82813`
- **Workflow:** `#V#scholarly_article_metadata_representation_workflow`
- **Failed instances:** `d74d0e99-8cc5-4509-a010-dec519133bba`
  (evidence `ev_YvGwmSM5K3UTOFgQWpjGGApN`) and
  `15352a1c-1c64-4f11-974c-c664a515f047`
  (evidence `ev_2opEFvdKubCsUwjTH4t9rO3Z`)
- **Canonical scope reported by the capsule:** `user`

#### What was observed

Both workflow instances failed with:

```text
ontology_agent_delegation_required
An executing agent needs a server-issued delegation bound to this exact ontology effect.
```

The read-back conclusively established that the two workflow instances were in
the terminal `failed` state. It did not establish that the scholarly-article
target had been read back exactly. In the same turn, direct fallback effects
included an indeterminate concept creation and two handler-reported successful
text upserts, so the requested representation could not honestly be called
complete.

#### The independent invariant worth preserving

The security requirement is real: untrusted model output, a represented
workflow, or spoofable client identity must not enlarge an authenticated
actor's ontology authority. Organisation-wide or global publication,
authority-changing effects, destructive shared mutations, and cross-user access
need a server-enforced boundary. Exact, short-lived delegation is one useful
way to bind the grantor, executing agent, audience, tool, effect, workflow or
turn, intended target and delta, and expiry.

The pathology is therefore not “security checks are bad” or “delegation should
be removed”. It is that one particular transport mechanism became an inherited
route requirement without a complete, function-preserving route for the
authorised workflow.

#### The ratchet sequence in this case

| Ratchet stage | This case |
|---|---|
| **Observation** | Agent-mediated ontology effects presented a real confused-deputy and identity-spoofing risk, especially for shared publication and authority changes. |
| **Conjecture** | Every agent-labelled ontology mutation must arrive with an exact server-issued delegation before the actor's direct authority may even be considered. |
| **Mechanism** | The publication authority service unconditionally denies an agent-labelled effect with no delegation. |
| **Regression test** | Negative tests prove that a tokenless call is denied; the positive workflow-propagation test manually injects a delegation-shaped value rather than exercising normal production issuance. |
| **Inherited law** | The durable workflow executor supplies no delegation, but its generic ontology actions are still required to present one. A valid private additive workflow therefore fails by construction. |

The distinction between the independent invariant and its current mechanism is
critical. The earlier authority work had good reason to prevent ambient or
client-supplied identity from becoming semantic-administrator authority.
Available evidence does not justify claiming that the original security
diagnosis was false. What it does establish is that the implementation was
allowed to count as complete without preserving a normal authorised workflow
outcome.

#### Exact causal path

1. Direct adaptive ontology calls automatically issue and bind a same-turn exact
   delegation after their method and arguments are known in
   [`adaptive_turn_service.py`](../../src/backend/services/adaptive_turn_service.py).
2. The represented capability's outer method is `workflow_execute`, which is
   not itself classified as an ontology mutation, so that issuance path is
   skipped.
3. The durable executor constructs its environment without an
   `ontology_delegation_id` in
   [`durable_executor.py`](../../src/backend/workflows/durable/durable_executor.py).
4. The first generic `create_concepts` step in the
   [paper workflow seed](../../src/backend/workflows/repo_seed_bundles/paper_representation_workflow_seed_bundle.json)
   labels the effect as workflow-agent execution and propagates the missing
   value through
   [`workflow_mcp_tool_actions.py`](../../src/backend/workflows/workflow_mcp_tool_actions.py).
5. The shared
   [publication authority service](../../src/backend/services/ontology_publication_authority_service.py)
   denies the effect before considering whether the authenticated user has
   direct authority for the ordinary private additive write.
6. The positive regression test in
   [`test_ontology_authority_tier3_regressions.py`](../../tests/backend/test_ontology_authority_tier3_regressions.py)
   checks propagation only by manually supplying `"server-issued-only"`; it
   does not prove that a production workflow can obtain a valid grant.
7. Custom scholarly-workflow handlers in
   [`paper_representation_workflow.py`](../../src/backend/workflows/durable/paper_representation_workflow.py)
   call lower-level mutation services directly. Semantically equivalent effects
   can therefore bypass the boundary that blocks generic actions.

Each local component can explain its behaviour: the authoriser denies a missing
credential, the gateway records agent provenance, the workflow propagates the
field it received, and the regression test confirms the selected rule. Their
agreement is not independent evidence that the end-to-end design works. It is
the same implementation conjecture repeated through code and tests while the
user outcome disappears—precisely the “accidental constitution” described in
the first article.

#### Reporting amplified the pathology

The outcome renderer then made the mechanism sound more authoritative than its
evidence allowed:

- `changed: true` meant that a durable workflow-instance record was created,
  not that article metadata changed;
- `outcome_resolved: true` meant the workflow failure was conclusively known,
  not that the task succeeded;
- the generic target projection reused the workflow `instance_id`, producing
  identical `instance` and `target` identifiers; and
- “the current target was read back exactly” referred to rereading the failed
  workflow instance, not the scholarly article.

This is another small ratchet effect: operational bookkeeping acquired
domain-sounding language and obscured the missing user outcome.

#### Why this qualifies as the pathology

The guard is locally defensible but globally constraining. It closes a selected
unsafe route, yet in this private additive case it also erases a safe,
authorised route that the server already has enough trusted context to bind.
No further human approval is semantically required: the direct path creates the
same kind of delegation automatically. Requiring the token without providing
an issuer adds user-visible failure and architectural burden without adding a
corresponding decision.

The enforcement is also route-dependent: governed generic actions fail while
custom handlers can bypass it. That is evidence that the current mechanism is
not yet identical with the intended security invariant.

#### Scope widened — 19 August 2026

The original framing above, "workflow-route requirement", is too narrow. The
same missing issuer was met on two further, unrelated routes while attempting an
explicitly authorised ontology repair tracked by
[JVNAUTOSCI-2651](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2651):
reclassifying twelve workflow action contracts wrongly recorded as instances of
`#V#mcp_tool`.

| Route | Result |
|---|---|
| Durable workflow executor | Supplies no delegation; the original RR-001 observation |
| `add_relationship` on direct stdio | `ontology_sessionless_delegation_not_supported` |
| `von_chat_run` | `von_chat_run_read_only`; an explicit `allow_writes=true` is rejected outright |

Three unrelated routes, one absent issuer. The defect is therefore in the
delegation transport rather than in the security invariant, which is a stronger
claim than this record originally supported.

The `von_chat_run` observation is distinctive and worth separating from the
others. Its own access profile reports `write_mode:
enabled_canonical_primary_with_explicit_approval` and
`write_category_tools_allowed: true`, while the entry point is unconditionally
read-only. Write capability is configured for the profile and unreachable
through it. Configuration that describes an affordance the code will never grant
is a quieter failure than a denial, because nothing contradicts it until someone
tries.

The blocked effect had every property that should make authorisation easy:
method known, arguments known, targets known, prior state captured, fully
reversible, and explicitly approved by the repository owner. No surface
distinguishes that case from an open-ended grant, so both are refused
identically. The stated reason for closing sessionless mutation is sound —
binding a grantor before caller arguments are matched to a stored exact intent
would expose a private read oracle — but that reasoning does not apply to an
exact specified effect, and no route exists that can tell the two apart.

This widening was recorded rather than opened as a separate case. The phenomenon
is one missing issuer observed three times, and splitting it across records would
make a systemic gap read as several local ones. A case log that accumulates an
entry per instance while nothing consolidates them is the ratchet operating on
the evidence log itself.

#### Evidence that would change this record

This record should be revised, narrowed, or closed if later evidence shows any
of the following:

- the failed workflow did receive a valid production-issued delegation and the
  recorded denial arose from a different authority fact;
- the executing route did not have server-established actor identity, or the
  intended effect crossed the actor's private publication boundary;
- exact workflow-instance read-back also established the requested article
  postcondition through evidence omitted from the capsule;
- the route-wide delegation requirement prevents a concrete materially
  unacceptable outcome that trusted actor binding, exact private scope,
  bounded effect policy, receipts, read-back, and recovery cannot adequately
  contain; or
- an outcome-level replay shows that the described causal sequence is no longer
  present in the relevant release.

Repair selection, delivery criteria, implementation progress, and release
evidence do not belong in this case record. They are current design and work
tracking concerns owned by
[JVNAUTOSCI-2649](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2649).
The case remains open as historical evidence until a dated outcome-level result
supports reclassification; that status does not prescribe which repair must be
used.

### RR-002 — A correct fix that grew a surplus guard, and the guard's own defence

- **Observed:** 19 August 2026
- **Status:** Closed by subtraction — the surplus mechanism was removed the same
  day, before it had ever fired
- **Related work:** [JVNAUTOSCI-2650](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2650),
  PRs #402 and #403, reversal in this change
- **User outcome:** Resolve MCP tool concepts from code instead of stale copies
  in MongoDB
- **Distinguishing feature:** Unlike RR-001, the causal diagnosis here was
  correct and evidenced by a stack trace. The pathology is in what was built
  *on top of* a correct repair.

#### What was observed

While implementing virtual concept providers, a single `fetch_concept` never
returned. A `faulthandler` stack dump showed an unbounded cycle: access control
asked whether a concept was virtual, the tool provider answered by building the
contract registry, building contracts loaded tool metadata from Vontology,
loading that metadata ran an access-control check, which asked again.
`lru_cache` does not guard re-entrancy, so each level rebuilt from scratch.

#### The repair, and the surplus

The no-new-mechanism fix was to make `owns` decide from a cheap in-memory name
set. That alone resolved the failure completely.

A thread-local re-entrancy guard was then added *on top*, refusing to recurse if
the cycle were ever reintroduced. It was justified by a hypothetical future
provider rather than an observed failure class, it had no removal condition, and
it degraded a loud failure into a quiet one: the suppressed call answers "not
virtual" for a concept that is virtual, which in access control means falling
through to rules a virtual concept cannot satisfy. A defence against a
hypothetical fault could therefore have denied a real one.

#### The revealing second turn

Asked whether the hazard was documented well enough to prevent recurrence, the
implementing agent answered by adding more machinery — a trip counter, a log
line, a `guard_trip_count` accessor and a further test — and shipped it as
"Make the re-entrancy rule enforceable". It never asked whether the guard should
exist. That is the article's "when a neighbouring case fails, another mechanism
is added outside the first", occurring inside a change whose stated purpose was
to harden a guardrail.

#### What was kept and what was removed

Removed: the guard, its counter, its logging and the two tests that existed only
to exercise it.

Kept: the rule stated on the `VirtualConceptProvider.owns` contract where an
implementer reads it, and a structural test asserting that no registered
provider's `owns` reaches `get_canonical_tool_registry`, `get_tool_metadata`,
`_load_from_vontology` or `can_access_concept`.

That test is itself worth recording, because two earlier versions of it were
useless. A version asserting the guard-trip count stayed at zero passed with the
bug deliberately reintroduced, because the contract registry was already warm.
A version that cleared the caches first also passed, because closing the cycle
needs stored concepts for the access check to run on. Only tripwires on the
forbidden calls failed against the reintroduced bug. Two coherent, plausible
regression tests provided no evidence at all, which is the first article's point
about a conjecture repeated in executable form.

#### A dual path this case declined to remove

The same programme left 52 materialised `#V#mcp_tool` rows coexisting with
virtual resolution, with materialised winning and no removal condition — the
"permanent dual paths" warning. Removing them was attempted and **declined on
evidence**:

- 12 of the 52 are not MCP tools at all. They have empty attributes, no
  `mcp_tool_name`, and are each the target of `#V#invokesAction` from an
  `#V#entity_representation_*_step`. They are workflow step actions
  misclassified as instances of `#V#mcp_tool`, and deleting them would be data
  loss with no replacement. They need an ontology repair, not a deletion.
- The remaining 40 carry operational metadata the virtual provider does not
  serve: `display_template` on 34, `user_salience` on 34, `planner_hint`, and
  evidence-contract wiring on 8.

The rows are therefore not redundant with the virtual path. They are a second
copy of a *different* class of data whose code defaults already exist in
`_DEFAULT_TOOL_METADATA`, and which has silently diverged in both directions:
20 of 39 named tools match their code defaults exactly, and 19 differ, some
where the code appears newer and some where only the database holds a value.

Deleting them would have destroyed the only copy of 19 tools' curated values.
Recording this as a declined action matters more than the deletion would have:
the "obvious cleanup" was itself a candidate ratchet move, removing machinery on
a tidiness argument rather than evidence.

#### Evidence that would change this record

- The removed guard proves necessary because a provider reaches the cycle
  through a path the documented rule and structural test do not cover.
- The structural test is shown to pass against some other realisation of the
  same cycle, indicating it recognises one incident rather than a failure class.
- Reconciliation of the 19 divergent tool-metadata entries shows the database
  values were stale rather than curated, making the rows straightforwardly
  deletable after the values move into code.

### RR-003 — A mechanism built without checking whether it could function

- **Observed:** 20 August 2026
- **Status:** Closed by redesign the same day; the gate was replaced with a
  nightly report before merge
- **Delivery tracking:**
  [JVNAUTOSCI-2656](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2656)
- **Distinguishing feature:** RR-002's guard worked and was merely surplus. This
  mechanism could not work at all, and the fact that established it was
  available before any code was written.

#### What was observed

Backend pytest has never run in CI. `test_mcp_manifest_parity` was failing for
an unknown period and was found by accident while investigating something
unrelated. Measurement then established that 110 of 3776 `cost_normal` tests
fail on main with a working database.

The diagnosis was correct and measured rather than conjectured, which is worth
stating plainly: this is not a case of a weak causal theory.

#### The response, and what was wrong with it

A per-pull-request gate, a shard matrix, a runner script, a 209-entry
deselection backlog, and a further test guarding that backlog against growth.

`main` has no branch protection and no required status checks. Four of the last
twenty commits went directly to it. A failing gate therefore blocks nothing. The
mechanism was incapable of doing the job it was built for, and one API call
would have established that before any of it was written.

The simplest credible alternative, a nightly report, was never compared. The
first article asks for exactly that comparison, and the second describes using
the argument to change the burden of proof on new mechanisms. Neither happened.

When the gate then failed on runner timeouts, the response was more machinery:
four shards, then six, then an attempt to tune Mongo timeouts. Each step
addressed the symptom. None asked whether the gate should exist. That is the
article's "when a neighbouring case fails, another mechanism is added outside
the first", observed across a single afternoon.

#### What stopped it

A direct question from the repository owner: why do we really need this. No
internal check caught it, and no test could have.

#### The finding about the case log itself

RR-002 records this precise reflex, surplus machinery on a correct diagnosis
defended with further machinery when challenged. It was written the previous day
by the same agent that then repeated it.

Recording a case did not prevent its recurrence within twenty-four hours. That
is evidence about what this log is: a lens that works when deliberately picked
up, not a passive safeguard that operates by having been written. The second
article anticipates the distinction when it warns against converting the essay
into a checklist; this is the same limit seen from the other side, where the
material is available and simply is not consulted at the moment of decision.

#### What survived

The measurement. That 110 tests fail with a database and 208 without, that
`lane_backend_core` holds 3488 of 3776 `cost_normal` tests so a lane matrix
cannot parallelise the suite, that shortening Mongo timeouts makes the suite
slower rather than faster, and that at least one test depends on state another
test leaves behind. None of that is contingent on the mechanism that was
abandoned.

#### An ambiguity the response assumed away

That 110 tests fail while nothing visibly breaks admits two readings:
enforcement is missing, or those tests were not protecting anything. The second
is the accidental-constitution failure described in the first article. Only the
first was considered, and the backlog was framed as work owed rather than as a
question.

#### Evidence that would change this record

- Branch protection is introduced, at which point a gate becomes capable of the
  job and the redesign should be revisited.
- The nightly report proves insufficient to surface drift within a useful time.
- Review of the 209 recorded failures shows they protect outcomes that matter,
  settling the ambiguity above in favour of enforcement.
