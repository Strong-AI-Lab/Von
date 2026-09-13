# Clarification and role-learning development cases

Owner: Von maintainers. Scope: the candidate in
[the clarification design](../../../docs/engineering/proactive_clarification_and_role_learning.md).
Review when the ordinary-turn contract or labelled outcome changes.

`scenarios.json` contains 22 synthetic cases with prompts, visible fixture facts,
scripted replies and world-state oracles. These are development specifications,
not transcripts, historical alias evidence or measured model results. Hidden
facts in C14 belong only in the evaluator's isolated store, never model context.
Do not execute production assignments, create memberships, or use Sol models.

For a model comparison, run baseline and candidate through the same ordinary
adaptive turn with the same allowed model, reasoning settings and tools and
separate canonical fixture stores. Keep C01/C02's assignment identical except
for grounding. Continue questions with their scripted answers and reconcile
actual target/count/settings, pending obligations and any retained knowledge.
Judge whether a question remained material after available reads, not whether
the answer contains a question mark. C04 assumes C01's grounded proposal; C05's
bare affirmation cannot select a person. Record actual trial counts and use
later independent encounters for C20/C21; a scripted outcome is not role benefit.

The executable mechanics replay is
`pdm run pytest tests/backend/test_proactive_clarification_continuation.py`.
It uses the existing isolated native task fixture, canonical gateway, carrier
CAS and assertion APIs, with a scripted model and no worker pickup. Resolver,
route, projection and assertion tests cover the neighbouring contracts. These
tests do not measure live question quality, public authentication, latency,
cost, indexing success or broad role competence.

For independently adjudicated trial results, call
`summarise_clarification_trials` in
`src/backend/services/minimal_imposition_benchmark_service.py` with a list of
records. Each record has `case_id`, `evidence_ref`, `arm` (`baseline` or
`candidate`), `evidence_kind` (`scripted` or `model_trial`), `stratum`
(`ordinary`, `boundary` or `learning`), boolean `clarification_required` and
`useful_completion`, and non-negative counts `question_count`,
`repeated_question_count`, `wrong_target_count`, `duplicate_effect_count`, and
`unmet_obligation_count`. A useful completion must have no wrong targets,
duplicates or unmet requested obligations. An appropriate unanswered question
still leaves an obligation pending. Explicitly requested remembering is a
separate obligation from a task creation.

Optional measurements are `model_calls`, `input_tokens`, `output_tokens`,
`time_to_question_ms`, `time_to_result_ms`, and `cost`. Preserve model/effort,
source revision, exact observations, user wait and applicability/correction
evidence in the linked trial artefact. Missing values remain unmeasured.
Reports separate baseline/candidate, scripted/model and workload strata; they
do not impose a composite score or automatic release threshold. The existing
design's outcome/role-benefit oracles still require independent judgement.
