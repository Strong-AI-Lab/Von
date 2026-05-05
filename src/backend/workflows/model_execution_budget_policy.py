"""Resolve model execution budget policy from Vontology model concepts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from .conversation_turn_llm_timeout import coerce_conversation_turn_llm_timeout_sec

logger = logging.getLogger(__name__)

MODEL_EXECUTION_BUDGET_POLICY_SCHEMA = "model_execution_budget_policy.v1"
MODEL_EXECUTION_BUDGET_POLICY_PREDICATES: tuple[str, ...] = (
    "#V#has_model_execution_budget_policy",
    "has_model_execution_budget_policy",
    "#V#hasModelExecutionBudgetPolicy",
    "hasModelExecutionBudgetPolicy",
)

MAX_COMPLETION_GATE_LOOP_ELAPSED_MS = 600_000
MAX_COMPLETION_GATE_LOOP_ATTEMPTS = 20
MAX_COMPLETION_GATE_NO_PROGRESS_LIMIT = 10


@dataclass(frozen=True)
class ModelExecutionBudgetPolicy:
    """Runtime budget hints authored on a model concept in Vontology."""

    model_concept_id: str
    source_predicate: str
    schema: str = MODEL_EXECUTION_BUDGET_POLICY_SCHEMA
    cost_class: str | None = None
    locality: str | None = None
    latency_class: str | None = None
    conversation_turn_llm_timeout_sec: float | None = None
    completion_gate_loop_max_elapsed_ms: int | None = None
    completion_gate_loop_max_attempts: int | None = None
    completion_gate_loop_no_progress_limit: int | None = None
    raw_policy: Mapping[str, Any] = field(default_factory=dict)

    def as_telemetry(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "model_concept_id": self.model_concept_id,
            "source_predicate": self.source_predicate,
        }
        for key in (
            "cost_class",
            "locality",
            "latency_class",
            "conversation_turn_llm_timeout_sec",
            "completion_gate_loop_max_elapsed_ms",
            "completion_gate_loop_max_attempts",
            "completion_gate_loop_no_progress_limit",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_provider(provider: Any) -> str | None:
    text = _safe_text(provider).lower()
    return text or None


def _split_provider_from_model(model: str) -> tuple[str | None, str]:
    cleaned = _safe_text(model).replace(": ", ":")
    if ":" not in cleaned:
        return None, cleaned
    provider, remainder = cleaned.split(":", 1)
    provider_token = provider.strip().lower()
    if provider_token in {"openai", "anthropic", "gemini", "ollama", "deepseek"}:
        return provider_token, remainder.strip()
    return None, cleaned


_NON_ALNUM_RUN_RE = re.compile(r"[^0-9a-z]+")


def _slug_token(value: str) -> str:
    return _NON_ALNUM_RUN_RE.sub("_", value.strip().lower()).strip("_")


def candidate_model_concept_ids(
    *, provider: str | None = None, model: str | None = None
) -> tuple[str, ...]:
    """Return deterministic concept-id candidates for a provider/model pair."""

    cleaned_model = _safe_text(model)
    if not cleaned_model:
        return ()

    model_provider, bare_model = _split_provider_from_model(cleaned_model)
    provider_token = _normalise_provider(provider) or model_provider
    model_slug = _slug_token(bare_model or cleaned_model)
    if not model_slug:
        return ()

    raw_candidates: list[str] = []
    if cleaned_model.lower().startswith("#v#"):
        raw_candidates.append(cleaned_model)
    if provider_token:
        raw_candidates.extend(
            [
                f"#V#{provider_token}_model_{model_slug}",
                f"#V#{provider_token}_{model_slug}",
            ]
        )
    raw_candidates.extend(
        [
            f"#V#model_{model_slug}",
            f"#V#{model_slug}_model",
            f"#V#{model_slug}",
        ]
    )

    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        canonical = canonicalise_vontology_concept_id(candidate)
        if canonical and canonical not in seen:
            seen.add(canonical)
            ordered.append(canonical)
    return tuple(ordered)


def _concept_exists(concept_id: str) -> bool:
    try:
        from ..db.repositories.concepts_repository import ConceptsRepository
        from ..vontology.code_concepts_registry import is_code_concept_id

        if is_code_concept_id(concept_id):
            return True
        return bool(ConceptsRepository.find_one({"concept_id": concept_id}, {"_id": 1}))
    except Exception as exc:
        logger.debug("Could not check model concept %s: %s", concept_id, exc)
        return False


def _coerce_bounded_int(
    value: Any,
    *,
    min_value: int,
    max_value: int,
) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < min_value:
        return None
    return max(min_value, min(max_value, parsed))


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def normalise_model_execution_budget_policy(
    raw_policy: Mapping[str, Any] | str | None,
    *,
    model_concept_id: str,
    source_predicate: str,
) -> ModelExecutionBudgetPolicy | None:
    """Coerce a Vontology JSON text relation into bounded runtime hints."""

    if isinstance(raw_policy, str):
        try:
            parsed = json.loads(raw_policy)
        except Exception:
            return None
    else:
        parsed = raw_policy
    if not isinstance(parsed, Mapping):
        return None

    schema = _safe_text(
        parsed.get("schema") or parsed.get("schema_version") or parsed.get("_schema")
    )
    if schema and schema != MODEL_EXECUTION_BUDGET_POLICY_SCHEMA:
        return None

    timeout_sec = coerce_conversation_turn_llm_timeout_sec(
        _first_present(
            parsed,
            "conversation_turn_llm_timeout_sec",
            "llm_timeout_sec",
            "timeout_sec",
        )
    )
    elapsed_ms = _coerce_bounded_int(
        _first_present(
            parsed,
            "completion_gate_loop_max_elapsed_ms",
            "completion_gate_elapsed_budget_ms",
            "max_elapsed_ms",
        ),
        min_value=1_000,
        max_value=MAX_COMPLETION_GATE_LOOP_ELAPSED_MS,
    )
    attempts = _coerce_bounded_int(
        _first_present(
            parsed,
            "completion_gate_loop_max_attempts",
            "completion_gate_max_attempts",
            "max_attempts",
        ),
        min_value=0,
        max_value=MAX_COMPLETION_GATE_LOOP_ATTEMPTS,
    )
    no_progress_limit = _coerce_bounded_int(
        _first_present(
            parsed,
            "completion_gate_loop_no_progress_limit",
            "completion_gate_no_progress_limit",
            "no_progress_limit",
        ),
        min_value=0,
        max_value=MAX_COMPLETION_GATE_NO_PROGRESS_LIMIT,
    )

    return ModelExecutionBudgetPolicy(
        model_concept_id=model_concept_id,
        source_predicate=source_predicate,
        cost_class=_safe_text(parsed.get("cost_class")) or None,
        locality=_safe_text(parsed.get("locality")) or None,
        latency_class=_safe_text(parsed.get("latency_class")) or None,
        conversation_turn_llm_timeout_sec=timeout_sec,
        completion_gate_loop_max_elapsed_ms=elapsed_ms,
        completion_gate_loop_max_attempts=attempts,
        completion_gate_loop_no_progress_limit=no_progress_limit,
        raw_policy=dict(parsed),
    )


def _load_policy_for_concept(concept_id: str) -> ModelExecutionBudgetPolicy | None:
    try:
        from ..services.text_value_service import get_texts_for_concept
    except Exception as exc:
        logger.debug("Text relation service unavailable for model policy: %s", exc)
        return None

    for predicate in MODEL_EXECUTION_BUDGET_POLICY_PREDICATES:
        try:
            rows = get_texts_for_concept(
                concept_id,
                predicate=predicate,
                limit=3,
                recent_first=True,
            )
        except Exception as exc:
            logger.debug(
                "Could not read model budget policy %s on %s: %s",
                predicate,
                concept_id,
                exc,
            )
            continue
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            policy = normalise_model_execution_budget_policy(
                row.get("text"),
                model_concept_id=concept_id,
                source_predicate=str(row.get("predicate") or predicate),
            )
            if policy is not None:
                return policy
    return None


@lru_cache(maxsize=128)
def resolve_model_execution_budget_policy(
    *, provider: str | None = None, model: str | None = None
) -> ModelExecutionBudgetPolicy | None:
    """Resolve the first Vontology-authored execution budget policy for a model."""

    for concept_id in candidate_model_concept_ids(provider=provider, model=model):
        if not _concept_exists(concept_id):
            continue
        policy = _load_policy_for_concept(concept_id)
        if policy is not None:
            return policy
    return None


def clear_model_execution_budget_policy_cache() -> None:
    resolve_model_execution_budget_policy.cache_clear()
