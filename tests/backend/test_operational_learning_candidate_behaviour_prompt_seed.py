from pathlib import Path


_PROMPT_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "operational_learning_candidate_behaviour_evaluator_prompt_seed.md"
)


def test_candidate_behaviour_prompt_requires_replayable_source_content() -> None:
    prompt = _PROMPT_SEED_PATH.read_text(encoding="utf-8")

    assert "Evidence sufficiency is content-based" in prompt
    assert "prove binding or provenance only" in prompt
    assert "Candidate-authored stimuli and expected outcomes are proposals" in prompt
    assert "`failure_evidence_material_availability`" in prompt
    assert "`coverage: digest_locators_only`" in prompt
    assert "only hashes or IDs plus candidate-authored vector text" in prompt
    assert "the original-failure case is `blocked`" in prompt
