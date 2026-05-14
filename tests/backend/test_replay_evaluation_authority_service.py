from __future__ import annotations

import json

import pytest

from src.backend.services import replay_evaluation_authority_service as authority


def _rubric_payload() -> dict[str, object]:
    return {
        "schema_version": authority.REPLAY_EVALUATION_RUBRIC_SCHEMA_VERSION,
        "rubric_id": "live_prompt_sampler_replay_evaluation",
        "rubric_version": "test",
        "expected_result_schema_version": (
            authority.REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION
        ),
        "verdict_values": ["pass", "partial", "fail", "inconclusive"],
    }


def test_resolve_replay_evaluation_rubric_loads_json_from_vontology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_get_texts_for_concept(**kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"text": json.dumps(_rubric_payload())}]

    monkeypatch.setattr(authority, "get_texts_for_concept", fake_get_texts_for_concept)

    rubric = authority.resolve_replay_evaluation_rubric(
        concept_id="#V#rubric",
        predicate="#V#has_rubric_json",
    )

    assert captured["subject_concept_id"] == "#V#rubric"
    assert captured["predicate"] == "#V#has_rubric_json"
    assert rubric.concept_id == "#V#rubric"
    assert rubric.rubric_version == "test"
    assert rubric.verdict_values == ("pass", "partial", "fail", "inconclusive")


def test_resolve_replay_evaluation_rubric_fails_closed_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority, "get_texts_for_concept", lambda **_: [])

    with pytest.raises(authority.ReplayEvaluationAuthorityUnavailable):
        authority.resolve_replay_evaluation_rubric(concept_id="#V#missing")


def test_represented_evaluation_result_must_match_rubric() -> None:
    rubric = authority.ReplayEvaluationRubric(
        concept_id="#V#rubric",
        source_predicate="#V#has_rubric_json",
        raw_rubric=_rubric_payload(),
        diagnostics={},
    )

    with pytest.raises(authority.ReplayEvaluationAuthorityUnavailable):
        authority.normalise_represented_replay_evaluation_result(
            {
                "schema_version": (
                    authority.REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION
                ),
                "verdict": "blocked",
                "rubric_concept_id": "#V#rubric",
            },
            rubric=rubric,
        )
