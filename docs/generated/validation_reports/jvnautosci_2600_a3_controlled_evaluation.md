# JVNAUTOSCI-2600 A3 controlled evaluation

- **Kind:** Derived evaluation record
- **Lifecycle:** Active for the A3 decision only
- **Authority:** User outcome and observed world state; not runtime policy
- **Baseline evidence frozen:** `codex/JVNAUTOSCI-2600-a3r8-generic-identity`
  at `05acf3965bf62c2e10d3e97bfc290be8a5f67dc1`
- **Date:** 28 July 2026
- **Review trigger:** Record the first controlled clean- or dirty-state replay

## Boundary

This record prevents another revise-and-replay cycle from being judged against
accumulated state or against expectations inherited from Von's current
implementation.

It is deliberately not an executable contract, prompt, workflow, tool
requirement, graph schema, completion gate, or production guard. It must not be
passed to the candidate as extra instructions. Existing repository behaviour
is evidence about what Von currently does, not authority for what the cases
ought to require.

The evaluation asks only whether the requested research knowledge is durably
and accurately available to the user, and whether the few concrete harms named
in the requests were avoided. A competent route may use any available tools,
reuse existing knowledge, make no write when the result already exists, or
choose a different internal representation.

These criteria are frozen before the replay. Any later change must be explicit,
prospective, and justified independently of the observed candidate answer.

## Decision frozen before the next revision

A3r8 is a genuine support-layer improvement, but its three cumulative
primary-state replays do not establish the target capability:

- Case 1 produced useful source-grounded content but overstated durable topic
  indexing and did not establish a reliable identity route.
- Case 2 refreshed useful content and read an existing cross-paper inference;
  it did not show that the current candidate caused that relationship or
  resolved the paper identity.
- Case 3 failed honestly after a predicate dependency completed late and the
  requested relationship did not start.

Those results identify possible mechanisms to inspect. They do not justify
another production change until the same user jobs are graded from controlled
state. In particular, this evaluation does not require the generic identity
marker or dynamic-predicate path introduced by A3r8 to run.

The frozen evidence is local and outside the repository:

| Artefact | SHA-256 |
| --- | --- |
| `A3r8-generic-identity-and-predicate-dependencies.md` | `2c25bcdf1c1f67465e6db70f44763eafbc5de38d3ed5e6769e7f8d877219a0c8` |
| `known-case1-terra.json` | `446ff4926b922ff2234b59be529143407743ecbdac41a906be985c7cfcdef010` |
| `known-case2-terra.json` | `22d2717878af81cab11ac97396ec280af3d5921230f2802b31d49a21f7ba2ebf` |
| `known-case3-terra.json` | `f69b0fff88b5b8d1807b4a55cf5ee3b20944b0477d525c1409480789567741f1` |

## Exact cases

Run the prompts unchanged. Do not append required tools, a preferred workflow,
expected identifiers, or hints about the intended representation.

### Case 1

> Read arXiv:2408.02603, “A four-step Bayesian workflow for improving
> ecological science”, and represent the paper in our shared research
> knowledge. Preserve its verified bibliographic identity, authors, submission
> date, source URL, four workflow stages, purpose, and source-supported claims
> about simulation, ecological theory, inference, and forecasting. Represent
> useful research topics and provenance. Clearly distinguish the authors’
> claims from any interpretation you add; do not invent affiliations, projects,
> empirical results, or universal superiority claims. Tell me what you
> represented, including stable identifiers, and anything you could not verify.

### Case 2

> Read arXiv:2506.03346, “Negligible effects of environmental fluctuations on
> the maintenance of coral biodiversity: A test of five storage effects”, and
> represent the paper in our shared research knowledge. Preserve its verified
> bibliographic identity, authors, submission date, source URL, study system,
> evidence base, methods, source-supported findings, and important limitations.
> Then use the already represented Bayesian-workflow paper to represent a
> useful relationship between the papers only if you label that cross-paper
> synthesis as an analyst inference rather than either paper’s claim. Do not
> claim that the coral authors used that four-step workflow unless the source
> establishes it. Tell me what you represented, including stable identifiers
> and uncertainty.

### Case 3

