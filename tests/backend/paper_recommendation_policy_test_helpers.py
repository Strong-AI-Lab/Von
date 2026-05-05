from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from src.backend.services.paper_recommendation_constants import (
    PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
)
from src.backend.services.paper_recommendation_policy_authority_service import (
    PaperRecommendationPolicy,
)

_POLICY_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "paper_recommendation_policy_profile_seed.json"
)


def make_test_paper_recommendation_policy(
    overrides: Mapping[str, Any] | None = None,
) -> PaperRecommendationPolicy:
    with _POLICY_SEED_PATH.open("r", encoding="utf-8") as handle:
        raw_policy = json.load(handle)
    payload = deepcopy(raw_policy)
    if isinstance(overrides, Mapping):
        _deep_update(payload, overrides)
    return PaperRecommendationPolicy(
        concept_id=PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
        source_predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
        raw_policy=payload,
        diagnostics={
            "policy_concept_id": PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
            "policy_predicate_id": PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
            "policy_version": payload.get("policy_version"),
        },
    )


def patch_paper_recommendation_policy(monkeypatch, module, policy=None):
    active_policy = policy or make_test_paper_recommendation_policy()
    monkeypatch.setattr(
        module,
        "resolve_paper_recommendation_policy",
        lambda **_kwargs: active_policy,
    )
    return active_policy


def _deep_update(target: dict[str, Any], updates: Mapping[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)
