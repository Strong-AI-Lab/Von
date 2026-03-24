"""Vontology-backed workflow-template selection and rendering helpers.

Workflow creation and gap recovery should resolve reusable workflow-spec
templates from first-class Vontology artefacts. Repo-side workflow template
bundles remain seed fixtures only and are used only to hydrate missing
template concepts.
"""

from __future__ import annotations

import copy
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
WORKFLOW_TEMPLATE_REPO_SEED_DIR = Path(__file__).with_name("repo_seed_bundles")
DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH = (
    WORKFLOW_TEMPLATE_REPO_SEED_DIR / "workflow_template_seed_bundle.json"
)

WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID = "workflow_creation.default_marker"
WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID = "workflow_creation.scholarly_representation"
WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID = (
    "workflow_creation.phd_student_representation"
)
WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID = (
    "workflow_gap_recovery.candidate_execution"
)

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
    WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID: (
        "#V#workflow_template_workflow_gap_recovery_candidate_execution"
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
) -> dict[str, Any]:
    profile = dict(raw_profile) if isinstance(raw_profile, Mapping) else {}
    return {
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
            raise ValueError(
                f"workflow_template_variable_missing:{missing}"
            ) from exc
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


def _text_value_for_predicates(
    concept_id: str,
    predicates: Sequence[str],
) -> str | None:
    for predicate in predicates:
        rows = get_texts_for_concept(concept_id, predicate=predicate, limit=10)
        for row in rows:
            text = _clean_text((row or {}).get("text"))
            if text:
                return text
    return None


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

    for item in raw_templates:
        if not isinstance(item, Mapping):
            continue
        template_id = _clean_text(item.get("template_id"))
        if not template_id:
            raise ValueError("repo_seed_workflow_template_id_missing")
        raw_template = item.get("workflow_spec_template")
        if not isinstance(raw_template, Mapping):
            raise ValueError(
                f"repo_seed_workflow_template_spec_missing:{template_id}"
            )
        profile = _normalise_template_profile(
            item.get("template_profile"),
            template_id=template_id,
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
) -> dict[str, Any]:
    """Hydrate missing workflow-template concepts from the repo seed bundle."""

    seed_bundle = _load_repo_seed_workflow_template_bundle_cached(
        str(Path(asset_path).resolve())
    )
    templates = dict(seed_bundle.get("templates") or {})
    profiles = dict(seed_bundle.get("profiles") or {})
    requested_ids = (
        tuple(
            str(item).strip()
            for item in template_ids
            if isinstance(item, str) and str(item).strip()
        )
        if template_ids is not None
        else tuple(templates.keys())
    )

    _ensure_template_type_surface()

    created_concept_ids: list[str] = []
    typed_concept_ids: list[str] = []
    persisted_template_ids: list[str] = []
    errors_by_template_id: dict[str, str] = {}

    for template_id in requested_ids:
        template = templates.get(template_id)
        profile = profiles.get(template_id)
        if not isinstance(template, Mapping) or not isinstance(profile, Mapping):
            errors_by_template_id[template_id] = "workflow_template_seed_missing"
            continue

        concept_id = _template_concept_id(template_id)
        concept_doc = _safe_get_concept(concept_id)
        if concept_doc is None:
            try:
                concept_service.create_concept(
                    name=_clean_text(template.get("name")) or template_id,
                    concept_id=concept_id,
                    description=_clean_text(template.get("description")),
                    parent_concept_ids=[WORKFLOW_TEMPLATE_TYPE_ID],
                    create_as_instance=True,
                    visibility_scope_mode="global_general",
                )
                created_concept_ids.append(concept_id)
            except Exception as exc:
                errors_by_template_id[template_id] = (
                    f"workflow_template_concept_create_failed:{exc}"
                )
                continue
        else:
            try:
                if _ensure_template_instance_typing(concept_id):
                    typed_concept_ids.append(concept_id)
            except Exception as exc:
                errors_by_template_id[template_id] = (
                    f"workflow_template_type_enforcement_failed:{exc}"
                )
                continue

        try:
            context = {
                "template_id": template_id,
                "source": str(seed_bundle.get("family_id") or "workflow_template_seed"),
            }
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
                predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
                text=json.dumps(dict(profile), ensure_ascii=True, sort_keys=True),
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
            persisted_template_ids.append(template_id)
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
        "errors_by_template_id": errors_by_template_id,
        "counts": {
            "requested_templates": len(requested_ids),
            "created_concepts": len(created_concept_ids),
            "typed_concepts": len(typed_concept_ids),
            "persisted_templates": len(persisted_template_ids),
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

        if required_all and not all(_contains_term(query, item) for item in required_all):
            continue
        if required_any and not any(_contains_term(query, item) for item in required_any):
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
    "REPO_SEED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION",
    "WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID",
    "WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID",
    "WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID",
    "WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID",
    "WORKFLOW_TEMPLATE_DEFAULT_DESCRIPTION_PREDICATE",
    "WORKFLOW_TEMPLATE_ID_PREDICATE",
    "WORKFLOW_TEMPLATE_PROFILE_PREDICATE",
    "WORKFLOW_TEMPLATE_PROFILE_SCHEMA_VERSION",
    "WORKFLOW_TEMPLATE_SPEC_PREDICATE",
    "WORKFLOW_TEMPLATE_TYPE_ID",
    "clear_workflow_template_bundle_cache",
    "ensure_repo_seeded_workflow_template_bundle",
    "load_workflow_template_bundle",
    "render_workflow_spec_template",
    "resolve_workflow_spec_template",
    "select_workflow_template",
]
