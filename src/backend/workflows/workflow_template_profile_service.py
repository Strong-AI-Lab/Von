"""Authored workflow-template profile loading and selection helpers.

Workflow creation and gap recovery should resolve reusable workflow spec
templates through declarative authored metadata rather than workflow-family-
specific Python branches.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Mapping, Sequence

AUTHORED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION = (
    "authored_workflow_template_bundle.v1"
)
WORKFLOW_TEMPLATE_PROFILE_SCHEMA_VERSION = "workflow_template_profile.v1"
WORKFLOW_TEMPLATE_SOURCE_DIR = Path(__file__).with_name("authored_sources")
DEFAULT_TEMPLATE_ASSET_PATH = (
    WORKFLOW_TEMPLATE_SOURCE_DIR / "workflow_template_profiles.json"
)

WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID = "workflow_creation.default_marker"
WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID = "workflow_creation.scholarly_representation"
WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID = (
    "workflow_creation.phd_student_representation"
)
WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID = (
    "workflow_gap_recovery.candidate_execution"
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


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
        len(overlap) * 0.12 + len(keyword_hits) * 0.18 + len(phrase_hits) * 0.28
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


@lru_cache(maxsize=None)
def _load_authored_workflow_template_bundle_cached(asset_path: str) -> dict[str, Any]:
    path = Path(asset_path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("authored_workflow_template_bundle_not_mapping")

    schema_version = _clean_text(payload.get("schema_version"))
    if schema_version != AUTHORED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "authored_workflow_template_bundle_schema_unsupported:"
            f"{schema_version or 'missing'}"
        )

    raw_templates = payload.get("templates")
    if not isinstance(raw_templates, Sequence) or isinstance(raw_templates, str):
        raise ValueError("authored_workflow_template_bundle_templates_missing")

    templates: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for item in raw_templates:
        if not isinstance(item, Mapping):
            continue
        template_id = _clean_text(item.get("template_id"))
        if not template_id:
            raise ValueError("authored_workflow_template_id_missing")
        raw_template = item.get("workflow_spec_template")
        if not isinstance(raw_template, Mapping):
            raise ValueError(
                f"authored_workflow_template_spec_missing:{template_id}"
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
        }
        profiles[template_id] = profile

    return {
        "asset_path": str(path),
        "family_id": _clean_text(payload.get("family_id")),
        "templates": templates,
        "profiles": profiles,
    }


def load_authored_workflow_template_bundle(
    asset_path: str | Path = DEFAULT_TEMPLATE_ASSET_PATH,
) -> dict[str, Any]:
    return _load_authored_workflow_template_bundle_cached(
        str(Path(asset_path).resolve())
    )


def clear_authored_workflow_template_bundle_cache() -> None:
    _load_authored_workflow_template_bundle_cached.cache_clear()


def select_workflow_template(
    *,
    request_text: str,
    explicit_template_id: str | None = None,
    asset_path: str | Path = DEFAULT_TEMPLATE_ASSET_PATH,
) -> dict[str, Any]:
    bundle = load_authored_workflow_template_bundle(asset_path)
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
    asset_path: str | Path = DEFAULT_TEMPLATE_ASSET_PATH,
) -> dict[str, Any]:
    bundle = load_authored_workflow_template_bundle(asset_path)
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
    asset_path: str | Path = DEFAULT_TEMPLATE_ASSET_PATH,
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
    "AUTHORED_WORKFLOW_TEMPLATE_BUNDLE_SCHEMA_VERSION",
    "WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID",
    "WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID",
    "WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID",
    "WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID",
    "WORKFLOW_TEMPLATE_PROFILE_SCHEMA_VERSION",
    "clear_authored_workflow_template_bundle_cache",
    "load_authored_workflow_template_bundle",
    "render_workflow_spec_template",
    "resolve_workflow_spec_template",
    "select_workflow_template",
]
