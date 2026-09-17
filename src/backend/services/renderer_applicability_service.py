"""Deterministic resolver for Vontology-defined renderer applicability.

This service provides the first implementation slice for JVNAUTOSCI-1140:
it resolves candidate renderers against explicit applicability metadata and
emits inspectable diagnostics for selection and fallback behaviour.

Design notes:
- Resolver inputs support both concept-backed objects and transient KR payloads.
- Un-typed transient KR payloads are interpreted as transient microtheories by
  default, matching the current design direction in JVNAUTOSCI-1140.
- The service is pure and deterministic; ontology fetch/population is handled
  by callers so this module can be reused in API, MCP, and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

OBJECT_KIND_CONCEPT = "concept"
OBJECT_KIND_TRANSIENT_MICROTHEORY = "transient_microtheory"

_DEFAULT_OBJECT_KINDS = (
    OBJECT_KIND_CONCEPT,
    OBJECT_KIND_TRANSIENT_MICROTHEORY,
)

_SCREEN_ELEMENT_FAMILY_ALIASES: dict[str, str] = {
    "table": "table",
    "tabular": "table",
    "workflow": "workflow_view",
    "workflow_view": "workflow_view",
    "task": "task_view",
    "task_view": "task_view",
    "calendar": "calendar_view",
    "calendar_view": "calendar_view",
    "chart": "chart_view",
    "chart_view": "chart_view",
    "location": "location_view",
    "location_view": "location_view",
    "geo": "location_view",
    "map": "location_view",
    "document": "document_view",
    "document_view": "document_view",
    "citation": "document_view",
    "kanban": "kanban_view",
    "kanban_view": "kanban_view",
    "timeline": "timeline",
    "hierarchy": "hierarchy_view",
    "hierarchy_view": "hierarchy_view",
    "tree": "hierarchy_view",
    "relation_graph": "relation_graph_view",
    "relation_graph_view": "relation_graph_view",
    "graph": "relation_graph_view",
}
_ALLOWED_SCREEN_ELEMENT_FAMILIES: frozenset[str] = frozenset(
    {
        "image",
        "table",
        "workflow_view",
        "task_view",
        "calendar_view",
        "chart_view",
        "location_view",
        "document_view",
        "kanban_view",
        "timeline",
        "hierarchy_view",
        "relation_graph_view",
    }
)
_RENDERER_FILTERING_BOUNDARY_SCHEMA_VERSION = "renderer_filtering_boundary_v1"
_RENDERER_FILTERING_PREVIEW_LIMIT = 12


def _normalise_strings(values: Sequence[Any] | None) -> tuple[str, ...]:
    if not values:
        return ()
    normalised: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        normalised.append(text)
    return tuple(normalised)


def _coerce_sequence_values(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return ()
    if isinstance(value, Sequence):
        return value
    return ()


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_screen_element_families(values: Any) -> tuple[str, ...]:
    raw_values = _coerce_sequence_values(values)
    if not raw_values:
        return ()

    normalised: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        text = str(raw_value or "").strip().lower()
        if not text:
            continue
        mapped = _SCREEN_ELEMENT_FAMILY_ALIASES.get(text, text)
        if mapped not in _ALLOWED_SCREEN_ELEMENT_FAMILIES:
            continue
        if mapped in seen:
            continue
        seen.add(mapped)
        normalised.append(mapped)
    return tuple(normalised)


@dataclass(frozen=True)
class TransientMicrotheoryPayload:
    """Canonical transient microtheory payload for renderer resolution."""

    context_scope: str
    assertions: tuple[dict[str, Any], ...]
    theorems: tuple[dict[str, Any], ...]
    provenance: dict[str, Any]
    confidence: float | None
    episode_id: str | None
    temporal_metadata: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TransientMicrotheoryPayload:
        scope = str(raw.get("context_scope") or "").strip()
        if not scope:
            raise ValueError(
                "transient_microtheory.context_scope is required and must be non-empty"
            )

        assertions_raw = raw.get("assertions")
        assertions: list[dict[str, Any]] = []
        if isinstance(assertions_raw, list):
            assertions = [_coerce_mapping(item) for item in assertions_raw if isinstance(item, Mapping)]

        theorems_raw = raw.get("theorems")
        theorems: list[dict[str, Any]] = []
        if isinstance(theorems_raw, list):
            theorems = [_coerce_mapping(item) for item in theorems_raw if isinstance(item, Mapping)]

        provenance = _coerce_mapping(raw.get("provenance"))
        episode_id = str(raw.get("episode_id") or provenance.get("episode_id") or "").strip()
        if not episode_id:
            episode_id = None

        return cls(
            context_scope=scope,
            assertions=tuple(assertions),
            theorems=tuple(theorems),
            provenance=provenance,
            confidence=_coerce_float(raw.get("confidence")),
            episode_id=episode_id,
            temporal_metadata=_coerce_mapping(raw.get("temporal_metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_scope": self.context_scope,
            "assertions": [dict(item) for item in self.assertions],
            "theorems": [dict(item) for item in self.theorems],
            "provenance": dict(self.provenance),
            "confidence": self.confidence,
            "episode_id": self.episode_id,
            "temporal_metadata": dict(self.temporal_metadata),
        }


@dataclass(frozen=True)
class RendererProfile:
    """Renderer applicability metadata resolved from ontology definitions."""

    renderer_id: str
    renderer_type: str
    modalities: tuple[str, ...]
    applies_to_object_kinds: tuple[str, ...]
    applies_to_concept_type_ids: tuple[str, ...]
    required_predicates: tuple[str, ...]
    required_context_tags: tuple[str, ...]
    minimum_confidence: float | None
    priority: int
    fallback_renderer_ids: tuple[str, ...]
    screen_element_families: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RendererProfile:
        renderer_id = str(raw.get("renderer_id") or "").strip()
        if not renderer_id:
            raise ValueError("renderer_id is required for renderer applicability")

        renderer_type = str(raw.get("renderer_type") or renderer_id).strip()
        modalities_value = raw.get("modalities")
        if isinstance(modalities_value, str):
            modalities = _normalise_strings([modalities_value])
        else:
            modalities = _normalise_strings(_coerce_sequence_values(modalities_value))
        if not modalities:
            raise ValueError(f"renderer {renderer_id} must declare at least one modality")

        kinds = _normalise_strings(
            _coerce_sequence_values(raw.get("applies_to_object_kinds"))
            or _DEFAULT_OBJECT_KINDS
        )
        if not kinds:
            kinds = _DEFAULT_OBJECT_KINDS

        minimum_confidence = _coerce_float(raw.get("minimum_confidence"))
        priority_value = raw.get("priority", 0)
        try:
            priority = int(priority_value)
        except (TypeError, ValueError):
            priority = 0

        return cls(
            renderer_id=renderer_id,
            renderer_type=renderer_type,
            modalities=modalities,
            applies_to_object_kinds=kinds,
            applies_to_concept_type_ids=_normalise_strings(
                _coerce_sequence_values(raw.get("applies_to_concept_type_ids"))
            ),
            required_predicates=_normalise_strings(
                _coerce_sequence_values(raw.get("required_predicates"))
            ),
            required_context_tags=_normalise_strings(
                _coerce_sequence_values(raw.get("required_context_tags"))
            ),
            minimum_confidence=minimum_confidence,
            priority=priority,
            fallback_renderer_ids=_normalise_strings(
                _coerce_sequence_values(raw.get("fallback_renderer_ids"))
            ),
            screen_element_families=_normalise_screen_element_families(
                raw.get("screen_element_families")
            ),
        )


@dataclass(frozen=True)
class RendererResolutionInput:
    """Normalised resolver input for concept-backed or transient objects."""

    object_kind: str | None
    concept_id: str | None
    concept_type_ids: tuple[str, ...]
    present_predicates: tuple[str, ...]
    context_tags: tuple[str, ...]
    confidence: float | None
    preferred_modalities: tuple[str, ...]
    transient_microtheory: TransientMicrotheoryPayload | None
    provenance: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RendererResolutionInput:
        object_kind = str(raw.get("object_kind") or "").strip() or None
        concept_id = str(raw.get("concept_id") or "").strip() or None
        transient_raw = raw.get("transient_microtheory")
        transient: TransientMicrotheoryPayload | None = None
        if isinstance(transient_raw, Mapping):
            transient = TransientMicrotheoryPayload.from_mapping(transient_raw)
        return cls(
            object_kind=object_kind,
            concept_id=concept_id,
            concept_type_ids=_normalise_strings(
                _coerce_sequence_values(raw.get("concept_type_ids"))
            ),
            present_predicates=_normalise_strings(
                _coerce_sequence_values(raw.get("present_predicates"))
            ),
            context_tags=_normalise_strings(
                _coerce_sequence_values(raw.get("context_tags"))
            ),
            confidence=_coerce_float(raw.get("confidence")),
            preferred_modalities=_normalise_strings(
                _coerce_sequence_values(raw.get("preferred_modalities"))
            ),
            transient_microtheory=transient,
            provenance=_coerce_mapping(raw.get("provenance")),
        )


@dataclass(frozen=True)
class RendererCandidateEvaluation:
    renderer_id: str
    applicable: bool
    rejection_reasons: tuple[str, ...]
    modalities: tuple[str, ...]
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "renderer_id": self.renderer_id,
            "applicable": self.applicable,
            "rejection_reasons": list(self.rejection_reasons),
            "modalities": list(self.modalities),
            "priority": self.priority,
        }


@dataclass(frozen=True)
class RendererSelection:
    renderer_id: str
    renderer_type: str
    modalities: tuple[str, ...]
    selection_reason: str
    fallback_renderer_ids: tuple[str, ...]
    screen_element_families: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "renderer_id": self.renderer_id,
            "renderer_type": self.renderer_type,
            "modalities": list(self.modalities),
            "selection_reason": self.selection_reason,
            "fallback_renderer_ids": list(self.fallback_renderer_ids),
            "screen_element_families": list(self.screen_element_families),
        }


@dataclass(frozen=True)
class RendererResolutionResult:
    interpreted_object_kind: str
    interpreted_as_transient_microtheory: bool
    selected_renderers: tuple[RendererSelection, ...]
    candidate_evaluations: tuple[RendererCandidateEvaluation, ...]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "interpreted_object_kind": self.interpreted_object_kind,
            "interpreted_as_transient_microtheory": self.interpreted_as_transient_microtheory,
            "selected_renderers": [item.to_dict() for item in self.selected_renderers],
            "candidate_evaluations": [item.to_dict() for item in self.candidate_evaluations],
            "diagnostics": dict(self.diagnostics),
        }


def _interpret_object_kind(
    request: RendererResolutionInput,
) -> tuple[str, bool]:
    object_kind = request.object_kind
    if object_kind:
        return object_kind, object_kind == OBJECT_KIND_TRANSIENT_MICROTHEORY
    if request.transient_microtheory is not None:
        return OBJECT_KIND_TRANSIENT_MICROTHEORY, True
    return OBJECT_KIND_CONCEPT, False


def _evaluate_profile(
    profile: RendererProfile,
    *,
    object_kind: str,
    concept_type_ids: set[str],
    present_predicates: set[str],
    context_tags: set[str],
    confidence: float | None,
) -> RendererCandidateEvaluation:
    reasons: list[str] = []
    if object_kind not in set(profile.applies_to_object_kinds):
        reasons.append("object_kind_not_supported")

    if (
        object_kind == OBJECT_KIND_CONCEPT
        and profile.applies_to_concept_type_ids
        and concept_type_ids.isdisjoint(set(profile.applies_to_concept_type_ids))
    ):
        reasons.append("concept_type_not_supported")

    required_predicates = set(profile.required_predicates)
    if required_predicates and not required_predicates.issubset(present_predicates):
        reasons.append("required_predicates_missing")

    required_context_tags = set(profile.required_context_tags)
    if required_context_tags and not required_context_tags.issubset(context_tags):
        reasons.append("required_context_tags_missing")

    if profile.minimum_confidence is not None:
        effective_confidence = confidence if confidence is not None else 0.0
        if effective_confidence < profile.minimum_confidence:
            reasons.append("confidence_below_threshold")

    return RendererCandidateEvaluation(
        renderer_id=profile.renderer_id,
        applicable=len(reasons) == 0,
        rejection_reasons=tuple(reasons),
        modalities=profile.modalities,
        priority=profile.priority,
    )


def _select_renderers(
    eligible_profiles: Sequence[RendererProfile],
    *,
    preferred_modalities: tuple[str, ...],
    allow_multimodal: bool,
) -> tuple[RendererSelection, ...]:
    if not eligible_profiles:
        return ()

    selected: list[RendererSelection] = []
    selected_ids: set[str] = set()
    profile_by_id = {profile.renderer_id: profile for profile in eligible_profiles}

    if allow_multimodal and preferred_modalities:
        for modality in preferred_modalities:
            for profile in eligible_profiles:
                if profile.renderer_id in selected_ids:
                    continue
                if modality not in set(profile.modalities):
                    continue
                selected.append(
                    RendererSelection(
                        renderer_id=profile.renderer_id,
                        renderer_type=profile.renderer_type,
                        modalities=profile.modalities,
                        selection_reason=f"highest_priority_for_modality:{modality}",
                        fallback_renderer_ids=(),
                        screen_element_families=profile.screen_element_families,
                    )
                )
                selected_ids.add(profile.renderer_id)
                break

    if not selected:
        primary = eligible_profiles[0]
        selected.append(
            RendererSelection(
                renderer_id=primary.renderer_id,
                renderer_type=primary.renderer_type,
                modalities=primary.modalities,
                selection_reason="highest_priority_applicable_renderer",
                fallback_renderer_ids=(),
                screen_element_families=primary.screen_element_families,
            )
        )
        selected_ids.add(primary.renderer_id)

    enriched: list[RendererSelection] = []
    for item in selected:
        profile = profile_by_id[item.renderer_id]
        fallback_ids: list[str] = []
        for fallback_id in profile.fallback_renderer_ids:
            if fallback_id in profile_by_id and fallback_id not in selected_ids:
                fallback_ids.append(fallback_id)
        if not fallback_ids:
            item_modalities = set(item.modalities)
            for candidate in eligible_profiles:
                if candidate.renderer_id == item.renderer_id:
                    continue
                if candidate.renderer_id in selected_ids:
                    continue
                if item_modalities.intersection(set(candidate.modalities)):
                    fallback_ids.append(candidate.renderer_id)
                    break
        enriched.append(
            RendererSelection(
                renderer_id=item.renderer_id,
                renderer_type=item.renderer_type,
                modalities=item.modalities,
                selection_reason=item.selection_reason,
                fallback_renderer_ids=tuple(fallback_ids),
                screen_element_families=profile.screen_element_families,
            )
        )
    return tuple(enriched)


def _build_renderer_filtering_boundary(
    *,
    evaluations: Sequence[RendererCandidateEvaluation],
    selections: Sequence[RendererSelection],
) -> dict[str, Any]:
    rejection_reason_counts: dict[str, int] = {}
    rejected_preview: list[dict[str, Any]] = []
    applicable_preview: list[dict[str, Any]] = []

    for evaluation in evaluations:
        row = evaluation.to_dict()
        if evaluation.applicable:
            if len(applicable_preview) < _RENDERER_FILTERING_PREVIEW_LIMIT:
                applicable_preview.append(
                    {
                        "renderer_id": row.get("renderer_id"),
                        "priority": row.get("priority"),
                        "modalities": row.get("modalities", []),
                    }
                )
            continue

        reasons = [
            str(reason).strip()
            for reason in row.get("rejection_reasons", [])
            if isinstance(reason, str) and str(reason).strip()
        ]
        for reason in reasons:
            rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + 1
        if len(rejected_preview) < _RENDERER_FILTERING_PREVIEW_LIMIT:
            rejected_preview.append(
                {
                    "renderer_id": row.get("renderer_id"),
                    "rejection_reasons": reasons,
                    "priority": row.get("priority"),
                }
            )

    selected_preview = [
        {
            "renderer_id": item.renderer_id,
            "renderer_type": item.renderer_type,
            "selection_reason": item.selection_reason,
            "modalities": list(item.modalities),
        }
        for item in list(selections)[:_RENDERER_FILTERING_PREVIEW_LIMIT]
    ]

    applicable_count = sum(1 for evaluation in evaluations if evaluation.applicable)
    rejected_count = max(0, len(evaluations) - applicable_count)
    return {
        "schema_version": _RENDERER_FILTERING_BOUNDARY_SCHEMA_VERSION,
        "candidate_count": len(evaluations),
        "applicable_count": applicable_count,
        "rejected_count": rejected_count,
        "selected_count": len(selections),
        "rejection_reason_counts": dict(
            sorted(rejection_reason_counts.items(), key=lambda item: item[0])
        ),
        "applicable_preview": applicable_preview,
        "rejected_preview": rejected_preview,
        "selected_preview": selected_preview,
    }


def resolve_renderer_applicability(
    *,
    renderer_profiles: Sequence[RendererProfile],
    request: RendererResolutionInput,
    allow_multimodal: bool = True,
) -> RendererResolutionResult:
    """Resolve renderer applicability with deterministic diagnostics."""

    object_kind, interpreted_as_transient = _interpret_object_kind(request)
    concept_type_ids = set(request.concept_type_ids)
    present_predicates = set(request.present_predicates)
    context_tags = set(request.context_tags)

    ordered_profiles = sorted(
        renderer_profiles,
        key=lambda profile: (-profile.priority, profile.renderer_id),
    )

    evaluations: list[RendererCandidateEvaluation] = []
    eligible_profiles: list[RendererProfile] = []
    for profile in ordered_profiles:
        evaluation = _evaluate_profile(
            profile,
            object_kind=object_kind,
            concept_type_ids=concept_type_ids,
            present_predicates=present_predicates,
            context_tags=context_tags,
            confidence=request.confidence,
        )
        evaluations.append(evaluation)
        if evaluation.applicable:
            eligible_profiles.append(profile)

    selections = _select_renderers(
        eligible_profiles,
        preferred_modalities=request.preferred_modalities,
        allow_multimodal=allow_multimodal,
    )

    transient = request.transient_microtheory
    diagnostics = {
        "preferred_modalities": list(request.preferred_modalities),
        "candidate_count": len(renderer_profiles),
        "eligible_count": len(eligible_profiles),
        "selected_count": len(selections),
        "selection_rationale": (
            "No applicable renderer profile found"
            if not selections
            else "Selection determined by applicability constraints then priority"
        ),
        "transient_interpretation": {
            "interpreted_as_transient_microtheory": interpreted_as_transient,
            "episode_id": transient.episode_id if transient else None,
            "context_scope": transient.context_scope if transient else None,
            "provenance": dict(transient.provenance) if transient else dict(request.provenance),
        },
    }
    diagnostics["filtering_boundary"] = _build_renderer_filtering_boundary(
        evaluations=evaluations,
        selections=selections,
    )

    return RendererResolutionResult(
        interpreted_object_kind=object_kind,
        interpreted_as_transient_microtheory=interpreted_as_transient,
        selected_renderers=selections,
        candidate_evaluations=tuple(evaluations),
        diagnostics=diagnostics,
    )


def resolve_renderer_applicability_from_metadata(
    *,
    renderer_definitions: Sequence[Mapping[str, Any]],
    request_payload: Mapping[str, Any],
    allow_multimodal: bool = True,
) -> RendererResolutionResult:
    """Convenience wrapper for metadata payloads from ontology or MCP calls."""

    profiles = tuple(RendererProfile.from_mapping(item) for item in renderer_definitions)
    request = RendererResolutionInput.from_mapping(request_payload)
    return resolve_renderer_applicability(
        renderer_profiles=profiles,
        request=request,
        allow_multimodal=allow_multimodal,
    )
