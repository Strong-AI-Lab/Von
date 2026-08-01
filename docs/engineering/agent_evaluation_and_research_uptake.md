# Agent Evaluation and Research Uptake

- **Kind:** Evaluation and research-uptake guide
- **Lifecycle:** Active
- **Authority:** Normative only for the evaluation or research claim being made
- **Last reviewed:** 1 August 2026

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
- useful-action rate and false refusal or abandonment
- unnecessary clarification, confirmation, and approval burden
- reliability across multiple trials
- policy adherence
- perturbation robustness
- cost and latency
- failure visibility
- retrieval hit quality
- memory use quality
- uncertainty or calibration quality
- recovery success, time, and burden after induced mistakes
- residual harm weighted by credible consequence rather than operation label

## 4. Benchmark scepticism

Before relying on a benchmark, ask:

- Does it reflect the deployed task?
- Does it score the final world state or only text similarity?
- Does it hide important failure modes?
- Does it over-reward empty or partial responses?
- Does it over-reward refusal, clarification, or confirmation that avoids useful
  low-risk work?
- Does its distribution resemble Von's predominantly ordinary administrative
  and scientific workload, or do adversarial boundary cases dominate far beyond
  their real base rate?
- Can materially different competent strategies pass, or is one internal path
  encoded as the answer?
- Would a simpler baseline already do well?

## 5. Real-path acceptance

For user-visible or end-to-end claims, acceptance should use:

- the exact production path, or
- the nearest faithful path if production cannot be used safely

Nearby unit tests and synthetic harnesses are supporting evidence, not the whole story.

For ordinary capability claims, representative low-risk work should dominate
the evaluation distribution. Include high-consequence and adversarial cases in
proportion to the claim, and weight failures by plausible residual harm,
detectability, and recovery. Otherwise the benchmark will select for a
paralysed agent even when the prose asks for useful autonomy.

## 6. Literature note requirement

When a design or acceptance claim relies materially on literature, write a
short note containing only what is needed to interpret that claim:

- what recent work was checked
- which claim from that work actually matters to Von
- what changed in the design because of it
- what was rejected and why
- any measurement gap that materially limits the current conclusion; do not
  invent follow-up work

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

## 9. Acceptance prompts

For research-sensitive or evaluation-heavy work, use the items material to the
claim:

- the baseline was appropriate
- the measured metric matches the claimed improvement
- useful action, unnecessary intervention, residual harm, and recovery are
  measured where they are material
- the evaluation path is reproducible
- the task notes capture the literature-informed design choice
- any benchmark limitation that materially affects interpretation was documented
