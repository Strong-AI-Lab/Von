# Minimal Imposition Benchmark Compatibility Note

- **Kind:** Current-implementation compatibility note
- **Lifecycle:** Retired as policy and acceptance authority
- **Authority:** Evidence only; the weighted model must not gate behaviour or
  dictate replacement architecture
- **Created:** 2026-04-04
- **Last reviewed:** 2026-07-25

## Purpose

Von currently exposes an older weighted minimal-imposition model through:

- `turn_execution_build_benchmark`
- `turn_execution_build_dashboard`
- `#V#minimal_imposition_benchmark_profile_autopilot_v1`

The profile combines interruption, clarification, approval, disruption,
overreach, recovery, false-success, and escalation proxies into composite
scores. Exact retired dimensions, thresholds, scenario labels, and Jira history
remain in version control.

## Interpretation boundary

The composite is compatibility telemetry, not a theory of good agent behaviour.
Several dimensions are proxies, some assume old selector/workflow machinery,
and one weighted number can hide a capable answer behind policy arithmetic.

Do not:

- require a turn or replacement controller to populate this profile;
- add stages, receipts, classifiers, or telemetry merely to improve its score;
- extend the dimension taxonomy as a substitute for end-to-end evidence; or
- use the score to certify safety, architecture, or minimal imposition.

## Replacement evaluation

Use the smallest comparison that supports the actual claim. Ordinary evaluation
should resemble Von's predominantly low-risk administrative and scientific
work. Measure final useful outcomes, false refusal or unnecessary interruption,
latency and human burden, and observed recovery or harm only where material.

When code adds a compulsory gate, branch, or wrapper, first apply the evidential
burden in `AGENTS.md`; then compare it with a permissive baseline and count safe
strategies it blocks. Different competent paths may pass. This is not a
universal runtime schema or a fixed evaluator pipeline.

Retire the compatibility profile and its code when no live consumer needs it;
do not preserve it as an oracle for the new controller.
