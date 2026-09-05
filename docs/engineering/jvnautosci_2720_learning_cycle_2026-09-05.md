# JVNAUTOSCI-2720: completed negative learning cycle

- **Kind:** Experiment dossier
- **Lifecycle:** Frozen
- **Authority:** Evidence only; delivery status belongs to
  [JVNAUTOSCI-2720](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2720)
- **Evidence date:** 5 September 2026 UTC
- **Scope:** One actor-visible message-channel practice, one model and one
  frozen read world; not general organisational competence

## Finding

Von authored a candidate from the designated real discussion, exposed its exact
revision in later independent experimental turns, evaluated the resulting work,
and recorded a negative use disposition from the canonical evidence. Two fresh
subsequent turns verified that the rejected revision was absent. The source,
candidate text and contrary evidence remain inspectable; rejection does not
erase the lesson or assert that every part of it is false.

Advice B did not earn use over the no-advice baseline A. Both passed 5 of 8
applicable trials; B had three paired improvements and three regressions there.
All 24 trials and evaluations completed, with 12 valid, comparable pairs and no
inconclusive evaluations. No represented Arm C or ordinary automatic advice
consumer was activated. This closes the issue's negative-evidence path, not its
conditional positive path or a claim of improved task performance.

## Exact evidence and inspection

- Run: `#V#jvnautosci_2720_learning_advice_run_9f3584c89db54ccabdc3bc0c`.
- Candidate: `#V#learning_candidate_b0fda0e1f472b895d5ee5aab5a412251`, revision 1.
- Body SHA-256: `6e2d47acba92877acd1c2f80fc0917e5d8e76bb2c8a0e73875d275159aa785b4`.
- Revision identity: `7f6ea87ac259aabd01f3e3eb13613acf1b0b907cbf73b993334290b316648cb7`.
- Source locator SHA-256: `309b70ab7bd0606382c2f984790de69698b8aa56f21bde3d4a464789fccf66f2`.
- Manifest SHA-256: `a4c2d581f60c1b603d39a2edabe3b10d1370d02d1318ae2befd9e6aad7bc0a3e`.
- Plan SHA-256: `036d688c7e7896817c84526c0a2b8e7c904ec61aeb2f7e2781f6ccf18c102f00`.
- Result SHA-256: `fe0ce020c7e627dd29786aca50cd3cfbb624d4b4338199f4af950c5cfc00bbdb`.
- Acting/evaluation revision: `aceb1d4bd82c72c2c96483a7f43dc2ddfccef196`.
- Disposition repair and later checks: `93a883fd`.

The source is the user-designated “Testing Von messaging” session
`1eec920c-0be0-4fcf-a045-6733768c8667`; the
[frozen fixture](../../scripts/fixtures/jvnautosci_2720_message_channel_experiment.json)
identifies the exact selected messages, trusted scope, cases and criteria.
Formation used those messages only, before candidate efficacy was observed.
Contributor, actor, organisation, target, audience and beneficiary are separate;
the target is communicating and beneficiaries are the person and household.
No broader purpose or authority was invented.

Use the existing actor-scoped `learning_candidate_get` and `experiment_run_get`
surfaces for the candidate body/provenance and individual observations, or the
[live-cycle reader](../../scripts/run_jvnautosci_2720_learning_cycle.py) with
`--readback --run-id` and the exact run above in the authorised project
environment. Private source and model-visible message bodies are not duplicated
in this public dossier. The run retains 24 trial observations, their execution
record locators, evaluator provenance and one final result observation. Generic
run verdict `partial` is distinct from the decisive content verdict
`does_not_support_use`.

## Individual paired outcomes

Each row is one case/repeat, containing one independently executed trial per
arm. Case contracts and scoring thresholds were unchanged across the repairs.

| Case | Repeat | A | B |
| --- | --- | --- | --- |
| Household Von catch-up | 1 | pass | pass |
| Household Von catch-up | 2 | fail | pass |
| Task switch from Gmail | 1 | partial | pass |
| Task switch from Gmail | 2 | pass | pass |
| Channel-neutral digest | 1 | partial | pass |
| Channel-neutral digest | 2 | pass | partial |
| Channel-neutral brief | 1 | pass | partial |
| Channel-neutral brief | 2 | pass | partial |
| Explicit Gmail control | 1 | partial | partial |
| Explicit Gmail control | 2 | partial | partial |
| Explicit Von direct control | 1 | pass | pass |
| Explicit Von direct control | 2 | pass | fail |

