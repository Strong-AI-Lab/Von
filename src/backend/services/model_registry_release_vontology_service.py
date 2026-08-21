"""Governed publication for versioned model-registry release inputs.

The repo bundle is release material, not live authority. Publication requires
an actor-bound governed-method invoker supplied by an administration surface;
this module never binds actor identity, writes a database directly, or runs at
startup.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODEL_REGISTRY_RELEASE_BUNDLE_SCHEMA_VERSION = "model_registry_release_bundle.v1"
DEFAULT_GEMINI_3_7_FLASH_RELEASE_PATH = (
    Path(__file__).resolve().parents[1]
    / "vontology"
    / "seed_bundles"
    / "model_registry_gemini_3_7_flash_release.json.in"
)

GovernedMethodInvoker = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
ConceptExistsReader = Callable[[str], bool]
RegistrySnapshotReader = Callable[[], Mapping[str, Any]]

_CREATE_KINDS = {"individual": "instance", "predicate": "predicate", "type": "type"}
_EXPECTED_FIELDS = (
    "provider",
    "model_id",
    "registry_entry_id",
    "model_concept_id",
    "api_profile_id",
    "api_surface",
    "structured_tool_calling",
    "tool_continuation_mode",
    "response_storage_policy",
    "connection_id",
    "pricing_version",
    "capabilities_version",
)
_PROFILE_PREDICATES = {
    "api_surface": "#V#has_api_surface",
    "structured_tool_calling": "#V#has_structured_tool_calling_support",
    "tool_continuation_mode": "#V#has_tool_continuation_mode",
    "response_storage_policy": "#V#has_response_storage_policy",
    "connection_id": "#V#has_provider_connection_id",
}


class ModelRegistryReleaseError(RuntimeError):
    """Typed failure at the release-input or publication boundary."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "model_registry_release_error.v1",
            "error_code": self.code,
            "details": copy.deepcopy(self.details),
        }


def _fail(code: str, **details: Any) -> None:
    raise ModelRegistryReleaseError(code, details=details)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _concept_id(value: Any) -> str:
    resolved = _text(value)
    if not resolved.startswith("#V#") or len(resolved) == 3:
        _fail("model_registry_release_concept_id_invalid", concept_id=resolved)
    return resolved


