"""Vontology-backed workflow-template selection and rendering helpers.

Workflow creation resolves reusable workflow-spec templates from first-class
Vontology artefacts. Repo-side workflow template bundles remain seed fixtures
only and are used only to hydrate missing template concepts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Mapping, Sequence

from ..services import concept_search_service, concept_service
from ..services.concept_service import ConceptNotFoundError
from ..services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)

REPO_SEED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION = (
    "repo_seed_workflow_template_bundle.v1"
)
WORKFLOW_TEMPLATE_PROFILE_SCHEMA_VERSION = "workflow_template_profile.v1"
WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD = "repo_seed_version"
_KNOWN_LEGACY_AUTHORITY_PAYLOAD_SHA256_BY_SEED_VERSION_FIELD = (
    "known_legacy_authority_payload_sha256_by_seed_version"
)
_SEED_MIGRATION_RECEIPT_CONTEXT_KEY = "repo_seed_migration_receipt"
_SEED_MIGRATION_RECEIPT_SCHEMA_VERSION = "repo_seed_migration_receipt.v1"
_SEED_INITIAL_MATERIALISATION_RECEIPT_ATTRIBUTE = (
    "repo_seed_initial_materialisation_receipt"
)
_SEED_INITIAL_MATERIALISATION_RECEIPT_SCHEMA_VERSION = (
    "repo_seed_initial_materialisation_receipt.v1"
)
WORKFLOW_TEMPLATE_REPO_SEED_DIR = Path(__file__).with_name("repo_seed_bundles")
DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH = (
    WORKFLOW_TEMPLATE_REPO_SEED_DIR / "workflow_template_seed_bundle.json"
)

WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID = "workflow_creation.default_marker"
WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID = "workflow_creation.scholarly_representation"
WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID = (
    "workflow_creation.phd_student_representation"
)
WORKFLOW_CREATION_PERSON_TEMPLATE_ID = "workflow_creation.person_representation"
WORKFLOW_CREATION_COMPANY_TEMPLATE_ID = "workflow_creation.company_representation"
WORKFLOW_CREATION_EVENT_TEMPLATE_ID = "workflow_creation.event_representation"
WORKFLOW_CREATION_PLACE_TEMPLATE_ID = "workflow_creation.place_representation"

WORKFLOW_TEMPLATE_TYPE_ID = "#V#workflow_definition_template"
WORKFLOW_TEMPLATE_ID_PREDICATE = "#V#hasWorkflowTemplateId"
WORKFLOW_TEMPLATE_PROFILE_PREDICATE = "#V#hasWorkflowTemplateProfileJson"
WORKFLOW_TEMPLATE_SPEC_PREDICATE = "#V#hasWorkflowSpecTemplateJson"
WORKFLOW_TEMPLATE_DEFAULT_DESCRIPTION_PREDICATE = (
    "#V#hasWorkflowTemplateDefaultDescription"
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DESCRIPTION_PREDICATES = ("hasDescription", "#V#hasDescription")
_TEMPLATE_ID_PREDICATES = (
    WORKFLOW_TEMPLATE_ID_PREDICATE,
    "hasWorkflowTemplateId",
    "has_workflow_template_id",
)
_TEMPLATE_PROFILE_PREDICATES = (
    WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
    "hasWorkflowTemplateProfileJson",
    "has_workflow_template_profile_json",
)
_TEMPLATE_SPEC_PREDICATES = (
    WORKFLOW_TEMPLATE_SPEC_PREDICATE,
    "hasWorkflowSpecTemplateJson",
    "has_workflow_spec_template_json",
)
_TEMPLATE_DEFAULT_DESCRIPTION_PREDICATES = (
    WORKFLOW_TEMPLATE_DEFAULT_DESCRIPTION_PREDICATE,
    "hasWorkflowTemplateDefaultDescription",
    "has_workflow_template_default_description",
)
_CANONICAL_TEMPLATE_CONCEPT_IDS = {
    WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID: "#V#workflow_template_workflow_creation_default_marker",
    WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID: (
        "#V#workflow_template_workflow_creation_scholarly_representation"
    ),
    WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID: (
        "#V#workflow_template_workflow_creation_phd_student_representation"
    ),
    WORKFLOW_CREATION_PERSON_TEMPLATE_ID: (
        "#V#workflow_template_workflow_creation_person_representation"
    ),
    WORKFLOW_CREATION_COMPANY_TEMPLATE_ID: (
        "#V#workflow_template_workflow_creation_company_representation"
    ),
    WORKFLOW_CREATION_EVENT_TEMPLATE_ID: (
        "#V#workflow_template_workflow_creation_event_representation"
    ),
    WORKFLOW_CREATION_PLACE_TEMPLATE_ID: (
        "#V#workflow_template_workflow_creation_place_representation"
    ),
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _normalise_string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        lowered = text.lower()
        if not text or lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
    return tuple(items)


def _normalise_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _tokenise(value: Any) -> tuple[str, ...]:
    text = _clean_text(value).lower()
    if not text:
        return ()
    return tuple(dict.fromkeys(_TOKEN_RE.findall(text)))


def _contains_term(text: str, term: str) -> bool:
    candidate = _clean_text(term).lower()
    if not candidate:
        return False
    if " " in candidate:
        return candidate in text
    return candidate in set(_tokenise(text))


def _lexical_score(
    *,
    query_text: str,
    keywords: Sequence[str],
    exemplars: Sequence[str],
    title: str,
    description: str,
) -> tuple[float, dict[str, Any]]:
    query = _clean_text(query_text).lower()
    query_tokens = set(_tokenise(query))
    if not query_tokens and not query:
        return 0.0, {"keyword_hits": (), "phrase_hits": (), "exemplar_hits": ()}

    keyword_hits: list[str] = []
    for keyword in keywords:
        if _contains_term(query, keyword):
            keyword_hits.append(_clean_text(keyword))

    candidate_text = " ".join(
        part
        for part in (
            _clean_text(title),
            _clean_text(description),
            *(_clean_text(item) for item in keywords),
            *(_clean_text(item) for item in exemplars),
        )
        if part
    ).lower()
    candidate_tokens = set(_tokenise(candidate_text))
    overlap = tuple(sorted(query_tokens & candidate_tokens))

    phrase_hits: list[str] = []
    for phrase in keywords:
        phrase_text = _clean_text(phrase).lower()
        if " " in phrase_text and phrase_text in query:
            phrase_hits.append(phrase_text)

    exemplar_hits: list[str] = []
    for exemplar in exemplars:
        exemplar_text = _clean_text(exemplar).lower()
        if exemplar_text and (exemplar_text in query or query in exemplar_text):
            exemplar_hits.append(exemplar_text)

    score = min(
        1.0,
        len(overlap) * 0.12
        + len(keyword_hits) * 0.18
        + len(phrase_hits) * 0.28
        + len(exemplar_hits) * 0.34,
    )
    return (
        round(score, 3),
        {
            "keyword_hits": tuple(keyword_hits),
            "phrase_hits": tuple(phrase_hits),
            "exemplar_hits": tuple(exemplar_hits),
            "token_overlap": overlap,
        },
    )


def _normalise_selection_mode(value: Any) -> str:
    mode = _clean_text(value).lower().replace("-", "_")
    if mode in {"automatic", "explicit_only", "fallback"}:
        return mode
    return "automatic"


def _normalise_template_profile(
    raw_profile: Any,
    *,
    template_id: str,
    repo_seed_version: str | None = None,
) -> dict[str, Any]:
    profile = dict(raw_profile) if isinstance(raw_profile, Mapping) else {}
    normalised = {
        "schema_version": WORKFLOW_TEMPLATE_PROFILE_SCHEMA_VERSION,
        "template_id": template_id,
        "selection_mode": _normalise_selection_mode(profile.get("selection_mode")),
        "priority": _normalise_int(profile.get("priority"), default=0),
        "keywords": _normalise_string_tuple(profile.get("keywords")),
        "exemplars": _normalise_string_tuple(
            profile.get("exemplars") or profile.get("examples")
        ),
        "required_terms_all": _normalise_string_tuple(
            profile.get("required_terms_all")
        ),
        "required_terms_any": _normalise_string_tuple(
            profile.get("required_terms_any")
        ),
        "forbidden_terms_any": _normalise_string_tuple(
            profile.get("forbidden_terms_any")
        ),
        "requires_synthesis_policy": _normalise_bool(
            profile.get("requires_synthesis_policy")
        ),
    }
    version = _clean_text(
        repo_seed_version
        if repo_seed_version is not None
        else profile.get(WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD)
    )
    if version:
        normalised[WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD] = version
    return normalised


def _numeric_seed_version(value: Any) -> tuple[int, ...] | None:
    version = _clean_text(value)
    if not version or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version) is None:
        return None
    return tuple(int(part) for part in version.split("."))


def _repo_seed_version_is_newer(*, candidate: Any, current: Any) -> bool:
    candidate_parts = _numeric_seed_version(candidate)
    if candidate_parts is None:
        return False
    current_parts = _numeric_seed_version(current)
    if current_parts is None:
        # Unknown or human-authored authority is not an implicit legacy seed.
        # Only a numeric represented version proves that this reviewed seed is
        # newer; explicit force/migration tooling must adjudicate unversioned
        # content rather than normal bootstrap overwriting it.
        return False
    width = max(len(candidate_parts), len(current_parts))
    return candidate_parts + (0,) * (width - len(candidate_parts)) > (
        current_parts + (0,) * (width - len(current_parts))
    )


def _stable_payload_sha256(value: Any) -> str:
    serialised = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _normalise_known_legacy_authority_digests_by_seed_version(
    value: Any,
) -> dict[str, dict[str, frozenset[str]]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(
            "repo_seed_workflow_template_known_legacy_digests_invalid"
        )
    normalised: dict[str, dict[str, frozenset[str]]] = {}
    for raw_template_id, raw_versions in value.items():
        template_id = _clean_text(raw_template_id)
        if not template_id or not isinstance(raw_versions, Mapping):
            raise ValueError(
                "repo_seed_workflow_template_known_legacy_digests_invalid"
            )
        version_digests: dict[str, frozenset[str]] = {}
        for raw_version, raw_digests in raw_versions.items():
            version_key = _clean_text(raw_version).lower()
            if (
                not version_key
                or not isinstance(raw_digests, Sequence)
                or isinstance(raw_digests, (str, bytes, bytearray))
            ):
                raise ValueError(
                    "repo_seed_workflow_template_known_legacy_digests_invalid"
                )
            digests: set[str] = set()
            for raw_digest in raw_digests:
                digest = _clean_text(raw_digest).lower()
                if len(digest) != 64 or any(
                    character not in "0123456789abcdef" for character in digest
                ):
                    raise ValueError(
                        "repo_seed_workflow_template_known_legacy_digest_invalid"
                    )
                digests.add(digest)
            version_digests[version_key] = frozenset(digests)
        normalised[template_id] = version_digests
    return normalised


def _template_seed_version_key(value: Any) -> str:
    version = _clean_text(value).lower()
    return version or "unversioned"


def _template_seed_migration_receipt(
    *,
    status: str,
    source_seed_version_key: str,
    source_authority_payload_sha256: str,
    source_authority_payload: Mapping[str, Any],
    target_seed_version: str,
    target_authority_payload_sha256: str,
    safe_partial_authority_payload_sha256: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": _SEED_MIGRATION_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "source_seed_version_key": source_seed_version_key,
        "source_authority_payload_sha256": source_authority_payload_sha256,
        # The exact source snapshot lets a later process recompute the only
        # partial states reachable by the ordered writes.  The digest list is
        # diagnostic evidence, not a caller-controlled migration capability.
        "source_authority_payload": copy.deepcopy(dict(source_authority_payload)),
        "target_seed_version": target_seed_version,
        "target_authority_payload_sha256": target_authority_payload_sha256,
        "safe_partial_authority_payload_sha256": list(
            dict.fromkeys(safe_partial_authority_payload_sha256)
        ),
    }


def _template_initial_materialisation_receipt(
    *,
    template_id: str,
    target_seed_version: str,
    target_authority_payload_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": (
            _SEED_INITIAL_MATERIALISATION_RECEIPT_SCHEMA_VERSION
        ),
        "status": "pending",
        "artefact_kind": "workflow_template",
        "template_id": template_id,
        "target_seed_version": target_seed_version,
        "target_authority_payload_sha256": target_authority_payload_sha256,
    }


def _pending_template_initial_materialisation_receipt_is_valid(
    value: Any,
    *,
    template_id: str,
    target_seed_version: str,
    target_authority_payload_sha256: str,
    target_authority_payload: Mapping[str, Any],
    current_partial_authority_payload: Mapping[str, Any],
) -> bool:
    partial_state_safe = all(
        field_name in target_authority_payload
        and current_value == target_authority_payload.get(field_name)
        for field_name, current_value in current_partial_authority_payload.items()
    )
    return bool(
        isinstance(value, Mapping)
        and value.get("schema_version")
        == _SEED_INITIAL_MATERIALISATION_RECEIPT_SCHEMA_VERSION
        and value.get("status") == "pending"
        and value.get("artefact_kind") == "workflow_template"
        and _clean_text(value.get("template_id")) == template_id
        and _clean_text(value.get("target_seed_version"))
        == target_seed_version
        and _clean_text(value.get("target_authority_payload_sha256")).lower()
        == target_authority_payload_sha256
        and _stable_payload_sha256(target_authority_payload)
        == target_authority_payload_sha256
        and partial_state_safe
    )


def _pending_template_seed_migration_receipt_is_valid(
    value: Any,
    *,
    target_seed_version: str,
    target_authority_payload_sha256: str,
    target_authority_payload: Mapping[str, Any],
    current_authority_payload_sha256: str,
    known_digests_by_version: Mapping[str, frozenset[str]],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    source_version_key = _clean_text(value.get("source_seed_version_key")).lower()
    source_sha256 = _clean_text(
        value.get("source_authority_payload_sha256")
    ).lower()
    source_payload = value.get("source_authority_payload")
    raw_safe_partial_digests = value.get(
        "safe_partial_authority_payload_sha256"
    )
    if (
        not isinstance(source_payload, Mapping)
        or not isinstance(raw_safe_partial_digests, Sequence)
        or isinstance(raw_safe_partial_digests, (str, bytes, bytearray))
    ):
        return False
    expected_safe_partial_digests = set(
        _safe_partial_template_migration_digests(
            source_payload=source_payload,
            target_payload=target_authority_payload,
        )
    )
    recorded_safe_partial_digests = {
        _clean_text(item).lower()
        for item in raw_safe_partial_digests
        if _clean_text(item)
    }
    return bool(
        value.get("schema_version") == _SEED_MIGRATION_RECEIPT_SCHEMA_VERSION
        and value.get("status") == "pending"
        and _clean_text(value.get("target_seed_version")) == target_seed_version
        and _clean_text(value.get("target_authority_payload_sha256")).lower()
        == target_authority_payload_sha256
        and source_sha256 in known_digests_by_version.get(source_version_key, ())
        and _stable_payload_sha256(source_payload) == source_sha256
        and _stable_payload_sha256(target_authority_payload)
        == target_authority_payload_sha256
        and recorded_safe_partial_digests == expected_safe_partial_digests
        and current_authority_payload_sha256 in expected_safe_partial_digests
    )


def _safe_partial_template_migration_digests(
    *,
    source_payload: Mapping[str, Any],
    target_payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Enumerate authority states reachable by the ordered seed writes."""

    current = copy.deepcopy(dict(source_payload))
    digests = [_stable_payload_sha256(current)]
    for field_name in (
        "template_id",
        "workflow_spec_template",
        "description",
        "default_workflow_description",
        "profile",
    ):
        target_value = copy.deepcopy(target_payload.get(field_name))
        if field_name in {"description", "default_workflow_description"} and not (
            _clean_text(target_value)
        ):
            continue
        current[field_name] = target_value
        digests.append(_stable_payload_sha256(current))
    return tuple(dict.fromkeys(digests))


