from __future__ import annotations

import json

import pytest

from src.backend.services import workflow_repo_seed_bootstrap
from src.backend.workflows import vontology_loader


def test_workflow_policy_point_read_has_no_elapsed_deadline(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def _get_texts(concept_id: str, **kwargs):
        calls.append((concept_id, dict(kwargs)))
        return [{"predicate": "#V#hasPolicy", "text": "{}"}]

    monkeypatch.setattr(vontology_loader, "get_texts_for_concept", _get_texts)
    cache: dict = {}

    rows = vontology_loader._get_cached_policy_text_rows(
        "#V#workflow_step",
        text_cache=cache,
    )

    assert rows == [{"predicate": "#V#hasPolicy", "text": "{}"}]
    assert calls == [("#V#workflow_step", {})]
    assert cache["#V#workflow_step"] == rows


def test_workflow_policy_read_failure_is_not_cached_as_empty(monkeypatch) -> None:
    def _fail(_concept_id: str, **_kwargs):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(vontology_loader, "get_texts_for_concept", _fail)
    cache: dict = {}

    with pytest.raises(RuntimeError, match="policy store unavailable"):
        vontology_loader._get_cached_policy_text_rows(
            "#V#workflow_step",
            text_cache=cache,
        )

    assert "#V#workflow_step" not in cache


def test_workflow_policy_prefetch_has_no_elapsed_deadline(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def _get_texts(concept_ids, **kwargs):
        calls.append((list(concept_ids), dict(kwargs)))
        return {concept_id: [] for concept_id in concept_ids}

    monkeypatch.setattr(vontology_loader, "get_texts_for_concepts", _get_texts)
    cache: dict = {}

    warning = vontology_loader._prefetch_policy_text_rows(
        ["#V#step_a", "#V#step_b"],
        text_cache=cache,
    )

    assert warning is None
    assert calls == [
        (
            ["#V#step_a", "#V#step_b"],
            {"limit_per_concept": 50},
        )
    ]
    assert cache == {"#V#step_a": [], "#V#step_b": []}


def test_repo_seed_marker_read_has_no_elapsed_deadline(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    payload = {
        "schema_version": "workflow_repo_seed_version.v1",
        "seed_version": "9",
        "authority_payload_sha256": "abc123",
    }

    def _get_texts(concept_id: str, **kwargs):
        calls.append((concept_id, dict(kwargs)))
        return [
            {
                "predicate": "#V#hasWorkflowRepoSeedVersionJson",
                "text": json.dumps(payload),
                "relation_id": "relation-1",
                "text_value_id": "text-1",
            }
        ]

    monkeypatch.setattr(
        workflow_repo_seed_bootstrap,
        "get_texts_for_concept",
        _get_texts,
    )

    marker = workflow_repo_seed_bootstrap._load_workflow_repo_seed_version_marker(
        "#V#workflow"
    )

    assert marker is not None
    assert marker["seed_version"] == "9"
    assert calls == [
        (
            "#V#workflow",
            {
                "predicate": "#V#hasWorkflowRepoSeedVersionJson",
                "limit": 5,
            },
        )
    ]