def _items(bundle: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    values = bundle.get(field)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        _fail("model_registry_release_section_invalid", field=field)
    if not all(isinstance(item, Mapping) for item in values):
        _fail("model_registry_release_section_item_invalid", field=field)
    return [dict(item) for item in values]


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _utc_datetime(value: Any) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _normalise_text_relation(spec: Mapping[str, Any]) -> dict[str, Any]:
    relation = dict(spec)
    relation["subject_concept_id"] = _concept_id(relation.get("subject_concept_id"))
    predicate = _text(relation.get("predicate"))
    relation["resolved_predicate"] = _concept_id(
        "#V#hasName" if predicate == "hasName" else predicate
    )
    mode = _text(relation.get("mode"))
    if mode not in {"additive", "singleton"}:
        _fail("model_registry_release_text_mode_invalid", mode=mode)
    has_text = bool(_text(relation.get("text")))
    has_json = isinstance(relation.get("json"), Mapping)
    if has_text == has_json:
        _fail("model_registry_release_text_value_invalid")
    relation["resolved_text"] = (
        _text(relation.get("text")) if has_text else _json_text(relation["json"])
    )
    relation["lang"] = _text(relation.get("lang")) or "en-NZ"
    return relation


def validate_model_registry_release_bundle(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact dependency closure and the read-back contract."""

    if not isinstance(payload, Mapping):
        _fail("model_registry_release_not_mapping")
    bundle = copy.deepcopy(dict(payload))
    if bundle.get("schema_version") != MODEL_REGISTRY_RELEASE_BUNDLE_SCHEMA_VERSION:
        _fail("model_registry_release_schema_unsupported")
    if not _text(bundle.get("release_version")):
        _fail("model_registry_release_version_required")
    if bundle.get("authority_role") != "repo_release_input_only":
        _fail("model_registry_release_authority_role_invalid")

    required_values = bundle.get("required_existing_concept_ids")
    if not isinstance(required_values, Sequence) or isinstance(required_values, str):
        _fail("model_registry_release_dependencies_required")
    required = {_concept_id(item) for item in required_values}
    concepts = _items(bundle, "concepts")
    created: dict[str, dict[str, Any]] = {}
    for concept in concepts:
        concept_id = _concept_id(concept.get("concept_id"))
        kind = _text(concept.get("kind"))
        if concept_id in required or concept_id in created or kind not in _CREATE_KINDS:
            _fail("model_registry_release_concept_invalid", concept_id=concept_id)
        if not _text(concept.get("name")):
            _fail("model_registry_release_concept_name_required", concept_id=concept_id)
        created[concept_id] = concept
    known = required | set(created)
    for concept_id, concept in created.items():
        parent = (
            "#V#predicate"
            if concept["kind"] == "predicate"
            else _concept_id(concept.get("instance_of_type"))
        )
        if parent not in known:
            _fail(
                "model_registry_release_dependency_undeclared",
                concept_id=concept_id,
                dependency_concept_id=parent,
            )

    relationships = _items(bundle, "relationships")
    relationship_keys: set[tuple[str, str, str]] = set()
    for relation in relationships:
        key = (
            _concept_id(relation.get("source_id")),
            _concept_id(relation.get("predicate")),
            _concept_id(relation.get("target_id")),
        )
        if key in relationship_keys or not set(key) <= known:
            _fail("model_registry_release_relationship_invalid", relationship=list(key))
        relationship_keys.add(key)

    text_relations = [
        _normalise_text_relation(item) for item in _items(bundle, "text_relations")
    ]
    if any(
        relation["subject_concept_id"] not in known
        or relation["resolved_predicate"] not in known
        for relation in text_relations
    ):
        _fail("model_registry_release_text_dependency_undeclared")

    model = bundle.get("model")
    expected = bundle.get("expected_read_back")
    if not isinstance(model, Mapping) or not isinstance(expected, Mapping):
        _fail("model_registry_release_contract_required")
    if any(not _text(expected.get(field)) for field in _EXPECTED_FIELDS):
        _fail("model_registry_release_read_back_field_required")
    if (
        expected["provider"] != model.get("provider")
        or expected["model_id"] != model.get("model_id")
        or expected["model_concept_id"] != model.get("concept_id")
        or expected["registry_entry_id"] != model.get("registry_entry_id")
        or expected["response_storage_policy"] != "disabled"
        or expected["tool_continuation_mode"] != "stateless"
    ):
        _fail("model_registry_release_read_back_contract_invalid")
    for concept_id in (
        expected["model_concept_id"],
        expected["registry_entry_id"],
        expected["api_profile_id"],
    ):
        if concept_id not in created:
            _fail("model_registry_release_identity_not_created", concept_id=concept_id)

    singleton = {
        (item["subject_concept_id"], item["resolved_predicate"]): item
        for item in text_relations
        if item["mode"] == "singleton"
    }
    required_text = {
        (expected["registry_entry_id"], "#V#has_model_id"): expected["model_id"],
        **{
            (expected["api_profile_id"], predicate): expected[field]
            for field, predicate in _PROFILE_PREDICATES.items()
        },
    }
    if any(
        singleton.get(key, {}).get("resolved_text") != value
        for key, value in required_text.items()
    ):
        _fail("model_registry_release_text_contract_incomplete")
    for predicate, field in (
        ("#V#has_model_pricing_json", "pricing_version"),
        ("#V#has_model_capabilities_json", "capabilities_version"),
    ):
        relation = singleton.get((expected["registry_entry_id"], predicate), {})
        if (
            not isinstance(relation.get("json"), Mapping)
            or relation["json"].get("version") != expected[field]
        ):
            _fail(
                "model_registry_release_json_contract_incomplete", predicate=predicate
            )
    pricing = singleton[(expected["registry_entry_id"], "#V#has_model_pricing_json")][
        "json"
    ]
    rates = pricing.get("rates")
    applicability = pricing.get("applicability")
    effective_at = _utc_datetime(pricing.get("effective_at_utc"))
    effective_until = _utc_datetime(pricing.get("effective_until_utc"))
    if (
        pricing.get("schema_version") != "llm_model_pricing.v1"
        or pricing.get("provider") != expected["provider"]
        or pricing.get("model_id") != expected["model_id"]
        or not _positive_number(pricing.get("unit_tokens"))
        or not isinstance(applicability, Mapping)
        or applicability.get("effective_service_tiers") != ["standard"]
        or applicability.get("connection_ids") != [expected["connection_id"]]
        or effective_at is None
        or effective_until is None
        or effective_at >= effective_until
        or not isinstance(rates, Mapping)
        or not _positive_number(rates.get("input_tokens"))
        or not _positive_number(rates.get("output_tokens"))
    ):
        _fail("model_registry_release_pricing_contract_invalid")
    capabilities = singleton[
        (expected["registry_entry_id"], "#V#has_model_capabilities_json")
    ]["json"]
    if (
        capabilities.get("schema_version") != "llm_model_capabilities.v1"
        or capabilities.get("provider") != expected["provider"]
        or capabilities.get("model_id") != expected["model_id"]
        or not _positive_number(capabilities.get("input_token_limit"))
        or not _positive_number(capabilities.get("output_token_limit"))
    ):
        _fail("model_registry_release_capabilities_contract_invalid")

    required_edges = {
        (
            "#V#default_model_registry",
            "#V#has_model_entry",
            expected["registry_entry_id"],
        ),
        (
            expected["registry_entry_id"],
            "#V#refers_to_model",
            expected["model_concept_id"],
        ),
        (
            expected["registry_entry_id"],
            "#V#has_model_api_profile",
            expected["api_profile_id"],
        ),
        (
            expected["model_concept_id"],
            "#V#has_provider",
            f"#V#{expected['provider']}_provider",
        ),
    }
    if not required_edges <= relationship_keys:
        _fail("model_registry_release_graph_contract_incomplete")

    bundle.update(
        {
            "required_existing_concept_ids": sorted(required),
            "concepts": concepts,
            "relationships": relationships,
            "text_relations": text_relations,
        }
    )
    return bundle


def load_model_registry_release_bundle(
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(asset_path or DEFAULT_GEMINI_3_7_FLASH_RELEASE_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelRegistryReleaseError(
            "model_registry_release_asset_unreadable",
            details={"asset_path": str(path)},
        ) from exc
    bundle = validate_model_registry_release_bundle(payload)
    bundle["asset_path"] = str(path)
    return bundle


def _canonical_concept_exists(concept_id: str) -> bool:
    from .concept_service import get_concept_by_id

    return isinstance(get_concept_by_id(concept_id), Mapping)


def inspect_model_registry_release_dependencies(
    bundle: Mapping[str, Any], *, concept_exists: ConceptExistsReader | None = None
) -> dict[str, Any]:
    validated = validate_model_registry_release_bundle(bundle)
    reader = concept_exists or _canonical_concept_exists
    required = list(validated["required_existing_concept_ids"])
    created = [item["concept_id"] for item in validated["concepts"]]
    existing_required = [item for item in required if reader(item)]
    missing = [item for item in required if item not in existing_required]
    return {
        "schema_version": "model_registry_release_dependency_read_back.v1",
        "success": not missing,
        "required_existing_concept_ids": required,
        "existing_required_concept_ids": existing_required,
        "missing_required_concept_ids": missing,
        "release_created_concept_ids": created,
        "already_existing_release_concept_ids": [
            item for item in created if reader(item)
        ],
    }


def _operation_id(version: str, method: str, identity: str) -> str:
    digest = hashlib.sha256(f"{version}\0{method}\0{identity}".encode()).hexdigest()
    return f"model-registry-release:{version}:{digest[:20]}"


def build_model_registry_release_plan(
    bundle: Mapping[str, Any], *, concept_exists: ConceptExistsReader | None = None
) -> dict[str, Any]:
    validated = validate_model_registry_release_bundle(bundle)
    version = validated["release_version"]
    operations: list[dict[str, Any]] = []
    existing: list[str] = []

    def add(method: str, identity: str, arguments: Mapping[str, Any]) -> None:
        operations.append(
            {
                "operation_id": _operation_id(version, method, identity),
                "method_name": method,
                "arguments": copy.deepcopy(dict(arguments)),
            }
        )

    for concept in validated["concepts"]:
        concept_id = concept["concept_id"]
        if concept_exists is not None and concept_exists(concept_id):
            existing.append(concept_id)
            continue
        parent = (
            "#V#predicate"
            if concept["kind"] == "predicate"
            else concept["instance_of_type"]
        )
        spec = {
            key: concept[key]
            for key in ("concept_id", "name", "description")
            if _text(concept.get(key))
        }
        spec["kind"] = _CREATE_KINDS[concept["kind"]]
        if concept["kind"] == "individual":
            spec["instance_of_type"] = parent
        add(
            "create_concepts",
            concept_id,
            {
                "parent_id": parent,
                "concepts": [spec],
                "duplicate_resolution_mode": "canonical_id_only",
                "scope_mode": "global_general",
            },
        )
    expected = validated["expected_read_back"]
    activation_key = (
        "#V#default_model_registry",
        "#V#has_model_entry",
        expected["registry_entry_id"],
    )
    activation_relation: dict[str, Any] | None = None
    for relation in validated["relationships"]:
        relation_key = (
            relation["source_id"],
            relation["predicate"],
            relation["target_id"],
        )
        if relation_key == activation_key:
            activation_relation = relation
            continue
        identity = "|".join(
            (relation["source_id"], relation["predicate"], relation["target_id"])
        )
        add(
            "add_relationship",
            identity,
            {
                "source_id": relation["source_id"],
                "predicate": relation["predicate"],
                "target": relation["target_id"],
            },
        )
    for relation in validated["text_relations"]:
        method = (
            "upsert_singleton_text_relation"
            if relation["mode"] == "singleton"
            else "upsert_text_relation"
        )
        arguments = {
            "concept_id": relation["subject_concept_id"],
            "predicate": relation["resolved_predicate"],
            "text": relation["resolved_text"],
            "language": relation["lang"],
        }
        if relation["mode"] == "singleton":
            arguments.update({"policy": "replace_others", "garbage_collect": True})
        add(method, "|".join(str(value) for value in arguments.values()), arguments)
    if activation_relation is None:
        _fail("model_registry_release_activation_edge_missing")
    add(
        "add_relationship",
        "|".join(activation_key),
        {
            "source_id": activation_relation["source_id"],
            "predicate": activation_relation["predicate"],
            "target": activation_relation["target_id"],
        },
    )
    return {
        "schema_version": "model_registry_release_plan.v1",
        "release_version": version,
        "source_tag": validated.get("source_tag"),
        "authority_requirement": "authenticated_global_ontology_publication",
        "operations": operations,
        "already_existing_release_concept_ids": existing,
    }


def verify_model_registry_release_read_back(
    bundle: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    validated = validate_model_registry_release_bundle(bundle)
    expected = validated["expected_read_back"]
    models = snapshot.get("models") if isinstance(snapshot, Mapping) else []
    models = (
        models if isinstance(models, Sequence) and not isinstance(models, str) else []
    )
    entry = next(
        (
            item
            for item in models
            if isinstance(item, Mapping)
            and item.get("registry_entry_id") == expected["registry_entry_id"]
        ),
        {},
    )
    profiles = entry.get("api_profiles") if isinstance(entry, Mapping) else []
    profile = next(
        (
            item
            for item in (profiles or [])
            if isinstance(item, Mapping)
            and item.get("profile_concept_id") == expected["api_profile_id"]
        ),
        {},
    )
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), Mapping) else {}
    capabilities = (
        entry.get("capabilities")
        if isinstance(entry.get("capabilities"), Mapping)
        else {}
    )
    expected_pricing = next(
        relation["json"]
        for relation in validated["text_relations"]
        if relation["subject_concept_id"] == expected["registry_entry_id"]
        and relation["resolved_predicate"] == "#V#has_model_pricing_json"
    )
    expected_capabilities = next(
        relation["json"]
        for relation in validated["text_relations"]
        if relation["subject_concept_id"] == expected["registry_entry_id"]
        and relation["resolved_predicate"] == "#V#has_model_capabilities_json"
    )
    actual = {
        "provider": entry.get("provider"),
        "model_id": entry.get("model_id"),
        "registry_entry_id": entry.get("registry_entry_id"),
        "model_concept_id": entry.get("concept_id"),
        "api_profile_id": profile.get("profile_concept_id"),
        **{field: profile.get(field) for field in _PROFILE_PREDICATES},
        "pricing_version": pricing.get("version"),
        "capabilities_version": capabilities.get("version"),
        "pricing_payload": copy.deepcopy(dict(pricing)),
        "capabilities_payload": copy.deepcopy(dict(capabilities)),
    }
    mismatches = {
        field: {"expected": expected[field], "actual": actual.get(field)}
        for field in _EXPECTED_FIELDS
        if actual.get(field) != expected[field]
    }
    for field, expected_payload in (
        ("pricing_payload", expected_pricing),
        ("capabilities_payload", expected_capabilities),
    ):
        if actual[field] != expected_payload:
            mismatches[field] = {
                "expected": copy.deepcopy(dict(expected_payload)),
                "actual": copy.deepcopy(dict(actual[field])),
            }
    return {
        "schema_version": "model_registry_release_read_back.v1",
        "success": not mismatches,
        "matched": not mismatches,
        "release_version": validated["release_version"],
        "snapshot_source": snapshot.get("source"),
        "expected": copy.deepcopy(dict(expected)),
        "actual": actual,
        "mismatches": mismatches,
    }


def _fresh_registry_snapshot() -> Mapping[str, Any]:
    from .model_registry_service import (
        clear_model_registry_snapshot_caches,
        get_model_registry_snapshot,
    )

    clear_model_registry_snapshot_caches(remove_disk=True)
    return get_model_registry_snapshot()


def _operation_succeeded(method: str, result: Mapping[str, Any]) -> bool:
    if result.get("success") is False or result.get("effect_status") in {
        "failed",
        "indeterminate",
        "partial",
    }:
        return False
    if method == "create_concepts":
        return result.get("total") == result.get("successful") == 1
    return result.get("success") is True


def publish_model_registry_release_bundle(
    *,
    invoke_governed_method: GovernedMethodInvoker,
    asset_path: str | Path | None = None,
    concept_exists: ConceptExistsReader | None = None,
    snapshot_reader: RegistrySnapshotReader | None = None,
    refreshed_snapshot_reader: RegistrySnapshotReader | None = None,
) -> dict[str, Any]:
    """Publish through governed calls and require fresh canonical read-back."""

    bundle = load_model_registry_release_bundle(asset_path)
    exists = concept_exists or _canonical_concept_exists
    dependency_read_back = inspect_model_registry_release_dependencies(
        bundle, concept_exists=exists
    )
    if not dependency_read_back["success"]:
        _fail(
            "model_registry_release_dependency_missing",
            missing_required_concept_ids=dependency_read_back[
                "missing_required_concept_ids"
            ],
        )
    read_before = snapshot_reader or _fresh_registry_snapshot
    before = verify_model_registry_release_read_back(bundle, read_before())
    if before["matched"]:
        return {
            "schema_version": "model_registry_release_publication.v1",
            "success": True,
            "changed": False,
            "idempotent": True,
            "release_version": bundle["release_version"],
            "operation_results": [],
            "dependency_read_back": dependency_read_back,
            "canonical_read_back": before,
        }

    plan = build_model_registry_release_plan(bundle, concept_exists=exists)
    receipts: list[dict[str, Any]] = []
    changed = False
    for operation in plan["operations"]:
        result = invoke_governed_method(
            operation["method_name"], copy.deepcopy(operation["arguments"])
        )
        if not isinstance(result, Mapping) or not _operation_succeeded(
            operation["method_name"], result if isinstance(result, Mapping) else {}
        ):
            _fail(
                "model_registry_release_governed_operation_failed",
                operation_id=operation["operation_id"],
                method_name=operation["method_name"],
                completed_operation_ids=[item["operation_id"] for item in receipts],
            )
        changed = changed or any(
            bool(result.get(field))
            for field in ("changed", "added", "relation_created", "successful")
        )
        receipts.append(
            {
                "operation_id": operation["operation_id"],
                "method_name": operation["method_name"],
                "result": copy.deepcopy(dict(result)),
            }
        )

    read_after = refreshed_snapshot_reader or _fresh_registry_snapshot
    after = verify_model_registry_release_read_back(bundle, read_after())
    if not after["matched"]:
        _fail(
            "model_registry_release_canonical_read_back_failed",
            completed_operation_ids=[item["operation_id"] for item in receipts],
            canonical_read_back=after,
        )
    return {
        "schema_version": "model_registry_release_publication.v1",
        "success": True,
        "changed": changed,
        "idempotent": not changed,
        "release_version": bundle["release_version"],
        "operation_results": receipts,
        "dependency_read_back": dependency_read_back,
        "canonical_read_back": after,
    }


__all__ = [
    "DEFAULT_GEMINI_3_7_FLASH_RELEASE_PATH",
    "MODEL_REGISTRY_RELEASE_BUNDLE_SCHEMA_VERSION",
    "ModelRegistryReleaseError",
    "build_model_registry_release_plan",
    "inspect_model_registry_release_dependencies",
    "load_model_registry_release_bundle",
    "publish_model_registry_release_bundle",
    "validate_model_registry_release_bundle",
    "verify_model_registry_release_read_back",
]
