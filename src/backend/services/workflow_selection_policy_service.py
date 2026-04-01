"""Learned policy support for workflow selection.

The Phase 3 selector already finds a relevant candidate set. Phase 4 adds a
lightweight learned policy on top of that recall step: selector experiences are
converted into workflow-level reward priors, token-level affinity signals, and
UCB-style exploration bonuses that can reorder the candidate list and provide
selector guidance, while the selector LLM remains the semantic routing
authority.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Mapping, Sequence

from .workflow_selection_experience import (
    SelectionExperienceTuple,
    extract_selection_query_tokens,
    list_selection_experiences,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _coerce_candidate_workflows(
    candidate_workflows: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in candidate_workflows or ():
        workflow_id = _safe_str(item.get("concept_id") or item.get("workflow_id"))
        if not workflow_id:
            continue
        rows.append(
            {
                "concept_id": workflow_id,
                "name": _safe_str(item.get("name")) or workflow_id,
                "description": _safe_str(item.get("description")),
            }
        )
    return rows


@dataclass(frozen=True)
class WorkflowSelectionPolicySnapshot:
    snapshot_id: str
    created_at: str
    experience_count: int
    completed_experience_count: int
    overall_average_reward: float
    workflow_stats: Dict[str, Dict[str, float]]
    token_stats: Dict[str, Dict[str, Dict[str, float]]]
    config: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_DEFAULT_CONFIG: dict[str, float] = {
    "minimum_completed_experiences": 6.0,
    "reward_weight": 0.6,
    "token_weight": 0.4,
    "exploration_weight": 0.18,
    "cold_start_bonus": 0.12,
    "max_tokens_per_workflow": 48.0,
}

_LEXICAL_GUIDANCE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "please",
        "run",
        "show",
        "start",
        "that",
        "the",
        "this",
        "to",
        "use",
        "what",
        "workflow",
    }
)

_policy_lock = Lock()
_live_policy_snapshot: WorkflowSelectionPolicySnapshot | None = None


def _coerce_experience_sequence(
    experiences: Sequence[SelectionExperienceTuple | Mapping[str, Any]] | None,
) -> list[SelectionExperienceTuple]:
    result: list[SelectionExperienceTuple] = []
    for item in experiences or ():
        if isinstance(item, SelectionExperienceTuple):
            result.append(item)
    return result


def clear_live_selection_policy() -> None:
    global _live_policy_snapshot
    with _policy_lock:
        _live_policy_snapshot = None


def set_live_selection_policy(
    snapshot: WorkflowSelectionPolicySnapshot | None,
) -> WorkflowSelectionPolicySnapshot | None:
    global _live_policy_snapshot
    with _policy_lock:
        _live_policy_snapshot = snapshot
    return snapshot


def get_live_selection_policy() -> WorkflowSelectionPolicySnapshot | None:
    with _policy_lock:
        snapshot = _live_policy_snapshot
    if snapshot is not None:
        return snapshot
    return refresh_live_selection_policy()


def train_selection_policy(
    *,
    experiences: Sequence[SelectionExperienceTuple | Mapping[str, Any]] | None = None,
    config_overrides: Mapping[str, float] | None = None,
) -> WorkflowSelectionPolicySnapshot | None:
    """Train a lightweight reward-guided routing policy from stored experiences."""

    config = dict(_DEFAULT_CONFIG)
    if isinstance(config_overrides, Mapping):
        for key, value in config_overrides.items():
            config[_safe_str(key)] = _coerce_float(value, default=config.get(key, 0.0))

    if experiences is None:
        items = list_selection_experiences(limit=5000, completed_only=True)
    else:
        items = _coerce_experience_sequence(experiences)

    completed = [
        item
        for item in items
        if item.reward is not None and _safe_str(item.selected_workflow_id)
    ]
    if len(completed) < int(config["minimum_completed_experiences"]):
        return None

    overall_average_reward = sum(float(item.reward or 0.0) for item in completed) / len(
        completed
    )
    workflow_totals: dict[str, dict[str, float]] = {}
    token_totals: dict[str, dict[str, dict[str, float]]] = {}

    for entry in completed:
        workflow_id = _safe_str(entry.selected_workflow_id)
        if not workflow_id:
            continue

        workflow_bucket = workflow_totals.setdefault(
            workflow_id,
            {"attempts": 0.0, "reward_sum": 0.0, "successes": 0.0},
        )
        workflow_bucket["attempts"] += 1.0
        workflow_bucket["reward_sum"] += float(entry.reward or 0.0)
        if entry.outcome in {"completed", "success"}:
            workflow_bucket["successes"] += 1.0

        tokens = entry.query_tokens or extract_selection_query_tokens(entry.query)
        per_workflow_tokens = token_totals.get(workflow_id)
        if per_workflow_tokens is None:
            per_workflow_tokens = {}
            token_totals[workflow_id] = per_workflow_tokens
        for token in tokens:
            token_metrics = per_workflow_tokens.setdefault(
                token,
                {"count": 0.0, "reward_sum": 0.0},
            )
            token_metrics["count"] += 1.0
            token_metrics["reward_sum"] += float(entry.reward or 0.0)

    workflow_stats: dict[str, dict[str, float]] = {}
    token_stats: dict[str, dict[str, dict[str, float]]] = {}
    max_tokens_per_workflow = max(1, int(config["max_tokens_per_workflow"]))

    for workflow_id, bucket in workflow_totals.items():
        attempts = max(1.0, bucket["attempts"])
        average_reward = bucket["reward_sum"] / attempts
        success_rate = bucket["successes"] / attempts
        workflow_stats[workflow_id] = {
            "attempts": float(attempts),
            "average_reward": round(average_reward, 6),
            "success_rate": round(success_rate, 6),
        }

        ranked_tokens = sorted(
            token_totals.get(workflow_id, {}).items(),
            key=lambda item: (
                -(item[1]["count"] * abs((item[1]["reward_sum"] / max(1.0, item[1]["count"])) - overall_average_reward)),
                -item[1]["count"],
                item[0],
            ),
        )
        token_bucket: dict[str, dict[str, float]] = {}
        for token, stats in ranked_tokens[:max_tokens_per_workflow]:
            count = max(1.0, stats["count"])
            token_bucket[token] = {
                "count": float(count),
                "average_reward": round(stats["reward_sum"] / count, 6),
            }
        token_stats[workflow_id] = token_bucket

    return WorkflowSelectionPolicySnapshot(
        snapshot_id=f"wsp_{_utcnow_iso().replace(':', '').replace('-', '')}",
        created_at=_utcnow_iso(),
        experience_count=len(items),
        completed_experience_count=len(completed),
        overall_average_reward=round(overall_average_reward, 6),
        workflow_stats=workflow_stats,
        token_stats=token_stats,
        config={key: round(value, 6) for key, value in config.items()},
    )


def refresh_live_selection_policy(
    *,
    experiences: Sequence[SelectionExperienceTuple | Mapping[str, Any]] | None = None,
    config_overrides: Mapping[str, float] | None = None,
) -> WorkflowSelectionPolicySnapshot | None:
    snapshot = train_selection_policy(
        experiences=experiences,
        config_overrides=config_overrides,
    )
    return set_live_selection_policy(snapshot)


def rank_policy_candidates(
    *,
    turn_text: str,
    candidate_workflows: Sequence[Mapping[str, Any]] | None,
    snapshot: WorkflowSelectionPolicySnapshot | None = None,
) -> list[dict[str, Any]]:
    """Rank candidate workflows using learned reward priors and exploration."""

    rows = _coerce_candidate_workflows(candidate_workflows)
    if not rows:
        return []

    policy = snapshot if snapshot is not None else get_live_selection_policy()
    if policy is None:
        return [
            {
                "workflow_id": row["concept_id"],
                "score": 0.0,
                "average_reward": 0.0,
                "token_signal": 0.0,
                "exploration_bonus": 0.0,
                "attempts": 0,
                "rank": index + 1,
                "reasoning": "No learned selection policy available yet.",
            }
            for index, row in enumerate(rows)
        ]

    tokens = extract_selection_query_tokens(turn_text)
    total_completed = max(1.0, float(policy.completed_experience_count))
    scores: list[dict[str, Any]] = []
    for row in rows:
        workflow_id = row["concept_id"]
        workflow_stats = policy.workflow_stats.get(workflow_id, {})
        attempts = max(0.0, _coerce_float(workflow_stats.get("attempts"), default=0.0))
        average_reward = _coerce_float(
            workflow_stats.get("average_reward"),
            default=policy.overall_average_reward,
        )
        token_entries = policy.token_stats.get(workflow_id, {})
        matching_token_rewards = [
            _coerce_float(token_entries[token]["average_reward"])
            for token in tokens
            if token in token_entries
        ]
        token_signal = (
            sum(matching_token_rewards) / len(matching_token_rewards)
            if matching_token_rewards
            else average_reward
        )
        exploration_bonus = _coerce_float(
            policy.config.get("exploration_weight", _DEFAULT_CONFIG["exploration_weight"])
        ) * math.sqrt(math.log(total_completed + 1.0) / (attempts + 1.0))
        if attempts <= 0.0:
            exploration_bonus += _coerce_float(
                policy.config.get("cold_start_bonus", _DEFAULT_CONFIG["cold_start_bonus"])
            )

        score = (
            _coerce_float(
                policy.config.get("reward_weight", _DEFAULT_CONFIG["reward_weight"])
            )
            * average_reward
            + _coerce_float(
                policy.config.get("token_weight", _DEFAULT_CONFIG["token_weight"])
            )
            * token_signal
            + exploration_bonus
        )
        reasoning_bits = [
            f"reward prior {average_reward:.2f}",
            f"evidence {int(attempts)}",
            f"exploration {exploration_bonus:.2f}",
        ]
        if matching_token_rewards:
            reasoning_bits.append(
                "token affinity "
                + ", ".join(token for token in tokens if token in token_entries)
            )
        scores.append(
            {
                "workflow_id": workflow_id,
                "score": round(score, 6),
                "average_reward": round(average_reward, 6),
                "token_signal": round(token_signal, 6),
                "exploration_bonus": round(exploration_bonus, 6),
                "attempts": int(attempts),
                "reasoning": "; ".join(reasoning_bits),
            }
        )

    scores.sort(
        key=lambda item: (
            -_coerce_float(item["score"]),
            -_coerce_float(item["average_reward"]),
            item["workflow_id"],
        )
    )
    for index, score in enumerate(scores, start=1):
        score["rank"] = index
    return scores


def _normalise_selector_text(value: Any) -> str:
    clean = _safe_str(value).lower()
    if not clean:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", clean).strip()


def _filtered_selector_tokens(value: Any, *, limit: int = 24) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in extract_selection_query_tokens(_safe_str(value), limit=limit * 3):
        if len(token) < 3 or token in _LEXICAL_GUIDANCE_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tuple(tokens)


def _selector_query_phrases(value: Any, *, max_phrases: int = 12) -> tuple[str, ...]:
    words = [token for token in _filtered_selector_tokens(value, limit=18)]
    phrases: list[str] = []
    seen: set[str] = set()
    for width in (3, 2):
        if len(words) < width:
            continue
        for index in range(len(words) - width + 1):
            phrase = " ".join(words[index : index + width])
            if phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
            if len(phrases) >= max_phrases:
                return tuple(phrases)
    return tuple(phrases)


def _recommend_guidance_candidate_by_specificity(
    *,
    turn_text: str,
    candidate_workflows: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    rows = _coerce_candidate_workflows(candidate_workflows)
    if len(rows) < 2:
        return None

    query_tokens = set(_filtered_selector_tokens(turn_text, limit=24))
    if len(query_tokens) < 2:
        return None
    query_phrases = _selector_query_phrases(turn_text)

    scores: list[dict[str, Any]] = []
    for row in rows:
        label_text = f"{row['concept_id']} {row['name']}"
        description_text = row.get("description") or ""
        label_tokens = set(_filtered_selector_tokens(label_text, limit=18))
        description_tokens = set(_filtered_selector_tokens(description_text, limit=36))
        label_overlap = tuple(sorted(label_tokens & query_tokens))
        description_overlap = tuple(
            sorted((description_tokens & query_tokens) - set(label_overlap))
        )
        normalised_label = _normalise_selector_text(label_text)
        phrase_hits = tuple(
            phrase
            for phrase in query_phrases
            if phrase and phrase in normalised_label
        )
        score = (
            len(label_overlap) * 3
            + len(description_overlap)
            + len(phrase_hits) * 4
        )
        scores.append(
            {
                "workflow_id": row["concept_id"],
                "score": score,
                "label_overlap": label_overlap,
                "description_overlap": description_overlap,
                "phrase_hits": phrase_hits,
            }
        )

    scores.sort(
        key=lambda item: (
            -int(item["score"]),
            -len(item["phrase_hits"]),
            -len(item["label_overlap"]),
            item["workflow_id"],
        )
    )
    if not scores:
        return None

    top = scores[0]
    second_score = int(scores[1]["score"]) if len(scores) > 1 else 0
    margin = int(top["score"]) - second_score
    label_overlap = tuple(top["label_overlap"])
    phrase_hits = tuple(top["phrase_hits"])
    if int(top["score"]) < 8 or len(label_overlap) < 2 or margin < 3:
        return None

    overlap_text = ", ".join(label_overlap[:4])
    phrase_text = ", ".join(phrase_hits[:3])
    reasoning = (
        f"Lexical selector guidance favours {top['workflow_id']} "
        f"(label overlap: {overlap_text or 'none'}"
    )
    if phrase_text:
        reasoning += f"; phrase hits: {phrase_text}"
    reasoning += f"; margin {margin})."
    confidence = min(
        0.96,
        0.74 + min(len(label_overlap), 4) * 0.04 + min(len(phrase_hits), 2) * 0.05,
    )
    return {
        "workflow_id": top["workflow_id"],
        "confidence_score": round(confidence, 6),
        "reasoning": reasoning,
        "selection_reason": "lexical_specificity_guidance_candidate",
        "candidate_scores": scores,
    }


def recommend_workflow_with_policy(
    *,
    turn_text: str,
    candidate_workflows: Sequence[Mapping[str, Any]] | None,
    snapshot: WorkflowSelectionPolicySnapshot | None = None,
) -> dict[str, Any]:
    """Return soft policy guidance for selector candidate ordering and telemetry."""

    lexical_guidance = _recommend_guidance_candidate_by_specificity(
        turn_text=turn_text,
        candidate_workflows=candidate_workflows,
    )
    scores = rank_policy_candidates(
        turn_text=turn_text,
        candidate_workflows=candidate_workflows,
        snapshot=snapshot,
    )
    policy = snapshot if snapshot is not None else get_live_selection_policy()
    if lexical_guidance is not None and policy is None:
        return {
            "policy_active": False,
            "guidance_mode": "prompt_guidance",
            "recommended_workflow_id": lexical_guidance["workflow_id"],
            "confidence_score": lexical_guidance["confidence_score"],
            "reasoning": lexical_guidance["reasoning"],
            "snapshot_id": None,
            "candidate_scores": scores,
            "ranked_candidate_ids": [
                item["workflow_id"]
                for item in lexical_guidance.get("candidate_scores", ())
            ],
            "guidance_basis": "lexical_specificity",
            "selection_reason": lexical_guidance["selection_reason"],
            "lexical_candidate_scores": list(
                lexical_guidance.get("candidate_scores", ())
            ),
        }

    if not scores or policy is None:
        return {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": scores,
            "ranked_candidate_ids": [item["workflow_id"] for item in scores],
        }

    top_score = scores[0]
    second_score_value = _coerce_float(scores[1]["score"]) if len(scores) > 1 else -1.0
    margin = _coerce_float(top_score["score"]) - second_score_value
    confidence = min(
        0.98,
        max(
            0.0,
            0.55 + max(0.0, margin) * 0.8 + min(int(top_score["attempts"]), 12) * 0.02,
        ),
    )
    reasoning = (
        f"Learned policy prefers {top_score['workflow_id']} "
        f"(margin {margin:.2f}; {top_score['reasoning']})."
    )
    return {
        "policy_active": True,
        "guidance_mode": "prompt_guidance",
        "recommended_workflow_id": top_score["workflow_id"],
        "confidence_score": round(confidence, 6),
        "reasoning": reasoning,
        "snapshot_id": policy.snapshot_id,
        "candidate_scores": scores,
        "ranked_candidate_ids": [item["workflow_id"] for item in scores],
        "selected_workflow_attempts": int(top_score["attempts"]),
        "selected_exploration_bonus": _coerce_float(top_score["exploration_bonus"]),
        "guidance_basis": "learned_policy",
        "lexical_guidance_workflow_id": (
            lexical_guidance["workflow_id"] if lexical_guidance is not None else None
        ),
        "lexical_guidance_confidence_score": (
            lexical_guidance["confidence_score"]
            if lexical_guidance is not None
            else None
        ),
        "lexical_guidance_reasoning": (
            lexical_guidance["reasoning"] if lexical_guidance is not None else None
        ),
        "lexical_candidate_scores": (
            list(lexical_guidance.get("candidate_scores", ()))
            if lexical_guidance is not None
            else []
        ),
    }


def evaluate_selection_policy(
    *,
    benchmark_cases: Sequence[Mapping[str, Any]],
    snapshot: WorkflowSelectionPolicySnapshot | None = None,
) -> dict[str, Any]:
    """Evaluate the learned policy against held-out benchmark cases."""

    policy = snapshot if snapshot is not None else get_live_selection_policy()
    if policy is None:
        return {
            "policy_accuracy": 0.0,
            "baseline_accuracy": 0.0,
            "accuracy_improvement": 0.0,
            "case_count": 0,
            "cases": [],
        }

    case_results: list[dict[str, Any]] = []
    baseline_correct = 0
    policy_correct = 0

    for case in benchmark_cases:
        expected_workflow_id = _safe_str(case.get("expected_workflow_id"))
        candidate_workflows = _coerce_candidate_workflows(case.get("candidate_workflows"))
        if not expected_workflow_id or not candidate_workflows:
            continue
        recommendation = recommend_workflow_with_policy(
            turn_text=_safe_str(case.get("turn_text")),
            candidate_workflows=candidate_workflows,
            snapshot=policy,
        )
        baseline_workflow_id = _safe_str(case.get("baseline_workflow_id"))
        if not baseline_workflow_id:
            baseline_workflow_id = candidate_workflows[0]["concept_id"]
        policy_workflow_id = _safe_str(recommendation.get("recommended_workflow_id"))

        baseline_hit = baseline_workflow_id == expected_workflow_id
        policy_hit = policy_workflow_id == expected_workflow_id
        baseline_correct += 1 if baseline_hit else 0
        policy_correct += 1 if policy_hit else 0
        case_results.append(
            {
                "turn_text": _safe_str(case.get("turn_text")),
                "expected_workflow_id": expected_workflow_id,
                "baseline_workflow_id": baseline_workflow_id,
                "policy_workflow_id": policy_workflow_id,
                "baseline_hit": baseline_hit,
                "policy_hit": policy_hit,
            }
        )

    case_count = len(case_results)
    if case_count == 0:
        return {
            "policy_accuracy": 0.0,
            "baseline_accuracy": 0.0,
            "accuracy_improvement": 0.0,
            "case_count": 0,
            "cases": [],
        }

    baseline_accuracy = baseline_correct / case_count
    policy_accuracy = policy_correct / case_count
    return {
        "policy_accuracy": round(policy_accuracy, 6),
        "baseline_accuracy": round(baseline_accuracy, 6),
        "accuracy_improvement": round(policy_accuracy - baseline_accuracy, 6),
        "case_count": case_count,
        "cases": case_results,
    }


__all__ = [
    "WorkflowSelectionPolicySnapshot",
    "clear_live_selection_policy",
    "evaluate_selection_policy",
    "get_live_selection_policy",
    "rank_policy_candidates",
    "recommend_workflow_with_policy",
    "refresh_live_selection_policy",
    "set_live_selection_policy",
    "train_selection_policy",
]
