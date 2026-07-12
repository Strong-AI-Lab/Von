"""Vontology-backed benchmark suite and rubric definitions.

Benchmark runners should compute metrics, join telemetry, and serialise reports.
The durable suite/case/rubric authority belongs in Vontology so evaluation
policy can be inspected, revised, and linked to workflow/prompt artefacts.
Repo seed bundles are accepted only as import fixtures for initially
materialising canonical suites.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation

BENCHMARK_SUITE_DEFINITION_SCHEMA_VERSION = "benchmark_suite_definition.v1"
BENCHMARK_SUITE_TYPE_ID = "#V#benchmark_suite"
HAS_BENCHMARK_SUITE_DEFINITION_JSON = "#V#has_benchmark_suite_definition_json"

SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID = (
    "#V#selector_routing_benchmark_suite"
)
CONTEXT_BUNDLE_BENCHMARK_SUITE_CONCEPT_ID = (
    "#V#context_bundle_benchmark_suite"
)
CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID = (
    "#V#context_grounded_answering_benchmark_suite"
)
OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID = (
    "#V#operational_certification_benchmark_suite"
)

_SUITE_DEFINITION_TEXT_PREDICATES: tuple[str, ...] = (
    HAS_BENCHMARK_SUITE_DEFINITION_JSON,
)

_REPO_SEED_BUNDLE_DIR = (
    Path(__file__).resolve().parent.parent / "workflows" / "repo_seed_bundles"
)

_CANONICAL_SUITE_FIXTURES: tuple[dict[str, Any], ...] = (
    {
        "suite_concept_id": SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
        "fixture_path": _REPO_SEED_BUNDLE_DIR
        / "selector_routing_benchmark_seed_bundle.json",
    },
    {
        "suite_concept_id": CONTEXT_BUNDLE_BENCHMARK_SUITE_CONCEPT_ID,
        "fixture_path": _REPO_SEED_BUNDLE_DIR
        / "context_bundle_benchmark_seed_bundle.json",
    },
    {
        "suite_concept_id": CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID,
        "fixture_path": _REPO_SEED_BUNDLE_DIR
        / "context_grounded_answering_benchmark_seed_bundle.json",
    },
    {
        "suite_concept_id": OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
        "fixture_path": _REPO_SEED_BUNDLE_DIR
        / "operational_certification_benchmark_seed_bundle.json",
    },
)


class BenchmarkSuiteAuthorityMissingError(RuntimeError):
    """Raised when a benchmark runner lacks represented suite authority."""

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any]):
        super().__init__(message)
        self.diagnostics = dict(diagnostics)


def _safe_str(value: Any, *, limit: int | None = None) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    if limit is not None:
        return text[:limit]
    return text


def _hash_payload(value: Any, *, length: int | None = None) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:length] if length is not None else digest


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
    return tuple(output)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _fixture_display_name(definition: Mapping[str, Any]) -> str:
    title = _safe_str(definition.get("suite_title")) or _safe_str(
        definition.get("suite_id")
    )
    if title:
        return title
    return "Benchmark suite"


def canonical_benchmark_suite_fixture_specs() -> tuple[dict[str, Any], ...]:
    return tuple(dict(item) for item in _CANONICAL_SUITE_FIXTURES)


def canonical_benchmark_suite_concept_ids() -> tuple[str, ...]:
    return tuple(str(item["suite_concept_id"]) for item in _CANONICAL_SUITE_FIXTURES)


def _normalise_suite_definition(
    raw: Mapping[str, Any],
    *,
    suite_concept_id: str,
    source: str,
    source_path: str | None = None,
) -> dict[str, Any]:
    case_sets = raw.get("case_sets")
    if not isinstance(case_sets, Mapping):
        raise ValueError("benchmark_suite_case_sets_missing")
    rubric = raw.get("rubric")
    if not isinstance(rubric, Mapping):
        raise ValueError("benchmark_suite_rubric_missing")

    normalised_case_sets: dict[str, list[dict[str, Any]]] = {}
    for raw_case_set, raw_cases in case_sets.items():
        case_set_id = _safe_str(raw_case_set)
        if not case_set_id:
            continue
        if not isinstance(raw_cases, Sequence) or isinstance(
            raw_cases,
            (str, bytes, bytearray),
        ):
            raise ValueError(f"benchmark_suite_case_set_invalid:{case_set_id}")
        normalised_case_sets[case_set_id] = [
            dict(item) for item in raw_cases if isinstance(item, Mapping)
        ]

    default_case_set = _safe_str(raw.get("default_case_set"))
    if not default_case_set and normalised_case_sets:
        default_case_set = next(iter(normalised_case_sets))
    if default_case_set not in normalised_case_sets:
        raise ValueError("benchmark_suite_default_case_set_missing")

    suite_id = _safe_str(raw.get("suite_id")) or suite_concept_id
    definition = {
        "definition_schema_version": _safe_str(
            raw.get("definition_schema_version")
        )
        or BENCHMARK_SUITE_DEFINITION_SCHEMA_VERSION,
        "schema_version": _safe_str(raw.get("schema_version")),
        "seed_schema_version": _safe_str(raw.get("seed_schema_version"))
        or _safe_str(raw.get("schema_version")),
        "suite_id": suite_id,
        "suite_concept_id": suite_concept_id,
        "suite_title": _safe_str(raw.get("suite_title")) or suite_id,
        "suite_description": _safe_str(raw.get("suite_description"), limit=4000),
        "default_case_set": default_case_set,
        "case_sets": normalised_case_sets,
        "rubric": dict(rubric),
        "source": source,
    }
    if source_path:
        definition["source_path"] = source_path
    return definition


def load_benchmark_suite_definition_from_seed_fixture(
    fixture_path: Path | str,
    *,
    suite_concept_id: str | None = None,
) -> dict[str, Any]:
    resolved_path = Path(fixture_path)
    text = resolved_path.read_text(encoding="utf-8")
    raw = json.loads(text)
    if not isinstance(raw, Mapping):
        raise ValueError("benchmark_suite_fixture_invalid")
    resolved_suite_concept_id = _safe_str(suite_concept_id) or _safe_str(
        raw.get("suite_concept_id")
    )
    if not resolved_suite_concept_id:
        raise ValueError("benchmark_suite_concept_id_missing")
    definition = _normalise_suite_definition(
        raw,
        suite_concept_id=resolved_suite_concept_id,
        source="seed_bundle_import_fixture",
        source_path=str(resolved_path),
    )
    definition["fixture_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return definition


def load_benchmark_suite_definition(
    suite_concept_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_suite_concept_id = _safe_str(suite_concept_id)
    diagnostics: dict[str, Any] = {
        "requested_suite_concept_id": resolved_suite_concept_id,
        "loaded_suite_concept_id": None,
        "source_predicate": None,
        "missing_suite_concept_ids": [],
        "malformed_suite_concept_ids": [],
    }
    if not resolved_suite_concept_id:
        diagnostics["missing_suite_concept_ids"] = [None]
        raise BenchmarkSuiteAuthorityMissingError(
            "benchmark_suite_concept_id_missing",
            diagnostics=diagnostics,
        )

    concept_doc = _safe_get_concept(resolved_suite_concept_id)
    if not isinstance(concept_doc, Mapping):
        diagnostics["missing_suite_concept_ids"] = [resolved_suite_concept_id]
        raise BenchmarkSuiteAuthorityMissingError(
            "benchmark_suite_concept_missing",
            diagnostics=diagnostics,
        )

    predicate = HAS_BENCHMARK_SUITE_DEFINITION_JSON
    texts = get_texts_for_concept(
        resolved_suite_concept_id,
        predicate=predicate,
        limit=10,
    )
    non_empty_texts = [
        _safe_str((row or {}).get("text"))
        for row in texts
        if _safe_str((row or {}).get("text"))
    ]
    if len(non_empty_texts) > 1:
        diagnostics["ambiguous_definition_count"] = len(non_empty_texts)
        raise BenchmarkSuiteAuthorityMissingError(
            "benchmark_suite_definition_ambiguous",
            diagnostics=diagnostics,
        )
    if non_empty_texts:
        try:
            raw = json.loads(non_empty_texts[0])
            if not isinstance(raw, Mapping):
                raise ValueError("benchmark_suite_definition_not_mapping")
            definition = _normalise_suite_definition(
                raw,
                suite_concept_id=resolved_suite_concept_id,
                source="vontology",
            )
        except Exception as exc:
            diagnostics["malformed_suite_concept_ids"].append(
                resolved_suite_concept_id
            )
            diagnostics["malformed_definition_error"] = type(exc).__name__
            raise BenchmarkSuiteAuthorityMissingError(
                "benchmark_suite_definition_malformed",
                diagnostics=diagnostics,
            ) from exc
        diagnostics["loaded_suite_concept_id"] = resolved_suite_concept_id
        diagnostics["source_predicate"] = predicate
        diagnostics["definition_sha256"] = _hash_payload(definition)
        return definition, diagnostics

    diagnostics["missing_suite_concept_ids"] = [resolved_suite_concept_id]
    raise BenchmarkSuiteAuthorityMissingError(
        "benchmark_suite_definition_missing",
        diagnostics=diagnostics,
    )


def load_benchmark_suite_case_set(
    *,
    suite_concept_id: str,
    case_set: str | None = None,
    fixture_path: Path | str | None = None,
) -> dict[str, Any]:
    if fixture_path is not None:
        definition = load_benchmark_suite_definition_from_seed_fixture(
            fixture_path,
            suite_concept_id=suite_concept_id,
        )
        diagnostics: dict[str, Any] = {
            "loaded_suite_concept_id": definition.get("suite_concept_id"),
            "source_predicate": None,
            "definition_sha256": _hash_payload(definition),
            "source": "seed_bundle_import_fixture",
        }
    else:
        definition, diagnostics = load_benchmark_suite_definition(suite_concept_id)

    requested_case_set = _safe_str(case_set) or _safe_str(
        definition.get("default_case_set")
    )
    case_sets = definition.get("case_sets")
    raw_cases = case_sets.get(requested_case_set) if isinstance(case_sets, Mapping) else None
    if raw_cases is None:
        raise ValueError("benchmark_suite_case_set_not_found")
    if not isinstance(raw_cases, Sequence) or isinstance(
        raw_cases,
        (str, bytes, bytearray),
    ):
        raise ValueError("benchmark_suite_case_set_invalid")

    return {
        "cases": [dict(item) for item in raw_cases if isinstance(item, Mapping)],
        "case_set": requested_case_set,
        "rubric": dict(definition.get("rubric") or {}),
        "suite_id": definition.get("suite_id"),
        "suite_concept_id": definition.get("suite_concept_id"),
        "suite_title": definition.get("suite_title"),
        "suite_description": definition.get("suite_description"),
        "suite_schema_version": definition.get("schema_version"),
        "definition_schema_version": definition.get("definition_schema_version"),
        "seed_schema_version": definition.get("seed_schema_version"),
        "source": definition.get("source"),
        "source_path": definition.get("source_path"),
        "fixture_sha256": definition.get("fixture_sha256"),
        "definition_sha256": diagnostics.get("definition_sha256")
        or _hash_payload(definition),
        "source_predicate": diagnostics.get("source_predicate"),
        "authority_diagnostics": diagnostics,
    }


def load_operational_certification_contract(
    *,
    suite_concept_id: str = OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
    case_set: str | None = None,
    fixture_path: Path | str | None = None,
) -> Any:
    """Load and validate the represented operational-certification contract.

    ``fixture_path`` is intentionally explicit so production callers fail
    closed on missing live authority while import/migration tests can opt into
    the repository fixture.
    """

    from .operational_certification_contract_service import (
        parse_operational_certification_contract,
    )

    payload = load_benchmark_suite_case_set(
        suite_concept_id=suite_concept_id,
        case_set=case_set,
        fixture_path=fixture_path,
    )
    return parse_operational_certification_contract(payload)


def _existing_suite_has_definition(suite_concept_id: str) -> bool:
    for predicate in _SUITE_DEFINITION_TEXT_PREDICATES:
        texts = get_texts_for_concept(suite_concept_id, predicate=predicate, limit=1)
        if texts:
            return True
    return False


def ensure_canonical_benchmark_suites_from_seed_fixtures(
    *,
    suite_concept_ids: Sequence[str] | None = None,
    definition_predicate: str = HAS_BENCHMARK_SUITE_DEFINITION_JSON,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    create_missing_concepts: bool = True,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    requested_ids = _normalise_strings(suite_concept_ids)
    fixture_specs = [
        dict(spec)
        for spec in _CANONICAL_SUITE_FIXTURES
        if not requested_ids or str(spec["suite_concept_id"]) in requested_ids
    ]
    provenance_payload = dict(provenance) if isinstance(provenance, Mapping) else None
    context_payload = dict(context) if isinstance(context, Mapping) else None

    type_created = False
    created_suite_concept_ids: list[str] = []
    persisted_suite_concept_ids: list[str] = []
    skipped_existing_suite_concept_ids: list[str] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    suite_type = _safe_get_concept(BENCHMARK_SUITE_TYPE_ID)
    if not isinstance(suite_type, Mapping) and create_missing_concepts:
        try:
            concept_service.create_concept(
                name="Benchmark suite",
                concept_id=BENCHMARK_SUITE_TYPE_ID,
                description=(
                    "Type for represented benchmark suites whose cases, rubrics, "
                    "coverage categories, and signal definitions are Vontology "
                    "authority rather than Python service constants."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
            )
            type_created = True
        except Exception as exc:
            errors_by_concept_id[BENCHMARK_SUITE_TYPE_ID] = f"type_create_failed:{exc}"

    for spec in fixture_specs:
        concept_id = str(spec["suite_concept_id"])
        fixture_path = Path(spec["fixture_path"])
        try:
            definition = load_benchmark_suite_definition_from_seed_fixture(
                fixture_path,
                suite_concept_id=concept_id,
            )
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"fixture_load_failed:{exc}"
            continue

        concept_doc = _safe_get_concept(concept_id)
        if not isinstance(concept_doc, Mapping):
            if not create_missing_concepts:
                missing_concept_ids.append(concept_id)
                continue
            try:
                concept_service.create_concept(
                    name=_fixture_display_name(definition),
                    concept_id=concept_id,
                    description=_safe_str(definition.get("suite_description")),
                    parent_concept_ids=[BENCHMARK_SUITE_TYPE_ID],
                    create_as_instance=True,
                )
                created_suite_concept_ids.append(concept_id)
            except Exception as exc:
                errors_by_concept_id[concept_id] = f"create_failed:{exc}"
                continue

        if not overwrite_existing and _existing_suite_has_definition(concept_id):
            skipped_existing_suite_concept_ids.append(concept_id)
            continue

        definition_json = json.dumps(
            definition,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=definition_predicate,
                lang=language,
                text=definition_json,
                policy=policy,
                provenance=provenance_payload,
                context=context_payload,
                garbage_collect=True,
            )
            persisted_suite_concept_ids.append(concept_id)
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"definition_upsert_failed:{exc}"

    return {
        "success": not (missing_concept_ids or errors_by_concept_id),
        "suite_type_id": BENCHMARK_SUITE_TYPE_ID,
        "suite_type_created": type_created,
        "requested_suite_concept_ids": [
            str(spec["suite_concept_id"]) for spec in fixture_specs
        ],
        "created_suite_concept_ids": created_suite_concept_ids,
        "persisted_suite_concept_ids": persisted_suite_concept_ids,
        "skipped_existing_suite_concept_ids": skipped_existing_suite_concept_ids,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
        "definition_predicate": definition_predicate,
        "counts": {
            "requested": len(fixture_specs),
            "created_concepts": len(created_suite_concept_ids),
            "persisted_definitions": len(persisted_suite_concept_ids),
            "skipped_existing": len(skipped_existing_suite_concept_ids),
            "missing_concepts": len(missing_concept_ids),
            "errors": len(errors_by_concept_id),
        },
    }


__all__ = [
    "BENCHMARK_SUITE_DEFINITION_SCHEMA_VERSION",
    "BENCHMARK_SUITE_TYPE_ID",
    "CONTEXT_BUNDLE_BENCHMARK_SUITE_CONCEPT_ID",
    "CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID",
    "HAS_BENCHMARK_SUITE_DEFINITION_JSON",
    "OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID",
    "SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID",
    "BenchmarkSuiteAuthorityMissingError",
    "canonical_benchmark_suite_concept_ids",
    "canonical_benchmark_suite_fixture_specs",
    "ensure_canonical_benchmark_suites_from_seed_fixtures",
    "load_benchmark_suite_case_set",
    "load_benchmark_suite_definition",
    "load_benchmark_suite_definition_from_seed_fixture",
    "load_operational_certification_contract",
]
