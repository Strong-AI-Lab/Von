from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import benchmark_suite_vontology_service as service
from src.backend.services.concept_service import ConceptNotFoundError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_DIR = _REPO_ROOT / "src" / "backend" / "workflows" / "repo_seed_bundles"


def _selector_fixture_path() -> Path:
    return _SEED_DIR / "selector_routing_benchmark_seed_bundle.json"


def test_load_benchmark_suite_case_set_uses_represented_vontology_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = service.load_benchmark_suite_definition_from_seed_fixture(
        _selector_fixture_path(),
        suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    )
    stored_definition = dict(definition)
    stored_definition.pop("source_path", None)
    stored_definition.pop("fixture_sha256", None)

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, *, predicate, limit=10: (
            [{"text": json.dumps(stored_definition)}]
            if predicate == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
            else []
        ),
    )

    result = service.load_benchmark_suite_case_set(
        suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    )

    assert result["source"] == "vontology"
    assert result["suite_concept_id"] == service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    assert result["case_set"] == "phase1_seed"
    assert result["source_predicate"] == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
    assert result["rubric"]["rubric_id"] == "selector_routing_rubric.v1"
    assert len(result["cases"]) >= 5


def test_load_benchmark_suite_case_set_fails_closed_when_suite_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_concept_id: str) -> dict[str, Any]:
        raise ConceptNotFoundError("missing")

    monkeypatch.setattr(service, "get_concept_by_concept_id", _missing)

    with pytest.raises(service.BenchmarkSuiteAuthorityMissingError) as exc_info:
        service.load_benchmark_suite_case_set(
            suite_concept_id=service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
        )

    assert str(exc_info.value) == "benchmark_suite_concept_missing"
    assert exc_info.value.diagnostics["missing_suite_concept_ids"] == [
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    ]


def test_ensure_canonical_benchmark_suites_imports_without_overwriting_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: set[str] = set()
    persisted: list[dict[str, Any]] = []

    def _get_concept(concept_id: str) -> dict[str, Any]:
        if concept_id in created:
            return {"concept_id": concept_id}
        raise ConceptNotFoundError("missing")

    def _create_concept(**kwargs: Any) -> dict[str, Any]:
        created.add(str(kwargs["concept_id"]))
        return {"concept_id": kwargs["concept_id"]}

    def _get_texts_for_concept(
        _concept_id: str,
        *,
        predicate: str,
        limit: int = 1,
    ) -> list[dict[str, str]]:
        return []

    def _upsert_singleton_text_relation(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        return {"success": True}

    monkeypatch.setattr(service, "get_concept_by_concept_id", _get_concept)
    monkeypatch.setattr(service.concept_service, "create_concept", _create_concept)
    monkeypatch.setattr(service, "get_texts_for_concept", _get_texts_for_concept)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _upsert_singleton_text_relation,
    )

    report = service.ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID],
    )

    assert report["success"] is True
    assert service.BENCHMARK_SUITE_TYPE_ID in created
    assert service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID in created
    assert report["persisted_suite_concept_ids"] == [
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    ]
    assert persisted[0]["predicate"] == service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
    stored_payload = json.loads(persisted[0]["text"])
    assert stored_payload["suite_concept_id"] == (
        service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
    )
