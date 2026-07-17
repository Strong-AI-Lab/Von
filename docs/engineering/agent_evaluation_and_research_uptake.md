# Agent Evaluation and Research Uptake

- **Kind:** Evaluation and research-uptake guide
- **Lifecycle:** Active
- **Authority:** Normative only for the evaluation or research claim being made
- **Last reviewed:** 18 July 2026

## 1. When to read this

Consult the relevant sections before planning or implementing work that is:

- architecture-shaping
- benchmark-related
- evaluation-heavy
- research-sensitive
- intended to incorporate a recent method from the literature

## 2. Core doctrine

Do not trust optimistic agent benchmarks or one-shot demos by default.

Evaluate the property you actually care about, on the nearest real task path, with a clear baseline and a clear reporting story.

## 3. Evaluation dimensions to consider

Where relevant, measure:

- task success
- reliability across multiple trials
- policy adherence
- perturbation robustness
- cost and latency
- failure visibility
- retrieval hit quality
- memory use quality
- uncertainty or calibration quality
- recovery behaviour

## 4. Benchmark scepticism

Before relying on a benchmark, ask:

- Does it reflect the deployed task?
- Does it score the final world state or only text similarity?
- Does it hide important failure modes?
- Does it over-reward empty or partial responses?
- Would a simpler baseline already do well?

## 5. Real-path acceptance

For user-visible or end-to-end claims, acceptance should use:

- the exact production path, or
- the nearest faithful path if production cannot be used safely

Nearby unit tests and synthetic harnesses are supporting evidence, not the whole story.

## 6. Literature note requirement

For research-sensitive tasks, write a short note that records:

- what recent work was checked
- which claim from that work actually matters to Von
- what changed in the design because of it
- what was rejected and why
- what follow-up measurement remains open

## 7. Research uptake rules

Adopt a new method only when it:

- addresses a real bottleneck
- fits Von's authority surfaces
- has a credible evaluation plan
- has an acceptable rollback path
- does not quietly increase architectural opacity

For the current KA/KCAP/common-sense KR/scientific KR uptake pass that proposes
new Vontology tooling surfaces, see
`docs/engineering/vontology_tooling_from_ka_kcap_kr_literature.md`. Use it as a
companion when implementing or evaluating Vontology tools that change retrieval,
knowledge acquisition, context handling, provenance, or scientific
representation behaviour.

## 8. Reporting discipline

When reporting results, state clearly:

- evaluation dataset or task
- number of trials where relevant
- baseline
- changed variable
- cost or latency effect
- important caveats

## 9. Acceptance checklist

Before closing research-sensitive or evaluation-heavy work, verify:

- the baseline was appropriate
- the measured metric matches the claimed improvement
- the evaluation path is reproducible
- the task notes capture the literature-informed design choice
- any benchmark limitation that materially affects interpretation was documented