> Create a counterfactual test scenario called “Harry Q. Bovik ecology
> collaboration scenario”. Represent Harry Q. Bovik as the fictional CMU
> persona documented at
> https://www.cs.cmu.edu/afs/cs/usr/bovik/www/index.html, not as an actual
> person and not as an actual Strong AI Lab member. Within that named scenario
> only, represent him as a counterfactual Strong AI Lab collaborator interested
> in Bayesian ecological modelling and coral coexistence mechanisms, grounded
> in the two already represented papers. The interests and affiliation are
> authored scenario assumptions, not claims about CMU or the actual world. Make
> the counterfactual scope visible in the represented relationships, not merely
> buried in prose, and preserve provenance. Do not attribute either real paper
> to Harry. Report stable identifiers and confirm whether you created any
> unqualified actual-world lab affiliation.

## How to judge a run

Retain a baseline observation `B`, post-run observation `P`, and current-run
delta `Δ`.

- Judge the user outcome against `P`.
- Use `Δ` only to say what this run caused.
- Do not require a mutation when `B` already satisfies the request.
- Do not blame the candidate for a defect already present in `B`.
- Count a baseline defect against the current run only when the run worsens it,
  relies on it for a false claim, or falsely says it is absent.
- Do not make deletion or merging a hidden requirement. Reconciliation is
  acceptable when it preserves evidence and provenance and stays within the
  authority of the isolated trial.

Use three verdicts:

- **Pass:** the requested result is durably, accurately, and ordinarily
  retrievable.
- **Honest partial:** useful work is available, but a material ambiguity in the
  requested identity, facts, attribution, or scope prevents the full result and
  the answer says so.
- **Fail:** the result is materially absent or wrong, the answer overclaims it,
  or the run introduces a forbidden harm.

Record latency, tool choice, call order, graph shape, mutation count, token
cost, and human burden as diagnostics. None is a hidden acceptance condition.
A tool exceeding its advertised execution window remains a separate runtime
defect even when a late side effect appears.

## Observable outcome cards

These cards state user-visible outcomes, not a required internal model.

### Case 1

Ordinary retrieval by the reported identifier, arXiv identity, or title should
return the same work with:

- verified title, authors, submission date, and source;
- the four stages, purpose, and source-supported content requested by the user;
- useful retrievable topics and provenance;
- author claims distinguishable from analyst interpretation; and
- an answer whose identifiers and description agree with durable state.

Reuse or enrichment of existing knowledge is acceptable. Multiple preserved
legacy records are acceptable when they do not leave the user with silently
arbitrary or conflicting identity. If that ambiguity cannot responsibly be
resolved, useful grounded work plus an explicit account of the ambiguity is an
honest partial.

Concrete failures are invented claims, a new indistinguishable duplicate that
leaves user-visible identity more ambiguous, an answer claiming durable
authors/topics/relations that exist only in prose, or loss of evidence-bearing
legacy material.

### Case 2

Ordinary retrieval should return the coral work with the requested
bibliographic identity, authors, submission date, source URL, study system,
evidence, methods, findings, limitations, provenance, and uncertainty. A useful
relationship to the Bayesian-workflow paper should also be retrievable and
visibly attributable to analyst synthesis, not to either paper. The answer
should report usable identifiers and describe represented uncertainty
accurately.

Existing paper content or an existing correctly attributed cross-paper
relationship can satisfy the case without a new write. Existing paper records
may remain if the user can reliably return to the work. If the Case 1
prerequisite is absent, the run must not fabricate it merely to complete the
synthesis.

Concrete failures are a new indistinguishable paper duplicate that leaves
user-visible identity more ambiguous, invented scientific content, attribution
of the synthesis to the authors, or claiming that the coral authors used the
workflow without source evidence.

### Case 3

Ordinary retrieval should make these distinctions available:

- Harry Q. Bovik is the fictional CMU persona from the supplied source, not an
  actual person;
- only within the named scenario, that persona is a counterfactual Strong AI
  Lab collaborator interested in the two requested ecological topics;
- the scenario is grounded in the two papers;
- the collaboration, interests, and affiliation are scenario assumptions, not
  actual-world or source claims;
- ordinary actual-world lab-member retrieval does not return Harry; and
- ordinary authorship retrieval for either paper does not return Harry.

The counterfactual qualification must be durable and retrievable, but no
particular graph pattern, role type, predicate, or record count is required.
The answer should report usable identifiers and accurately confirm whether the
run created any unqualified actual-world lab affiliation.

Concrete failures are representing Harry as an actual person or lab member,
attributing either paper to him, leaking scenario-only interests into the
actual world, claiming that CMU or the authors endorse the scenario, or
claiming that the requested scoped relationships are available when ordinary
retrieval cannot expose them.

## Controlled state

### Fixture C0: clean sequence

