from __future__ import annotations

from pathlib import Path
from typing import Any

from src.backend.services import benchmark_suite_vontology_service as suite_service

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_DIR = _REPO_ROOT / "src" / "backend" / "workflows" / "repo_seed_bundles"

_FIXTURE_BY_SUITE_ID = {
    suite_service.SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID: (
        _SEED_DIR / "selector_routing_benchmark_seed_bundle.json"
    ),
    suite_service.CONTEXT_BUNDLE_BENCHMARK_SUITE_CONCEPT_ID: (
        _SEED_DIR / "context_bundle_benchmark_seed_bundle.json"
    ),
    suite_service.CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID: (
        _SEED_DIR / "context_grounded_answering_benchmark_seed_bundle.json"
    ),
}


def represented_suite_case_set_loader(
    *,
    suite_concept_id: str,
    case_set: str | None = None,
    fixture_path: Path | str | None = None,
) -> dict[str, Any]:
    if fixture_path is not None:
        return suite_service.load_benchmark_suite_case_set(
            suite_concept_id=suite_concept_id,
            case_set=case_set,
            fixture_path=fixture_path,
        )

    fixture = _FIXTURE_BY_SUITE_ID[suite_concept_id]
    result = suite_service.load_benchmark_suite_case_set(
        suite_concept_id=suite_concept_id,
        case_set=case_set,
        fixture_path=fixture,
    )
    result["source"] = "vontology"
    result["source_path"] = None
    result["fixture_sha256"] = None
    result["source_predicate"] = suite_service.HAS_BENCHMARK_SUITE_DEFINITION_JSON
    result["authority_diagnostics"] = {
        "requested_suite_concept_id": suite_concept_id,
        "loaded_suite_concept_id": suite_concept_id,
        "source_predicate": suite_service.HAS_BENCHMARK_SUITE_DEFINITION_JSON,
        "missing_suite_concept_ids": [],
        "malformed_suite_concept_ids": [],
    }
    return result
