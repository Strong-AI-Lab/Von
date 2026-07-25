# Legacy Execution-Correctness Benchmark Compatibility Note

- **Kind:** Current-tooling compatibility note and historical record
- **Lifecycle:** Retired as general acceptance authority
- **Authority:** Evidence only; does not prescribe replacement architecture,
  workflow stages, or release gates
- **Original programme:** `JVNAUTOSCI-1657` Phase 1
- **Last reviewed:** 2026-07-25

## What remains

Von's existing benchmark and dashboard tooling may still expose Phase 1
execution labels, selector-routing measures, pre-dispatch latency attribution,
actor/critic overlays, and legacy Jira identifiers including:

- `JVNAUTOSCI-965`
- `JVNAUTOSCI-966`
- `JVNAUTOSCI-967`
- `JVNAUTOSCI-1429`
- `JVNAUTOSCI-1664`
- `JVNAUTOSCI-1838`
- `JVNAUTOSCI-1839`

Current consumers may use those fields as compatibility telemetry. The retired
classification rules, benchmark procedures, seed-bundle conventions, threshold
values, promotion choreography, and closure templates remain in git history.

## Interpretation boundary

The old stack assumed selector, workflow, dispatch, completion-gate, critic, and
dashboard layers that a replacement controller need not contain. A successful
direct tool/function/model path is not incomplete merely because it cannot
populate those layers.

Do not:

- make a legacy score or label a release gate for new architecture;
- add stages, represented artefacts, receipts, or test fixtures to satisfy the
  old evaluator;
- treat selector accuracy as the whole user outcome;
- infer correctness from a `completed` label without checking the material
  answer or effect; or
- extend this protocol with another universal taxonomy.

## Evaluation now

Follow `AGENTS.md` and the active evaluation guide. Use the smallest
architecture-neutral evidence that supports the claim: final useful answer or
world state, honest non-success, relevant provenance, realistic latency and
human burden, and neighbouring cases only when generality is claimed.

When a proposed mechanism blocks possible strategies, compare it with a
permissive baseline. Different competent paths may pass. Retire legacy fields
and code when no live consumer needs them rather than preserving this note as
an oracle.