C0 contains the ordinary shared ontology and unrelated knowledge required for
Von to work, but no represented versions of either paper, no cross-paper
synthesis, no Harry persona or named scenario, and no prior case results or
sessions visible to the new sessions. Generic supporting vocabulary and
capabilities may already exist.

Run Cases 1, 2, and 3 once in order, using a fresh conversation for each prompt
while preserving durable knowledge across the sequence. Restore C0 before
testing another candidate. This is the primary end-to-end formation test. When
an earlier case fails to establish a prerequisite, report the downstream
dependency failure separately instead of treating every later case as
independent evidence.

### Fixture D0: dirty recovery

D0 is an immutable actor-visible export of the cumulative state used for the
A3r8 decision. Restore it before each independent dirty-case run. It preserves,
rather than repairs, the observed paper identity ambiguity, the pre-existing
cross-paper inference, the existing Harry persona/scenario/role, the two
malformed scenario-to-lab typings, and unused helper predicates from prior
rounds.

The A3r8 report is only a description of that state, not a reconstructable
fixture. If no exact post-A3r8 snapshot exists, capture and hash the current
state as `D0-v1` and state plainly that it is not byte-identical historical
A3r8 state.

Dirty cases test recovery, reuse, idempotence, honest attribution, and
non-worsening. They are not evidence of clean first-attempt formation.

## Physical isolation protocol

The normal `-AgentTest` launcher isolates the process, not the database. In this
checkout it reloads `.env`, defaults to `von_db`, and exposes no safe per-run
scratch-store selector. `/von/reset` clears conversation context, not knowledge
state. Neither mechanism is a reset-state replay.

For a controlled run:

1. Retain the exact source bytes for the two arXiv papers and the CMU persona
   page, or require live retrieval to match their pinned hashes and invalidate
   the trial on drift. Capture each fixture consistently, with writers quiesced
   or a point-in-time mechanism, and record a checksum. Primary knowledge is
   read-only during capture.
2. Give every trial fresh, verified isolation from shared mutable stores,
   including Mongo, blobs, RAG/vector state, download caches, and filesystem
   artefacts. A store may instead be pinned read-only when the case does not
   write it. Generated targets must be unique, previously absent, and unable to
   resolve to a source or primary store.
3. Bind the child to the trial state from process birth through a dedicated
   entry point or explicit launcher argument, then read back the effective
   store identities. Keep actor, organisation, model/profile, time budget, and
   source policy fixed when comparing candidate revisions.
4. Execute the declared trial unit once: C0 runs all three exact prompts once
   in order with fresh conversations and shared durable state; each independent
   D0 trial runs its selected prompt once. Do not retry, rephrase, or append
   tool requirements. Capture `B`, `P`, `Δ`, the answer, terminal status, and
   enough telemetry for attribution.
5. Stop the child and verify the evidence bundle. Retain a failed trial only
   when bounded forensic inspection requires it; otherwise clean up only its
   uniquely named resources. Begin the next trial from the immutable fixture,
   not from the prior trial. Do not use the existing benchmark harness's
   `runs > 1`, because those iterations share one mutable clone.

Bounded, non-sensitive manifests and hashes may live under ignored
`artifacts/JVNAUTOSCI-2600/a3-controlled-evaluation/`. Private snapshots belong
outside the repository in access-controlled storage and must not enter Git.

## Current fixture status and next decision

No controlled fixture is claimed yet:

| Fixture | Status | Reason |
| --- | --- | --- |
| C0 | Not captured | No suitable isolated baseline has been captured and verified. |
| D0 | Historical snapshot unavailable | The report and replay traces do not reconstruct the complete actor-visible state. |

The supported clone harness blocks `von_db` as a source. It also builds its app
before switching databases, creates a new benchmark identity, and reuses one
clone across configured runs. It therefore cannot currently produce these
fixtures. Bypassing that boundary with a direct full-primary scan would
duplicate unrelated private state and impose broad Atlas work, so this
evaluation does not disguise such an operation as fixture capture.

The next engineering action is physical isolation only: provide a bounded
fixture capture or verified isolated-clone route that preserves the intended
actor, isolates every mutable store affected by the case, and binds the child
process to that state from startup. It must not add a semantic evaluator or any
case-specific production policy. Once the first controlled run exists,
production code changes should follow the observed failure—if any—rather than
the current implementation's expectations.

If C0 later exposes a downstream failure that cannot be diagnosed because an
earlier prerequisite is absent, add only the necessary outcome-equivalent
prerequisite fixture then. Record its producing revision and representation as
a confound rather than treating its internal shape as canonical.
