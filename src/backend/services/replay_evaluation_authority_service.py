"""Vontology-backed authority for replay evaluation rubrics.

Replay runners may collect structural telemetry and smoke-test diagnostics, but
durable experiment observations need a represented evaluation result. This
module only loads and validates that represented authority surface; it does not
score replay arms or decide promotion policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

from .testing_workflow_contracts import EXPERIMENT_VERDICTS
from .text_value_service import get_texts_for_concept

REPLAY_EVALUATION_RUBRIC_CONCEPT_ID = (
    "#V#live_prompt_sampler_replay_evaluation_rubric_v1"
)
REPLAY_EVALUATION_RUBRIC_JSON_PREDICATE_ID = (
    "#V#has_replay_evaluation_rubric_json"
)
REPLAY_EVALUATION_RUBRIC_SCHEMA_VERSION = "replay_evaluation_rubric.v1"
REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION = (
    "represented_replay_evaluation_result.v1"
)
_DEFAULT_LANG = "en-NZ"


class ReplayEvaluationAuthorityUnavailable(RuntimeError):
    """Raised when represented replay-evaluation authority cannot be used."""


@dataclass(frozen=True)
class ReplayEvaluationRubric:
    """Parsed replay-evaluation rubric loaded from Vontology."""

    concept_id: str
    source_predicate: str
    raw_rubric: Mapping[str, Any]
    diagnostics: Mapping[str, Any]

    @property
    def rubric_id(self) -> str:
        return _safe_text(self.raw_rubric.get("rubric_id")) or self.concept_id

    @property
    def rubric_version(self) -> str:
        return _safe_text(self.raw_rubric.get("rubric_version")) or "unknown"

    @property
    def expected_result_schema_version(self) -> str:
        return (
            _safe_text(self.raw_rubric.get("expected_result_schema_version"))
            or REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION
        )

    @property
    def verdict_values(self) -> tuple[str, ...]:
        return _normalise_string_tuple(self.raw_rubric.get("verdict_values"))

    @property
    def authority_payload(self) -> dict[str, Any]:
        return {
            "rubric_concept_id": self.concept_id,
            "rubric_id": self.rubric_id,
            "rubric_version": self.rubric_version,
            "rubric_predicate_id": self.source_predicate,
            "expected_result_schema_version": self.expected_result_schema_version,
        }


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _normalise_string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _safe_text(item).lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return tuple(items)


def _validate_rubric_payload(payload: Mapping[str, Any]) -> None:
    schema_version = _safe_text(payload.get("schema_version"))
    if schema_version != REPLAY_EVALUATION_RUBRIC_SCHEMA_VERSION:
        raise ReplayEvaluationAuthorityUnavailable(
            "replay evaluation rubric has unsupported schema_version"
        )
    if not _safe_text(payload.get("rubric_id")):
        raise ReplayEvaluationAuthorityUnavailable(
            "replay evaluation rubric is missing rubric_id"
        )
    if not _safe_text(payload.get("rubric_version")):
        raise ReplayEvaluationAuthorityUnavailable(
            "replay evaluation rubric is missing rubric_version"
        )
    if not _safe_text(payload.get("expected_result_schema_version")):
        raise ReplayEvaluationAuthorityUnavailable(
            "replay evaluation rubric is missing expected_result_schema_version"
        )
    verdict_values = _normalise_string_tuple(payload.get("verdict_values"))
    if not verdict_values:
        raise ReplayEvaluationAuthorityUnavailable(
            "replay evaluation rubric has no verdict_values"
        )
    unsupported = sorted(set(verdict_values) - set(EXPERIMENT_VERDICTS))
    if unsupported:
        raise ReplayEvaluationAuthorityUnavailable(
            "replay evaluation rubric contains unsupported experiment verdicts: "
            + ", ".join(unsupported)
        )


def resolve_replay_evaluation_rubric(
    *,
    concept_id: str = REPLAY_EVALUATION_RUBRIC_CONCEPT_ID,
    predicate: str = REPLAY_EVALUATION_RUBRIC_JSON_PREDICATE_ID,
) -> ReplayEvaluationRubric:
    """Load the represented replay-evaluation rubric from Vontology."""

    rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate=predicate,
        lang=_DEFAULT_LANG,
        limit=1,
        recent_first=True,
    )
    if not rows:
        raise ReplayEvaluationAuthorityUnavailable(
            f"replay evaluation rubric JSON missing on {concept_id}"
        )
    raw_text = _safe_text((rows[0] or {}).get("text"))
    if not raw_text:
        raise ReplayEvaluationAuthorityUnavailable(
            f"replay evaluation rubric JSON empty on {concept_id}"
        )
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ReplayEvaluationAuthorityUnavailable(
            f"replay evaluation rubric JSON invalid on {concept_id}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ReplayEvaluationAuthorityUnavailable(
            f"replay evaluation rubric JSON is not an object on {concept_id}"
        )
    _validate_rubric_payload(payload)
    return ReplayEvaluationRubric(
        concept_id=concept_id,
        source_predicate=predicate,
        raw_rubric=dict(payload),
        diagnostics={
            "rubric_concept_id": concept_id,
            "rubric_predicate_id": predicate,
            "rubric_version": _safe_text(payload.get("rubric_version")),
        },
    )


def normalise_represented_replay_evaluation_result(
    value: Mapping[str, Any],
    *,
    rubric: ReplayEvaluationRubric,
) -> dict[str, Any]:
    """Validate a represented replay-evaluation result against a loaded rubric."""

    schema_version = _safe_text(value.get("schema_version"))
    expected_schema_version = rubric.expected_result_schema_version
    if schema_version != expected_schema_version:
        raise ReplayEvaluationAuthorityUnavailable(
            "represented replay evaluation result has unsupported schema_version"
        )
    result_rubric = _safe_text(value.get("rubric_concept_id"))
    if result_rubric and result_rubric != rubric.concept_id:
        raise ReplayEvaluationAuthorityUnavailable(
            "represented replay evaluation result references a different rubric"
        )
    verdict = _safe_text(value.get("verdict")).lower()
    if not verdict:
        raise ReplayEvaluationAuthorityUnavailable(
            "represented replay evaluation result is missing verdict"
        )
    if verdict not in rubric.verdict_values:
        raise ReplayEvaluationAuthorityUnavailable(
            "represented replay evaluation verdict is not authorised by the rubric"
        )
    if verdict not in EXPERIMENT_VERDICTS:
        raise ReplayEvaluationAuthorityUnavailable(
            "represented replay evaluation verdict is not an experiment verdict"
        )
    payload = {str(key): item for key, item in value.items() if isinstance(key, str)}
    payload["schema_version"] = schema_version
    payload["verdict"] = verdict
    payload["rubric_concept_id"] = result_rubric or rubric.concept_id
    payload.setdefault("rubric_id", rubric.rubric_id)
    payload.setdefault("rubric_version", rubric.rubric_version)
    return payload


__all__ = [
    "REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION",
    "REPLAY_EVALUATION_RUBRIC_CONCEPT_ID",
    "REPLAY_EVALUATION_RUBRIC_JSON_PREDICATE_ID",
    "REPLAY_EVALUATION_RUBRIC_SCHEMA_VERSION",
    "ReplayEvaluationAuthorityUnavailable",
    "ReplayEvaluationRubric",
    "normalise_represented_replay_evaluation_result",
    "resolve_replay_evaluation_rubric",
]