def _iter_required_template_variables(
    template_payload: Any,
) -> tuple[str, ...]:
    variables: list[str] = []
    formatter = Formatter()

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            for _literal_text, field_name, _format_spec, _conversion in formatter.parse(
                value
            ):
                if field_name:
                    variables.append(field_name)
            return
        if isinstance(value, Mapping):
            for child in value.values():
                _walk(child)
            return
        if isinstance(value, Sequence) and not isinstance(value, str):
            for child in value:
                _walk(child)

    _walk(template_payload)
    return tuple(dict.fromkeys(variables))


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> Any:  # pragma: no cover - defensive
        raise KeyError(key)


def _render_template_value(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format_map(_SafeFormatDict(dict(variables)))
        except KeyError as exc:  # pragma: no cover - explicit error pathway
            missing = str(exc.args[0] if exc.args else exc)
            raise ValueError(f"workflow_template_variable_missing:{missing}") from exc
    if isinstance(value, Mapping):
        return {
            str(key): _render_template_value(child, variables)
            for key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_render_template_value(child, variables) for child in value]
    return copy.deepcopy(value)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = concept_service.get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    except Exception:
        return None
    return concept if isinstance(concept, Mapping) else None


def _text_row_for_predicates(
    concept_id: str,
    predicates: Sequence[str],
) -> Mapping[str, Any] | None:
    for predicate in predicates:
        rows = get_texts_for_concept(concept_id, predicate=predicate, limit=10)
        for row in rows:
            text = _clean_text((row or {}).get("text"))
            if text:
                return row
    return None


def _text_value_for_predicates(
    concept_id: str,
    predicates: Sequence[str],
) -> str | None:
    row = _text_row_for_predicates(concept_id, predicates)
    return _clean_text(row.get("text")) if isinstance(row, Mapping) else None


def _parse_json_mapping_text(
    text: str | None,
    *,
    error_code: str,
) -> dict[str, Any]:
    if not text:
        raise ValueError(error_code)
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(error_code) from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(error_code)
    return dict(parsed)


def _template_authority_payload(
    *,
    template_id: str,
    profile: Mapping[str, Any],
    workflow_spec_template: Mapping[str, Any],
    description: Any,
    default_workflow_description: Any,
) -> dict[str, Any]:
    return {
        "template_id": template_id,
        "profile": dict(profile),
        "workflow_spec_template": dict(workflow_spec_template),
        "description": _clean_text(description),
        "default_workflow_description": _clean_text(
            default_workflow_description
        ),
    }


def _live_template_authority_payload(
    *,
    concept_id: str,
    template_id: str,
) -> dict[str, Any]:
    live_template_id = _text_value_for_predicates(
        concept_id,
        _TEMPLATE_ID_PREDICATES,
    )
    if not live_template_id:
        raise ValueError(
            f"workflow_template_id_missing_or_invalid:{template_id}"
        )
    profile = _parse_json_mapping_text(
        _text_value_for_predicates(concept_id, _TEMPLATE_PROFILE_PREDICATES),
        error_code="workflow_template_profile_missing_or_invalid",
    )
    workflow_spec_template = _parse_json_mapping_text(
        _text_value_for_predicates(concept_id, _TEMPLATE_SPEC_PREDICATES),
        error_code="workflow_template_spec_missing_or_invalid",
    )
    return _template_authority_payload(
        template_id=live_template_id,
        profile=profile,
        workflow_spec_template=workflow_spec_template,
        description=_text_value_for_predicates(concept_id, _DESCRIPTION_PREDICATES),
        default_workflow_description=_text_value_for_predicates(
            concept_id,
            _TEMPLATE_DEFAULT_DESCRIPTION_PREDICATES,
        ),
    )


def _live_template_partial_authority_payload(
    *,
    concept_id: str,
) -> dict[str, Any] | None:
    """Read present template authority fields without treating absence as error.

    Invalid present JSON remains a blocker. This is used only to prove that an
    interrupted initial materialisation still contains target values or missing
    fields, never novel human-authored authority.
    """

    payload: dict[str, Any] = {}
    live_template_id = _text_value_for_predicates(
        concept_id,
        _TEMPLATE_ID_PREDICATES,
    )
    if live_template_id:
        payload["template_id"] = live_template_id
    for field_name, predicates, error_code in (
        (
            "profile",
            _TEMPLATE_PROFILE_PREDICATES,
            "workflow_template_profile_invalid",
        ),
        (
            "workflow_spec_template",
            _TEMPLATE_SPEC_PREDICATES,
            "workflow_template_spec_invalid",
        ),
    ):
        text = _text_value_for_predicates(concept_id, predicates)
        if not text:
            continue
        try:
            payload[field_name] = _parse_json_mapping_text(
                text,
                error_code=error_code,
            )
        except ValueError:
            return None
    description = _text_value_for_predicates(concept_id, _DESCRIPTION_PREDICATES)
    if description:
        payload["description"] = description
    default_description = _text_value_for_predicates(
        concept_id,
        _TEMPLATE_DEFAULT_DESCRIPTION_PREDICATES,
    )
    if default_description:
        payload["default_workflow_description"] = default_description
    return payload


def _template_concept_id(template_id: str) -> str:
    override = _CANONICAL_TEMPLATE_CONCEPT_IDS.get(template_id)
    if override:
        return override
    slug = _SLUG_RE.sub("_", template_id.strip().lower()).strip("_")
    if not slug:
        raise ValueError("workflow_template_id_missing")
    return f"#V#workflow_template_{slug}"


def _concept_display_name(concept_doc: Mapping[str, Any], fallback: str) -> str:
    names = concept_doc.get("names")
    if isinstance(names, list):
        for name_row in names:
            if not isinstance(name_row, Mapping):
                continue
            name_text = _clean_text(name_row.get("name"))
            if name_text and str(name_row.get("type") or "NL").upper() == "NL":
                return name_text
    return fallback


@lru_cache(maxsize=None)
def _load_repo_seed_workflow_template_bundle_cached(asset_path: str) -> dict[str, Any]:
    path = Path(asset_path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("repo_seed_workflow_template_bundle_not_mapping")

    schema_version = _clean_text(payload.get("schema_version"))
    if schema_version != REPO_SEED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "repo_seed_workflow_template_bundle_schema_unsupported:"
            f"{schema_version or 'missing'}"
        )

    raw_templates = payload.get("templates")
    if not isinstance(raw_templates, Sequence) or isinstance(raw_templates, str):
        raise ValueError("repo_seed_workflow_template_bundle_templates_missing")

    templates: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    repo_seed_version = _clean_text(payload.get("seed_version"))
    known_legacy_authority_digests_by_seed_version = (
        _normalise_known_legacy_authority_digests_by_seed_version(
            payload.get(
                _KNOWN_LEGACY_AUTHORITY_PAYLOAD_SHA256_BY_SEED_VERSION_FIELD
            )
        )
    )

    for item in raw_templates:
        if not isinstance(item, Mapping):
            continue
        template_id = _clean_text(item.get("template_id"))
        if not template_id:
            raise ValueError("repo_seed_workflow_template_id_missing")
        raw_template = item.get("workflow_spec_template")
        if not isinstance(raw_template, Mapping):
            raise ValueError(f"repo_seed_workflow_template_spec_missing:{template_id}")
        profile = _normalise_template_profile(
            item.get("template_profile"),
            template_id=template_id,
            repo_seed_version=repo_seed_version or None,
        )
        templates[template_id] = {
            "template_id": template_id,
            "name": _clean_text(item.get("name")) or template_id,
            "description": _clean_text(item.get("description")),
            "default_workflow_description": _clean_text(
                item.get("default_workflow_description")
            ),
            "workflow_spec_template": copy.deepcopy(dict(raw_template)),
            "required_variables": _iter_required_template_variables(raw_template),
            "concept_id": _template_concept_id(template_id),
        }
        profiles[template_id] = profile

    return {
        "asset_path": str(path),
        "family_id": _clean_text(payload.get("family_id")),
        "seed_version": repo_seed_version,
        "known_legacy_authority_payload_sha256_by_seed_version": {
            template_id: {
                version_key: sorted(digests)
                for version_key, digests in version_digests.items()
            }
            for template_id, version_digests in (
                known_legacy_authority_digests_by_seed_version.items()
            )
        },
        "source_tag": _clean_text(payload.get("source_tag")),
        "source": "repo_seed_bundle",
        "templates": templates,
        "profiles": profiles,
    }


def _ensure_template_type_surface() -> str:
    concept_doc = _safe_get_concept(WORKFLOW_TEMPLATE_TYPE_ID)
    if concept_doc is not None:
        return WORKFLOW_TEMPLATE_TYPE_ID

    concept_service.create_concept(
        name="Workflow Definition Template",
        concept_id=WORKFLOW_TEMPLATE_TYPE_ID,
        description=(
            "Reusable declarative workflow authoring template stored as a first-class "
            "Vontology artefact."
        ),
        parent_concept_ids=["#V#workflow_definition"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )
    return WORKFLOW_TEMPLATE_TYPE_ID


def _ensure_template_instance_typing(concept_id: str) -> bool:
    concept_doc = _safe_get_concept(concept_id)
    if concept_doc is None:
        return False
    relationships = dict(concept_doc.get("relationships") or {})
    instance_of = relationships.get("is_an_instance_of") or []
    if isinstance(instance_of, str):
        instance_of = [instance_of]
    else:
        instance_of = [
            str(item).strip()
            for item in instance_of
            if isinstance(item, str) and str(item).strip()
        ]
    if WORKFLOW_TEMPLATE_TYPE_ID in instance_of:
        return False
    instance_of.append(WORKFLOW_TEMPLATE_TYPE_ID)
    relationships["is_an_instance_of"] = list(dict.fromkeys(instance_of))
    concept_service.update_concept(concept_id, {"relationships": relationships})
    return True


def ensure_repo_seeded_workflow_template_bundle(
    *,
    asset_path: str | Path = DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
    template_ids: Sequence[str] | None = None,
    migrate_older_seed_versions: bool = False,
) -> dict[str, Any]:
    """Hydrate missing templates or explicitly migrate older seeded versions.

    Normal runtime loading remains Vontology-authoritative and only calls this
    surface for missing concepts.  A service that owns a reviewed template
    release may opt into ``migrate_older_seed_versions``; equal or newer
    represented versions are then preserved rather than silently overwritten.
    Legacy authority advances only when its complete payload matches a reviewed
    digest for its represented seed version. Interrupted migrations carry a
    receipt whose source snapshot is independently checked before resumption.
    """

    seed_bundle = _load_repo_seed_workflow_template_bundle_cached(
        str(Path(asset_path).resolve())
    )
    templates = dict(seed_bundle.get("templates") or {})
    profiles = dict(seed_bundle.get("profiles") or {})
    repo_seed_version = _clean_text(seed_bundle.get("seed_version"))
    known_legacy_digests_by_template_and_version = {
        template_id: {
            version_key: frozenset(
                _clean_text(digest).lower()
                for digest in raw_digests
                if _clean_text(digest)
            )
            for version_key, raw_digests in raw_versions.items()
            if isinstance(raw_digests, Sequence)
            and not isinstance(raw_digests, (str, bytes, bytearray))
        }
        for template_id, raw_versions in dict(
            seed_bundle.get(
                "known_legacy_authority_payload_sha256_by_seed_version"
            )
            or {}
        ).items()
        if isinstance(raw_versions, Mapping)
    }
    requested_ids = (
        tuple(
            str(item).strip()
            for item in template_ids
            if isinstance(item, str) and str(item).strip()
        )
        if template_ids is not None
        else tuple(templates.keys())
    )

    created_concept_ids: list[str] = []
    typed_concept_ids: list[str] = []
    persisted_template_ids: list[str] = []
    migrated_template_ids: list[str] = []
    migrated_known_legacy_template_ids: list[str] = []
    resumed_initial_materialisation_template_ids: list[str] = []
    skipped_current_template_ids: list[str] = []
    migration_readback_by_template_id: dict[str, dict[str, Any]] = {}
    unversioned_migration_blockers_by_template_id: dict[str, dict[str, Any]] = {}
    migration_blockers_by_template_id: dict[str, dict[str, Any]] = {}
    seed_version_authority_mismatches_by_template_id: dict[
        str, dict[str, Any]
    ] = {}
    errors_by_template_id: dict[str, str] = {}

    for template_id in requested_ids:
        migration_pending = False
        initial_materialisation_pending = False
        known_legacy_migration_pending = False
        pending_receipt: Mapping[str, Any] | None = None
        migration_source_version_key = ""
        migration_source_authority_sha256 = ""
        migration_source_authority_payload: dict[str, Any] = {}
        migration_safe_partial_digests: tuple[str, ...] = ()
        migration_profile_context: dict[str, Any] = {}
        template = templates.get(template_id)
        profile = profiles.get(template_id)
        if not isinstance(template, Mapping) or not isinstance(profile, Mapping):
            errors_by_template_id[template_id] = "workflow_template_seed_missing"
            continue
        expected_authority_payload = _template_authority_payload(
            template_id=template_id,
            profile=profile,
            workflow_spec_template=dict(
                template.get("workflow_spec_template") or {}
            ),
            description=template.get("description"),
            default_workflow_description=template.get(
                "default_workflow_description"
            ),
        )
        expected_authority_sha256 = _stable_payload_sha256(
            expected_authority_payload
        )

        concept_id = _template_concept_id(template_id)
        initial_materialisation_receipt = (
            _template_initial_materialisation_receipt(
                template_id=template_id,
                target_seed_version=repo_seed_version,
                target_authority_payload_sha256=expected_authority_sha256,
            )
        )
        concept_doc = _safe_get_concept(concept_id)
        if concept_doc is None:
            try:
                _ensure_template_type_surface()
                concept_service.create_concept(
                    name=_clean_text(template.get("name")) or template_id,
                    concept_id=concept_id,
                    description=_clean_text(template.get("description")),
                    parent_concept_ids=[WORKFLOW_TEMPLATE_TYPE_ID],
                    create_as_instance=True,
                    attributes={
                        _SEED_INITIAL_MATERIALISATION_RECEIPT_ATTRIBUTE: (
                            initial_materialisation_receipt
                        )
                    },
                    visibility_scope_mode="global_general",
                )
                created_concept_ids.append(concept_id)
                initial_materialisation_pending = True
                pending_receipt = initial_materialisation_receipt
            except Exception as exc:
                errors_by_template_id[template_id] = (
                    f"workflow_template_concept_create_failed:{exc}"
                )
                continue
        else:
            concept_attributes = concept_doc.get("attributes")
            existing_initial_receipt = (
                concept_attributes.get(
                    _SEED_INITIAL_MATERIALISATION_RECEIPT_ATTRIBUTE
                )
                if isinstance(concept_attributes, Mapping)
                else None
            )
            current_partial_authority_payload = (
                _live_template_partial_authority_payload(concept_id=concept_id)
            )
            if _pending_template_initial_materialisation_receipt_is_valid(
                existing_initial_receipt,
                template_id=template_id,
                target_seed_version=repo_seed_version,
                target_authority_payload_sha256=expected_authority_sha256,
                target_authority_payload=expected_authority_payload,
                current_partial_authority_payload=(
                    current_partial_authority_payload
                    if isinstance(current_partial_authority_payload, Mapping)
                    else {"invalid": True}
                ),
            ) and isinstance(existing_initial_receipt, Mapping):
                initial_materialisation_pending = True
                pending_receipt = dict(existing_initial_receipt)
                resumed_initial_materialisation_template_ids.append(template_id)
            elif migrate_older_seed_versions:
                try:
                    current_authority_payload = _live_template_authority_payload(
                        concept_id=concept_id,
                        template_id=template_id,
                    )
                    current_profile = dict(
                        current_authority_payload.get("profile") or {}
                    )
                    current_profile_row = _text_row_for_predicates(
                        concept_id,
                        _TEMPLATE_PROFILE_PREDICATES,
                    )
                except ValueError as exc:
                    skipped_current_template_ids.append(template_id)
                    errors_by_template_id[template_id] = str(exc)
                    continue
                current_version = _clean_text(
                    current_profile.get(WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD)
                )
                current_version_key = _template_seed_version_key(current_version)
                observed_authority_sha256 = _stable_payload_sha256(
                    current_authority_payload
                )
                known_digests_by_version = (
                    known_legacy_digests_by_template_and_version.get(
                        template_id,
                        {},
                    )
                )
                known_digests = known_digests_by_version.get(
                    current_version_key,
                    frozenset(),
                )
                migration_profile_context = (
                    dict(current_profile_row.get("context") or {})
                    if isinstance(current_profile_row, Mapping)
                    and isinstance(current_profile_row.get("context"), Mapping)
                    else {}
                )
                pending_receipt = migration_profile_context.get(
                    _SEED_MIGRATION_RECEIPT_CONTEXT_KEY
                )
                pending_receipt_valid = (
                    _pending_template_seed_migration_receipt_is_valid(
                        pending_receipt,
                        target_seed_version=repo_seed_version,
                        target_authority_payload_sha256=(
                            expected_authority_sha256
                        ),
                        target_authority_payload=expected_authority_payload,
                        current_authority_payload_sha256=(
                            observed_authority_sha256
                        ),
                        known_digests_by_version=known_digests_by_version,
                    )
                )
                current_numeric_version = _numeric_seed_version(current_version)
                repo_numeric_version = _numeric_seed_version(repo_seed_version)
                if pending_receipt_valid and isinstance(pending_receipt, Mapping):
                    migration_pending = True
                    known_legacy_migration_pending = True
                    migration_source_version_key = _clean_text(
                        pending_receipt.get("source_seed_version_key")
                    ).lower()
                    migration_source_authority_sha256 = _clean_text(
                        pending_receipt.get("source_authority_payload_sha256")
                    ).lower()
                    migration_source_authority_payload = dict(
                        pending_receipt.get("source_authority_payload") or {}
                    )
                    migration_safe_partial_digests = (
                        _safe_partial_template_migration_digests(
                            source_payload=migration_source_authority_payload,
                            target_payload=expected_authority_payload,
                        )
                    )
                elif observed_authority_sha256 in known_digests:
                    migration_pending = True
                    known_legacy_migration_pending = True
                    migration_source_version_key = current_version_key
                    migration_source_authority_sha256 = observed_authority_sha256
                    migration_source_authority_payload = current_authority_payload
                    migration_safe_partial_digests = (
                        _safe_partial_template_migration_digests(
                            source_payload=current_authority_payload,
                            target_payload=expected_authority_payload,
                        )
                    )
                elif (
                    current_numeric_version is not None
                    and repo_numeric_version is not None
                    and current_numeric_version > repo_numeric_version
                ):
                    skipped_current_template_ids.append(template_id)
                    continue
                elif (
                    current_numeric_version == repo_numeric_version
                    and observed_authority_sha256 == expected_authority_sha256
                ):
                    skipped_current_template_ids.append(template_id)
                    continue
                else:
                    error_code = (
                        "unversioned_authority_requires_explicit_migration"
                        if current_version_key == "unversioned"
                        else "legacy_authority_requires_explicit_migration"
                    )
                    blocker = {
                        "error_code": error_code,
                        "observed_seed_version_key": current_version_key,
                        "observed_authority_payload_sha256": (
                            observed_authority_sha256
                        ),
                        "known_legacy_authority_payload_sha256": sorted(
                            known_digests
                        ),
                        "pending_migration_receipt_present": isinstance(
                            pending_receipt,
                            Mapping,
                        ),
                        "pending_migration_receipt_valid": pending_receipt_valid,
                    }
                    migration_blockers_by_template_id[template_id] = blocker
                    if current_version_key == "unversioned":
                        unversioned_migration_blockers_by_template_id[
                            template_id
                        ] = blocker
                    elif (
                        _numeric_seed_version(current_version)
                        == _numeric_seed_version(repo_seed_version)
                    ):
                        seed_version_authority_mismatches_by_template_id[
                            template_id
                        ] = blocker
                    errors_by_template_id[template_id] = error_code
                    skipped_current_template_ids.append(template_id)
                    continue

                pending_receipt = _template_seed_migration_receipt(
                    status="pending",
                    source_seed_version_key=migration_source_version_key,
                    source_authority_payload_sha256=(
                        migration_source_authority_sha256
                    ),
                    source_authority_payload=migration_source_authority_payload,
                    target_seed_version=repo_seed_version,
                    target_authority_payload_sha256=expected_authority_sha256,
                    safe_partial_authority_payload_sha256=(
                        migration_safe_partial_digests
                    ),
                )
                migration_profile_context = {
                    **migration_profile_context,
                    _SEED_MIGRATION_RECEIPT_CONTEXT_KEY: pending_receipt,
                }
                try:
                    upsert_singleton_text_relation(
                        subject_concept_id=concept_id,
                        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
                        text=json.dumps(
                            current_profile,
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                        lang="en-NZ",
                        context=migration_profile_context,
                        garbage_collect=True,
                    )
                except Exception as exc:
                    errors_by_template_id[template_id] = (
                        f"workflow_template_migration_receipt_upsert_failed:{exc}"
                    )
                    continue

            try:
                _ensure_template_type_surface()
                if _ensure_template_instance_typing(concept_id):
                    typed_concept_ids.append(concept_id)
            except Exception as exc:
                errors_by_template_id[template_id] = (
                    f"workflow_template_type_enforcement_failed:{exc}"
                )
                continue

        try:
            authority_write_pending = bool(
                migration_pending or initial_materialisation_pending
            )
            migration_receipt_payload = (
                dict(pending_receipt)
                if authority_write_pending and isinstance(pending_receipt, Mapping)
                else {}
            )
            if authority_write_pending and not migration_receipt_payload:
                raise RuntimeError("workflow_template_migration_receipt_missing")
            context = {
                "template_id": template_id,
                "source": str(seed_bundle.get("family_id") or "workflow_template_seed"),
                "repo_seed_version": repo_seed_version or None,
            }
            if authority_write_pending:
                preserved_profile_context = {
                    key: value
                    for key, value in migration_profile_context.items()
                    if key != _SEED_MIGRATION_RECEIPT_CONTEXT_KEY
                }
                context = {
                    **preserved_profile_context,
                    **context,
                }
            profile_context = dict(context)
            if authority_write_pending:
                profile_context[_SEED_MIGRATION_RECEIPT_CONTEXT_KEY] = (
                    migration_receipt_payload
                )
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=WORKFLOW_TEMPLATE_ID_PREDICATE,
                text=template_id,
                lang="en-NZ",
                context=dict(context),
                garbage_collect=True,
            )
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
                text=json.dumps(
                    dict(template.get("workflow_spec_template") or {}),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                lang="en-NZ",
                context=dict(context),
                garbage_collect=True,
            )
            description = _clean_text(template.get("description"))
            if description:
                upsert_singleton_text_relation(
                    subject_concept_id=concept_id,
                    predicate="hasDescription",
                    text=description,
                    lang="en-NZ",
                    context=dict(context),
                    garbage_collect=True,
                )
            default_description = _clean_text(
                template.get("default_workflow_description")
            )
            if default_description:
                upsert_singleton_text_relation(
                    subject_concept_id=concept_id,
                    predicate=WORKFLOW_TEMPLATE_DEFAULT_DESCRIPTION_PREDICATE,
                    text=default_description,
                    lang="en-NZ",
                    context=dict(context),
                    garbage_collect=True,
                )
            # The version-bearing profile is deliberately the final authority
            # write. A failure before this point leaves the source version in
            # place, while the pending receipt makes the partial state safely
            # resumable on the next bootstrap.
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
                text=json.dumps(dict(profile), ensure_ascii=True, sort_keys=True),
                lang="en-NZ",
                context=profile_context,
                garbage_collect=True,
            )
            if authority_write_pending:
                observed_authority_payload = _live_template_authority_payload(
                    concept_id=concept_id,
                    template_id=template_id,
                )
                expected_sha256 = expected_authority_sha256
                observed_sha256 = _stable_payload_sha256(
                    observed_authority_payload
                )
                migration_readback = {
                    "verified": expected_sha256 == observed_sha256,
                    "expected_seed_version": repo_seed_version or None,
                    "observed_seed_version": _clean_text(
                        dict(observed_authority_payload.get("profile") or {}).get(
                            WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD
                        )
                    )
                    or None,
                    "expected_authority_payload_sha256": expected_sha256,
                    "observed_authority_payload_sha256": observed_sha256,
                }
                migration_readback_by_template_id[template_id] = migration_readback
                if migration_readback["verified"] is not True:
                    errors_by_template_id[template_id] = (
                        "workflow_template_migration_readback_mismatch"
                    )
                    continue
                verified_receipt = {
                    **migration_receipt_payload,
                    "status": "verified",
                }
                try:
                    upsert_singleton_text_relation(
                        subject_concept_id=concept_id,
                        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
                        text=json.dumps(
                            dict(profile),
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                        lang="en-NZ",
                        context={
                            **context,
                            _SEED_MIGRATION_RECEIPT_CONTEXT_KEY: verified_receipt,
                        },
                        garbage_collect=True,
                    )
                except Exception as exc:
                    errors_by_template_id[template_id] = (
                        f"workflow_template_migration_receipt_verify_failed:{exc}"
                    )
                    continue
                if initial_materialisation_pending:
                    latest_concept = _safe_get_concept(concept_id) or {}
                    latest_attributes = (
                        dict(latest_concept.get("attributes") or {})
                        if isinstance(latest_concept.get("attributes"), Mapping)
                        else {}
                    )
                    latest_attributes[
                        _SEED_INITIAL_MATERIALISATION_RECEIPT_ATTRIBUTE
                    ] = {
                        **migration_receipt_payload,
                        "status": "verified",
                    }
                    try:
                        concept_service.update_concept(
                            concept_id,
                            {"attributes": latest_attributes},
                        )
                    except Exception as exc:
                        errors_by_template_id[template_id] = (
                            "workflow_template_initial_materialisation_receipt_"
                            f"verify_failed:{exc}"
                        )
                        continue
            persisted_template_ids.append(template_id)
            if migration_pending:
                migrated_template_ids.append(template_id)
                if known_legacy_migration_pending:
                    migrated_known_legacy_template_ids.append(template_id)
        except Exception as exc:
            errors_by_template_id[template_id] = (
                f"workflow_template_seed_persist_failed:{exc}"
            )

    clear_workflow_template_bundle_cache()
    return {
        "success": not errors_by_template_id,
        "requested_template_ids": list(requested_ids),
        "created_concept_ids": created_concept_ids,
        "typed_concept_ids": typed_concept_ids,
        "persisted_template_ids": persisted_template_ids,
        "migrated_template_ids": migrated_template_ids,
        "migrated_known_legacy_template_ids": (
            migrated_known_legacy_template_ids
        ),
        "resumed_initial_materialisation_template_ids": (
            resumed_initial_materialisation_template_ids
        ),
        "skipped_current_template_ids": skipped_current_template_ids,
        "migration_readback_by_template_id": migration_readback_by_template_id,
        "unversioned_migration_blockers_by_template_id": (
            unversioned_migration_blockers_by_template_id
        ),
        "migration_blockers_by_template_id": migration_blockers_by_template_id,
        "seed_version_authority_mismatches_by_template_id": (
            seed_version_authority_mismatches_by_template_id
        ),
        "repo_seed_version": repo_seed_version or None,
        "errors_by_template_id": errors_by_template_id,
        "counts": {
            "requested_templates": len(requested_ids),
            "created_concepts": len(created_concept_ids),
            "typed_concepts": len(typed_concept_ids),
            "persisted_templates": len(persisted_template_ids),
            "migrated_templates": len(migrated_template_ids),
            "migrated_known_legacy_templates": len(
                migrated_known_legacy_template_ids
            ),
            "resumed_initial_materialisations": len(
                resumed_initial_materialisation_template_ids
            ),
            "skipped_current_templates": len(skipped_current_template_ids),
            "unversioned_migration_blockers": len(
                unversioned_migration_blockers_by_template_id
            ),
            "migration_blockers": len(migration_blockers_by_template_id),
            "errors": len(errors_by_template_id),
        },
    }


def _load_template_entry_from_vontology(
    concept_id: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    concept_doc = _safe_get_concept(concept_id)
    if concept_doc is None:
        raise ValueError(f"workflow_template_concept_missing:{concept_id}")

    template_id = _text_value_for_predicates(concept_id, _TEMPLATE_ID_PREDICATES)
    if not template_id:
        raise ValueError(f"workflow_template_id_missing:{concept_id}")

    raw_profile = _parse_json_mapping_text(
        _text_value_for_predicates(concept_id, _TEMPLATE_PROFILE_PREDICATES),
        error_code=f"workflow_template_profile_missing_or_invalid:{template_id}",
    )
    raw_template = _parse_json_mapping_text(
        _text_value_for_predicates(concept_id, _TEMPLATE_SPEC_PREDICATES),
        error_code=f"workflow_template_spec_missing_or_invalid:{template_id}",
    )
    profile = _normalise_template_profile(raw_profile, template_id=template_id)
    template = {
        "template_id": template_id,
        "concept_id": concept_id,
        "name": _concept_display_name(concept_doc, template_id),
        "description": _text_value_for_predicates(concept_id, _DESCRIPTION_PREDICATES)
        or _clean_text(concept_doc.get("description")),
        "default_workflow_description": _text_value_for_predicates(
            concept_id,
            _TEMPLATE_DEFAULT_DESCRIPTION_PREDICATES,
        )
        or _clean_text(raw_profile.get("default_workflow_description")),
        "workflow_spec_template": copy.deepcopy(raw_template),
        "required_variables": _iter_required_template_variables(raw_template),
    }
    return template_id, template, profile


@lru_cache(maxsize=1)
def _load_vontology_workflow_template_bundle_cached() -> dict[str, Any]:
    search_result = concept_search_service.search_concepts(
        "",
        filter_kind=["individual"],
        instance_of=WORKFLOW_TEMPLATE_TYPE_ID,
        direct_instances_only=True,
        limit=200,
    )
    concept_ids = tuple(
        sorted(
            str(item.get("concept_id") or "").strip()
            for item in (search_result.get("results") or [])
            if isinstance(item, Mapping)
            and isinstance(item.get("concept_id"), str)
            and str(item.get("concept_id")).strip()
        )
    )

    templates: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    errors_by_concept_id: dict[str, str] = {}

    for concept_id in concept_ids:
        try:
            template_id, template, profile = _load_template_entry_from_vontology(
                concept_id
            )
        except Exception as exc:
            errors_by_concept_id[concept_id] = str(exc)
            continue
        templates[template_id] = template
        profiles[template_id] = profile

    return {
        "source": "vontology",
        "family_id": "workflow_template_profiles",
        "template_concept_ids": list(concept_ids),
        "templates": templates,
        "profiles": profiles,
        "errors_by_concept_id": errors_by_concept_id,
    }


def load_workflow_template_bundle(
    asset_path: str | Path = DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
    *,
    auto_seed: bool = True,
    required_template_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Load the authoritative workflow-template bundle from Vontology.

    If the authoritative template concepts are missing, hydrate them from the
    repo-side seed bundle and retry once.
    """

    bundle = _load_vontology_workflow_template_bundle_cached()
    templates = dict(bundle.get("templates") or {})
    required_ids = tuple(
        str(item).strip()
        for item in (required_template_ids or ())
        if isinstance(item, str) and str(item).strip()
    )
    missing_required_ids = [
        template_id for template_id in required_ids if template_id not in templates
    ]
    if templates and not missing_required_ids:
        return bundle

    if auto_seed:
        seed_report = ensure_repo_seeded_workflow_template_bundle(
            asset_path=asset_path,
            template_ids=missing_required_ids or None,
        )
        bundle = _load_vontology_workflow_template_bundle_cached()
        enriched_bundle = dict(bundle)
        enriched_bundle["seed_report"] = seed_report
        templates = dict(enriched_bundle.get("templates") or {})
        return enriched_bundle if templates else enriched_bundle

    return bundle


def clear_workflow_template_bundle_cache() -> None:
    _load_repo_seed_workflow_template_bundle_cached.cache_clear()
    _load_vontology_workflow_template_bundle_cached.cache_clear()


def select_workflow_template(
    *,
    request_text: str,
    explicit_template_id: str | None = None,
    asset_path: str | Path = DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
) -> dict[str, Any]:
    bundle = load_workflow_template_bundle(
        asset_path,
        required_template_ids=[explicit_template_id] if explicit_template_id else None,
    )
    templates = dict(bundle.get("templates") or {})
    profiles = dict(bundle.get("profiles") or {})

    if explicit_template_id:
        template_id = _clean_text(explicit_template_id)
        template = templates.get(template_id)
        profile = profiles.get(template_id)
        if template is None or profile is None:
            raise ValueError(f"workflow_template_unknown:{template_id}")
        return {
            "template_id": template_id,
            "selection_source": "explicit",
            "selection_score": 1.0,
            "selection_signals": {},
            "template": template,
            "profile": profile,
        }

    query = _clean_text(request_text).lower()
    automatic_candidates: list[dict[str, Any]] = []
    fallback_candidates: list[dict[str, Any]] = []

    for template_id, template in templates.items():
        profile = profiles.get(template_id)
        if not isinstance(profile, Mapping):
            continue
        selection_mode = str(profile.get("selection_mode") or "automatic")
        if selection_mode == "explicit_only":
            continue

        required_all = _normalise_string_tuple(profile.get("required_terms_all"))
        required_any = _normalise_string_tuple(profile.get("required_terms_any"))
        forbidden_any = _normalise_string_tuple(profile.get("forbidden_terms_any"))

        if required_all and not all(
            _contains_term(query, item) for item in required_all
        ):
            continue
        if required_any and not any(
            _contains_term(query, item) for item in required_any
        ):
            continue
        if forbidden_any and any(_contains_term(query, item) for item in forbidden_any):
            continue

        lexical_score, selection_signals = _lexical_score(
            query_text=query,
            keywords=_normalise_string_tuple(profile.get("keywords")),
            exemplars=_normalise_string_tuple(profile.get("exemplars")),
            title=str(template.get("name") or template_id),
            description=str(template.get("description") or ""),
        )
        priority = _normalise_int(profile.get("priority"), default=0)
        selection = {
            "template_id": template_id,
            "selection_source": (
                "fallback" if selection_mode == "fallback" else "automatic"
            ),
            "selection_score": round(priority + lexical_score, 3),
            "selection_signals": selection_signals,
            "template": template,
            "profile": profile,
        }
        if selection_mode == "fallback":
            fallback_candidates.append(selection)
        else:
            automatic_candidates.append(selection)

    pool = automatic_candidates or fallback_candidates
    if not pool:
        raise ValueError("workflow_template_selection_failed")

    pool.sort(
        key=lambda item: (
            -float(item.get("selection_score") or 0.0),
            str(item.get("template_id") or ""),
        )
    )
    return pool[0]


def render_workflow_spec_template(
    *,
    template_id: str,
    variables: Mapping[str, Any],
    asset_path: str | Path = DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
) -> dict[str, Any]:
    bundle = load_workflow_template_bundle(
        asset_path,
        required_template_ids=[template_id],
    )
    templates = dict(bundle.get("templates") or {})
    template = templates.get(_clean_text(template_id))
    if not isinstance(template, Mapping):
        raise ValueError(f"workflow_template_unknown:{template_id}")

    rendered = _render_template_value(
        template.get("workflow_spec_template"),
        variables,
    )
    if not isinstance(rendered, Mapping):
        raise ValueError(f"workflow_template_render_invalid:{template_id}")
    return dict(rendered)


def resolve_workflow_spec_template(
    *,
    request_text: str,
    variables: Mapping[str, Any],
    explicit_template_id: str | None = None,
    asset_path: str | Path = DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = select_workflow_template(
        request_text=request_text,
        explicit_template_id=explicit_template_id,
        asset_path=asset_path,
    )
    template_id = str(selection.get("template_id") or "").strip()
    rendered = render_workflow_spec_template(
        template_id=template_id,
        variables=variables,
        asset_path=asset_path,
    )
    diagnostics = {
        "template_id": template_id,
        "selection_source": selection.get("selection_source"),
        "selection_score": selection.get("selection_score"),
        "selection_signals": dict(selection.get("selection_signals") or {}),
        "profile": dict(selection.get("profile") or {}),
        "template_concept_id": selection.get("template", {}).get("concept_id"),
        "default_workflow_description": str(
            selection.get("template", {}).get("default_workflow_description") or ""
        ).strip()
        or None,
        "required_variables": list(
            dict.fromkeys(selection.get("template", {}).get("required_variables") or ())
        ),
    }
    return rendered, diagnostics


__all__ = [
    "DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH",
    "WORKFLOW_CREATION_COMPANY_TEMPLATE_ID",
    "REPO_SEED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION",
    "WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID",
    "WORKFLOW_CREATION_EVENT_TEMPLATE_ID",
    "WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID",
    "WORKFLOW_CREATION_PERSON_TEMPLATE_ID",
    "WORKFLOW_CREATION_PLACE_TEMPLATE_ID",
    "WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID",
    "WORKFLOW_TEMPLATE_DEFAULT_DESCRIPTION_PREDICATE",
    "WORKFLOW_TEMPLATE_ID_PREDICATE",
    "WORKFLOW_TEMPLATE_PROFILE_PREDICATE",
    "WORKFLOW_TEMPLATE_PROFILE_SCHEMA_VERSION",
    "WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD",
    "WORKFLOW_TEMPLATE_SPEC_PREDICATE",
    "WORKFLOW_TEMPLATE_TYPE_ID",
    "clear_workflow_template_bundle_cache",
    "ensure_repo_seeded_workflow_template_bundle",
    "load_workflow_template_bundle",
    "render_workflow_spec_template",
    "resolve_workflow_spec_template",
    "select_workflow_template",
]
