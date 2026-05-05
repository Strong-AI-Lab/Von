"""Vontology-backed authority for paper recommendation policy profiles.

The repo seed is only a bootstrap fixture. At runtime, recommendation services read
policy from the Vontology policy profile concept and treat missing/invalid policy as
an unavailable authority surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .paper_recommendation_constants import (
    PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_POLICY_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
)
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_vontology_materialisation_helpers import ensure_instance_typing

_DEFAULT_LANG = "en-NZ"
_MANAGED_BY = "paper_recommendation_policy_authority_service"
_POLICY_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_recommendation_policy_profile_seed.json"
)


class PaperRecommendationPolicyUnavailable(RuntimeError):
    """Raised when the represented recommendation policy cannot be loaded."""


@dataclass(frozen=True)
class PaperRecommendationPolicy:
    """Parsed paper recommendation policy loaded from a Vontology concept."""

    concept_id: str
    source_predicate: str
    raw_policy: Mapping[str, Any]
    diagnostics: Mapping[str, Any]

    @property
    def policy_version(self) -> str:
        return _safe_str(self.raw_policy.get("policy_version")) or "unknown"

    def profile_field_specs(self) -> tuple[dict[str, Any], ...]:
        rows = self.raw_policy.get("profile_fields")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return ()
        specs: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            field = _safe_str(row.get("field"))
            if not field:
                continue
            kind = (_safe_str(row.get("kind")) or "text").casefold()
            if kind not in {"text", "string_list"}:
                kind = "text"
            specs.append(
                {
                    "field": field,
                    "kind": kind,
                    "label": _safe_str(row.get("label")) or field,
                }
            )
        return tuple(specs)

    def profile_field_label(self, field: str) -> str:
        clean_field = _safe_str(field) or ""
        for spec in self.profile_field_specs():
            if spec.get("field") == clean_field:
                return _safe_str(spec.get("label")) or clean_field
        return clean_field

    def feedback_label_specs(self) -> tuple[dict[str, Any], ...]:
        rows = self.raw_policy.get("feedback_labels")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return ()
        specs: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            label = _safe_str(row.get("label"))
            if not label:
                continue
            raw_score = row.get("score")
            if raw_score is None:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            aliases = _normalise_string_list(row.get("aliases"))
            specs.append({"label": label, "score": score, "aliases": aliases})
        return tuple(specs)

    def normalise_feedback_label(self, value: Any) -> tuple[str | None, float | None]:
        if value is None:
            return None, None
        specs = self.feedback_label_specs()
        if not specs:
            return None, None
        if isinstance(value, bool):
            return _feedback_label_for_numeric_score(1.0 if value else -1.0, specs)
        if isinstance(value, (int, float)):
            return _feedback_label_for_numeric_score(float(value), specs)
        cleaned = _safe_str(value)
        if not cleaned:
            return None, None
        alias_map: dict[str, tuple[str, float]] = {}
        for spec in specs:
            label = str(spec["label"])
            score = float(spec["score"])
            alias_map[_alias_key(label)] = (label, score)
            for alias in spec.get("aliases") or ():
                alias_map[_alias_key(alias)] = (label, score)
        return alias_map.get(_alias_key(cleaned), (None, None))

    def feedback_label_summary(self) -> str:
        labels = [str(spec["label"]) for spec in self.feedback_label_specs()]
        return ", ".join(labels) if labels else "configured feedback labels"

    def matching_string_tuple(self, name: str) -> tuple[str, ...]:
        matching = _mapping_value(self.raw_policy, "matching")
        return _normalise_string_list(matching.get(name))

    def paper_text_predicates(self, field: str) -> tuple[str, ...]:
        matching = _mapping_value(self.raw_policy, "matching")
        predicates = _mapping_value(matching, "paper_text_predicates")
        return _normalise_string_list(predicates.get(field))

    def context_label(self, group: str, key: str, default: str) -> str:
        matching = _mapping_value(self.raw_policy, "matching")
        labels = _mapping_value(matching, group)
        return _safe_str(labels.get(key)) or default

    @property
    def relationship_per_predicate_limit(self) -> int:
        matching = _mapping_value(self.raw_policy, "matching")
        return _int_value(matching.get("relationship_per_predicate_limit"), 6, minimum=1)

    @property
    def recommendation_settings(self) -> Mapping[str, Any]:
        return _mapping_value(self.raw_policy, "recommendation")

    @property
    def delivery_settings(self) -> Mapping[str, Any]:
        return _mapping_value(self.raw_policy, "delivery")

    @property
    def default_candidate_recall_limit(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("default_candidate_recall_limit"), 24, minimum=1)

    @property
    def max_candidate_recall_limit(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("max_candidate_recall_limit"), 100, minimum=1)

    @property
    def default_max_results(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("default_max_results"), 10, minimum=1)

    @property
    def max_max_results(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("max_max_results"), 50, minimum=1)

    @property
    def default_llm_candidate_limit(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("default_llm_candidate_limit"), 12, minimum=1)

    @property
    def min_recommendation_score(self) -> float:
        settings = self.recommendation_settings
        return _float_value(settings.get("min_score"), 0.55)

    @property
    def embedding_only_active_limit(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("embedding_only_active_limit"), 3, minimum=0)

    @property
    def summary_excerpt_chars(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("summary_excerpt_chars"), 280, minimum=40)

    @property
    def paper_context_excerpt_chars(self) -> int:
        settings = self.recommendation_settings
        return _int_value(settings.get("paper_context_excerpt_chars"), 4000, minimum=400)

    @property
    def placeholder_rationale_summary(self) -> str:
        settings = self.recommendation_settings
        return _safe_str(settings.get("placeholder_rationale_summary")) or ""

    @property
    def delivery_required_type_ids(self) -> tuple[str, ...]:
        return _normalise_string_list(self.delivery_settings.get("recipient_required_type_ids"))

    @property
    def delivery_candidate_seed_type_id(self) -> str | None:
        return _safe_str(self.delivery_settings.get("candidate_seed_type_id"))

    @property
    def delivery_organisation_predicate_id(self) -> str | None:
        return _safe_str(self.delivery_settings.get("organisation_predicate_id"))

    @property
    def default_max_recommendations_per_message(self) -> int:
        settings = self.delivery_settings
        return _int_value(settings.get("default_max_recommendations_per_message"), 3, minimum=1)

    @property
    def max_recommendations_per_message(self) -> int:
        settings = self.delivery_settings
        return _int_value(settings.get("max_recommendations_per_message"), 10, minimum=1)

    @property
    def rationale_unavailable_text(self) -> str:
        return _safe_str(self.delivery_settings.get("rationale_unavailable_text")) or ""

    @property
    def delivery_review_hint(self) -> str:
        return _safe_str(self.delivery_settings.get("review_hint")) or ""

    @property
    def delivery_trigger_source_default(self) -> str:
        return _safe_str(self.delivery_settings.get("trigger_source_default")) or "paper_recommendation_refresh"

    def delivery_recommendation_noun(self, count: int) -> str:
        key = "recommendation_noun_singular" if int(count or 0) == 1 else "recommendation_noun_plural"
        return _safe_str(self.delivery_settings.get(key)) or "paper recommendations"

    def delivery_message_subject(self, count: int) -> str:
        key = "message_subject_singular" if int(count or 0) == 1 else "message_subject_plural"
        return _safe_str(self.delivery_settings.get(key)) or "New paper recommendations from Von"

    def delivery_item_fields(self) -> tuple[dict[str, Any], ...]:
        rows = self.delivery_settings.get("item_fields")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return ()
        output: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            field = _safe_str(row.get("field"))
            label = _safe_str(row.get("label"))
            if not field or not label:
                continue
            output.append(
                {
                    "field": field,
                    "label": label,
                    "include_if_empty": bool(row.get("include_if_empty")),
                }
            )
        return tuple(output)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _safe_str(item)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return tuple(output)


def _mapping_value(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _int_value(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(int(minimum), parsed)
    if maximum is not None:
        parsed = min(int(maximum), parsed)
    return parsed


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _alias_key(value: Any) -> str:
    cleaned = _safe_str(value) or ""
    return " ".join(cleaned.replace("_", " ").replace("-", " ").casefold().split())


def _feedback_label_for_numeric_score(
    score: float,
    specs: Sequence[Mapping[str, Any]],
) -> tuple[str | None, float | None]:
    if not specs:
        return None, None
    if score > 0:
        candidates = sorted(specs, key=lambda row: float(row.get("score") or 0), reverse=True)
    elif score < 0:
        candidates = sorted(specs, key=lambda row: float(row.get("score") or 0))
    else:
        zero_candidates = [row for row in specs if float(row.get("score") or 0) == 0.0]
        candidates = zero_candidates or sorted(specs, key=lambda row: abs(float(row.get("score") or 0)))
    if not candidates:
        return None, None
    chosen = candidates[0]
    label = _safe_str(chosen.get("label"))
    if not label:
        return None, None
    return label, float(chosen.get("score") or 0.0)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _load_seed_policy_payload() -> dict[str, Any]:
    with _POLICY_SEED_ASSET_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise PaperRecommendationPolicyUnavailable("policy seed payload is not a JSON object")
    _validate_policy_payload(payload)
    return payload


def _validate_policy_payload(payload: Mapping[str, Any]) -> None:
    if not _safe_str(payload.get("schema_version")):
        raise PaperRecommendationPolicyUnavailable("policy payload is missing schema_version")
    if not _safe_str(payload.get("policy_version")):
        raise PaperRecommendationPolicyUnavailable("policy payload is missing policy_version")
    policy = PaperRecommendationPolicy(
        concept_id=PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
        source_predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
        raw_policy=payload,
        diagnostics={},
    )
    if not policy.profile_field_specs():
        raise PaperRecommendationPolicyUnavailable("policy payload has no profile_fields")
    if not policy.feedback_label_specs():
        raise PaperRecommendationPolicyUnavailable("policy payload has no feedback_labels")
    if not policy.paper_text_predicates("summary"):
        raise PaperRecommendationPolicyUnavailable("policy payload has no paper summary predicates")
    if not policy.delivery_required_type_ids:
        raise PaperRecommendationPolicyUnavailable("policy payload has no delivery recipient type policy")
    if not policy.delivery_item_fields():
        raise PaperRecommendationPolicyUnavailable("policy payload has no delivery item field policy")


def _ensure_concept(
    *,
    concept_id: str,
    name: str,
    description: str,
    parent_concept_ids: Sequence[str],
    create_as_instance: bool,
    created_ids: list[str],
) -> None:
    if _safe_get_concept(concept_id) is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=list(parent_concept_ids),
            create_as_instance=create_as_instance,
            visibility_scope_mode="global_general",
        )
        created_ids.append(concept_id)
    if create_as_instance:
        ensure_instance_typing(
            concept_id=concept_id,
            type_ids=parent_concept_ids,
        )


def ensure_paper_recommendation_policy_authority(
    *,
    force_seed_refresh: bool = False,
) -> dict[str, Any]:
    """Ensure the represented paper recommendation policy profile exists."""

    created_ids: list[str] = []
    errors: list[str] = []
    seeded_policy = False
    linked_workflow = False
    try:
        _ensure_concept(
            concept_id=PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
            name="Paper recommendation policy profile",
            description=(
                "Represented profile containing paper recommendation matching, "
                "delivery, feedback, and profile-form policy."
            ),
            parent_concept_ids=("#V#thing",),
            create_as_instance=True,
            created_ids=created_ids,
        )
        _ensure_concept(
            concept_id=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
            name="Has paper recommendation policy JSON",
            description="Links a recommendation policy profile to its structured JSON policy body.",
            parent_concept_ids=("#V#predicate", "#V#binary_predicate"),
            create_as_instance=True,
            created_ids=created_ids,
        )
        _ensure_concept(
            concept_id=PAPER_RECOMMENDATION_POLICY_LINK_PREDICATE_ID,
            name="Uses paper recommendation policy profile",
            description="Links the canonical recommendation workflow to its represented policy profile.",
            parent_concept_ids=("#V#predicate", "#V#binary_predicate"),
            create_as_instance=True,
            created_ids=created_ids,
        )

        existing_rows = get_texts_for_concept(
            subject_concept_id=PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
            predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
            limit=1,
        )
        if force_seed_refresh or not existing_rows:
            seed_payload = _load_seed_policy_payload()
            upsert_singleton_text_relation(
                subject_concept_id=PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
                predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
                lang=_DEFAULT_LANG,
                text=json.dumps(
                    seed_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                provenance={
                    "source": _MANAGED_BY,
                    "reason": "JVNAUTOSCI-2194 policy profile bootstrap",
                },
                context={"jira_issue": "JVNAUTOSCI-2194"},
                policy="replace_others",
                garbage_collect=True,
            )
            seeded_policy = True

        upsert_singleton_text_relation(
            subject_concept_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
            predicate=PAPER_RECOMMENDATION_POLICY_LINK_PREDICATE_ID,
            lang=_DEFAULT_LANG,
            text=PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
            provenance={
                "source": _MANAGED_BY,
                "reason": "canonical workflow policy profile link",
            },
            context={"jira_issue": "JVNAUTOSCI-2194"},
            policy="replace_others",
            garbage_collect=True,
        )
        linked_workflow = True
    except Exception as exc:
        errors.append(str(exc))

    return {
        "success": not errors,
        "policy_concept_id": PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
        "created_concept_ids": created_ids,
        "seeded_policy": seeded_policy,
        "linked_workflow": linked_workflow,
        "errors": errors,
    }


def _resolve_policy_concept_id() -> str:
    try:
        rows = get_texts_for_concept(
            subject_concept_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
            predicate=PAPER_RECOMMENDATION_POLICY_LINK_PREDICATE_ID,
            limit=1,
        )
    except Exception:
        rows = []
    if rows:
        linked = _safe_str((rows[0] or {}).get("text"))
        if linked and linked.startswith("#V#"):
            return linked
    return PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID


def resolve_paper_recommendation_policy(
    *,
    auto_materialise: bool = True,
) -> PaperRecommendationPolicy:
    """Load the represented paper recommendation policy from Vontology."""

    if auto_materialise:
        report = ensure_paper_recommendation_policy_authority()
        if not report.get("success"):
            raise PaperRecommendationPolicyUnavailable(
                "paper recommendation policy authority could not be ensured: "
                + "; ".join(str(item) for item in report.get("errors") or [])
            )
    policy_concept_id = _resolve_policy_concept_id()
    rows = get_texts_for_concept(
        subject_concept_id=policy_concept_id,
        predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
        limit=1,
    )
    if not rows:
        raise PaperRecommendationPolicyUnavailable(
            f"paper recommendation policy JSON missing on {policy_concept_id}"
        )
    raw_text = _safe_str((rows[0] or {}).get("text"))
    if not raw_text:
        raise PaperRecommendationPolicyUnavailable(
            f"paper recommendation policy JSON empty on {policy_concept_id}"
        )
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PaperRecommendationPolicyUnavailable(
            f"paper recommendation policy JSON invalid on {policy_concept_id}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise PaperRecommendationPolicyUnavailable(
            f"paper recommendation policy JSON is not an object on {policy_concept_id}"
        )
    _validate_policy_payload(payload)
    return PaperRecommendationPolicy(
        concept_id=policy_concept_id,
        source_predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
        raw_policy=dict(payload),
        diagnostics={
            "policy_concept_id": policy_concept_id,
            "policy_predicate_id": PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
            "policy_version": _safe_str(payload.get("policy_version")),
        },
    )


def normalise_profile_fields(
    value: Mapping[str, Any] | None,
    *,
    policy: PaperRecommendationPolicy | None = None,
) -> dict[str, Any]:
    active_policy = policy or resolve_paper_recommendation_policy()
    raw_profile = value if isinstance(value, Mapping) else {}
    output: dict[str, Any] = {}
    for spec in active_policy.profile_field_specs():
        field = str(spec["field"])
        if spec.get("kind") == "string_list":
            output[field] = list(_normalise_string_list(raw_profile.get(field)))
        else:
            output[field] = _safe_str(raw_profile.get(field)) or ""
    return output


def normalise_paper_matching_profile_overlay(
    value: Any,
    *,
    subject_concept_id: str,
    policy: PaperRecommendationPolicy | None = None,
) -> dict[str, Any]:
    active_policy = policy or resolve_paper_recommendation_policy()
    raw_profile = value if isinstance(value, Mapping) else {}
    profile: dict[str, Any] = {
        "schema_version": "paper_matching_profile.v1",
        "subject_concept_id": subject_concept_id,
        "updated_at": _safe_str(raw_profile.get("updated_at")) or None,
    }
    profile.update(normalise_profile_fields(raw_profile, policy=active_policy))
    return profile


def normalise_paper_recommendation_feedback_label(
    value: Any,
    *,
    policy: PaperRecommendationPolicy | None = None,
) -> tuple[str | None, float | None]:
    active_policy = policy or resolve_paper_recommendation_policy()
    return active_policy.normalise_feedback_label(value)


__all__ = [
    "PaperRecommendationPolicy",
    "PaperRecommendationPolicyUnavailable",
    "ensure_paper_recommendation_policy_authority",
    "normalise_paper_matching_profile_overlay",
    "normalise_paper_recommendation_feedback_label",
    "normalise_profile_fields",
    "resolve_paper_recommendation_policy",
]
