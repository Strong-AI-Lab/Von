> **Merge impact — complete before implementation detail:** If this PR merged
> now, what concrete user or operational outcome would become true?
>
> Replace this line with one ordinary sentence: `Merge decision: ready — ...`
> or `Merge decision: not ready — ...`. Review it as prose; do not automate it
> as a merge gate.
>
> A remaining observation blocks merge only when evidence shows that it defeats
> a minimum ship criterion or satisfies a stated stop-ship condition.

## User outcome

What user or operational job improves, and why is the change needed?

## Material changes

Describe only the affected code, represented artefacts, data, or guidance.
Link the Jira issue or decision record when one governs the work.

If this adds a compulsory semantic gate or restriction, identify the specific,
credible, materially unacceptable outcome; the evidence or causal demonstration
that it is reachable here; and why the least restrictive bounded, observable,
and recoverable approach is inadequate. Scope the control to that failure mode.

## Evidence

What was actually run or inspected, and what scope does that evidence support?
Include the nearest faithful user path when the claim is user-visible.

## Ship boundary

- **Minimum ship criteria:** Smallest outcome and evidence sufficient to merge.
- **Stop-ship conditions:** Concrete material conditions that would defeat
  those criteria.
- **Non-blocking observations:** Known limitations, measurements, or questions
  that do not defeat the criteria.
- **Non-goals:** Nearby work deliberately outside this PR.

Classify any human gate, experiment, activation step, unresolved uncertainty,
or follow-up under its actual effect on this boundary. Do not invent entries or
mark inapplicable subsystems merely to complete the template.