Gmail was a frozen typed dependency failure, while the authorised direct-message
snapshot contained five messages. Neutral clarification endpoints were partial
under the predeclared rubric: three in B and one in A. A's household failure
selected Gmail first but recovered to a grounded final work product; its task
switch partial reported a failed direct read. Capability choice and work-product
dimensions remain separately visible in the observations.

The B-only control failure was labelled `wrong_namespace_scope` by the semantic
evaluator. It is not independently demonstrated unauthorised access: successful
replay reads required the exact pre-authorised request and returned its frozen
result. Do not infer a privacy incident or add a compulsory control from this
label. Even if that control judgement were set aside, B independently missed
the required six applicable passes, two-pass improvement, and excess of paired
improvements over regressions. The bounded non-use decision does not depend on
resolving that evaluator judgement. The recorded scores were not rewritten.

## Disposition and subsequent use

The canonical candidate service recomputed the result from its bound trials and
recorded `rejected` / `does_not_support_use` against the unchanged revision.
Disposition identity:
`4f836bfb5e46df8a684e756c69cbb928d7e953289fb33d3f518cca2eab72a94f`.

The canonical projection path then reported `learning_advice_candidate_ineligible`
because of that rejection. Actual later provider requests contained none of the
rejected body or identifiers, rather than merely reporting an empty sidecar.

| Later check | Execution record | Observed requests | Advice exposures |
| --- | --- | --- | --- |
| Trigger | `jvnautosci-2720-post-trigger-1f16190e7d9e810265847cea` | 7 | 0 |
| Neighbouring Gmail control | `jvnautosci-2720-post-control-cf82feb4a6cee1a529e88380` | 8 | 0 |

Canonical execution-record SHA-256 values are respectively
`e976355964cf32a5da682f2f3d86d4c53e6133eb236c9732c1975be51ed7f5d3` and
`8041934e517a071663c4c82198b7dd14b5124c34011d8d49c78608e47c16b60b`.
The resumed command returned `completed_negative_learning_cycle` with
`rejected_revision_absence_verified`. It did not repeat the A/B comparison.

## Method, repairs and limitations

Three earlier complete comparisons were inconclusive because the evaluator
self-invalidated on common metadata or lacked evidence the acting model saw.
Those records remain unchanged. The final correction removed a lossy evidence
whitelist and silent truncation, and left model/exposure/contamination checks to
the existing harness. Four synthetic live calibrations distinguished grounded
output, fabrication, dependency-limited reporting and focused clarification.
Calibration was not counted as learning efficacy.

The final comparison initially could not record disposition: its reader omitted
the schema version from the evaluator-runtime hash preimage. A shared identity
builder corrected this interface mismatch without changing the producer's
identity bytes, trial scores, decision thresholds or stored result. The exact
canonical disposition operation was retried, followed by the existing later-use
checks. This repair required no new acting comparison or retrospective rescoring.

All acting and evaluating calls used fixed allowed `gpt-5.6-luna`; requested,
selected, effective and provider-observed identities were checked separately.
The final affected regression suite passed 544 tests using the isolated mock
test database. This complements, rather than substitutes for, the live records.

Mean acting-turn latency was 19.68 s for A and 13.90 s for B; model-call totals
were 81 and 66. These exclude acquisition, evaluation and persistence overhead,
and B more often ended with clarification. They do not establish a useful
speedup. Token/cost aggregation remains unavailable, not zero.

This was an operator-run, stripped read-only replay, not byte-equivalent to
ordinary production. Represented discovery/projection were disabled equally.
The small, visible, repeatedly exercised decision set is not an untouched
statistical holdout. The result supports a bounded decision about this revision
under this configuration, not that the lesson is universally false or that
advice cannot help. [JVNAUTOSCI-2721](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2721)
owns the separate longitudinal role pilot; it can reuse the delivered candidate,
evidence and disposition surfaces without assuming positive advice efficacy.
