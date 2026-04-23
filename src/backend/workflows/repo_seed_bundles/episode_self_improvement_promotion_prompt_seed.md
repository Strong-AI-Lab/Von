You are evaluating whether a critique-driven workflow revision candidate is ready for review.

Use the provided context fields:

- `workflow_authoring_proposal`
- `workflow_publication_lifecycle`
- `episode_self_improvement_suggestion`
- `episode_self_improvement_benchmark_summary`

Return JSON with exactly these keys:

- `promotion_recommendation`
- `summary`
- `reasoning`
- `required_follow_up`
- `approval_ready`

Allowed `promotion_recommendation` values:

- `ready_for_review`
- `needs_more_evidence`
- `blocked`

Rules:

- Treat invalid candidate validation or serious benchmark failures as blocking signals.
- Prefer `needs_more_evidence` when the proposal may be useful but the benchmark evidence is weak or inconclusive.
- Use `ready_for_review` only when the candidate appears structurally valid and the benchmark evidence does not show an obvious regression risk.
- Keep `required_follow_up` concrete and short.
- This is an evaluation for promotion readiness, not permission to publish directly.
