"""Policy-light contracts and aggregation for operational certification.

The durable scenario catalogue, evaluator policy, minefields, budgets, and
certification gates belong in a represented benchmark suite.  This module is a
generic support surface: it validates that represented contract, evaluates a
small set of structural matchers, and projects repeatable trial evidence.  It
does not contain domain scenarios, prompts, acceptance thresholds, or recovery
policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
from typing import Any


OPERATIONAL_CERTIFICATION_POLICY_SCHEMA_VERSION = "operational_certification_policy.v1"
OPERATIONAL_CERTIFICATION_SCENARIO_SCHEMA_VERSION = (
    "operational_certification_scenario.v1"
)
REPRESENTED_OPERATIONAL_EVALUATOR_RESULT_SCHEMA_VERSION = (
    "represented_operational_evaluator_result.v1"
)
OPERATIONAL_CERTIFICATION_TRIAL_RESULT_SCHEMA_VERSION = (
    "operational_certification_trial_result.v1"
)
OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION = (
    "operational_certification_campaign_result.v1"
)
OPERATIONAL_CERTIFICATION_CONTRACT_PROJECTION_SCHEMA_VERSION = (
    "operational_certification_contract_projection.v1"
)

_POLICY_KEYS = (
    "operational_certification_policy",
    "certification_policy",
)
_MATCHER_KINDS = frozenset({"exact", "subset", "exists", "count"})
_COMPARISON_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
_MISSING = object()


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def json_serialisable_projection(value: Any) -> Any:
    """Return a deterministic JSON-compatible copy or raise ``TypeError``.

    Silent ``default=str`` coercion makes evidence digests depend on incidental
    Python representations.  Certification evidence is therefore deliberately
    strict about the values it accepts.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("certification evidence contains a non-finite number")
        return value
    if isinstance(value, datetime):
        # PyMongo returns BSON datetimes as native ``datetime`` values. Preserve
        # their exact timezone-bearing or naive representation in a canonical,
        # JSON-compatible form instead of relying on incidental ``str`` output.
        return value.isoformat(timespec="microseconds")
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("certification evidence mapping keys must be strings")
            output[key] = json_serialisable_projection(item)
        return output
    if _is_sequence(value):
        return [json_serialisable_projection(item) for item in value]
    if hasattr(value, "to_projection") and callable(value.to_projection):
        return json_serialisable_projection(value.to_projection())
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return json_serialisable_projection(value.to_dict())
    raise TypeError(
        f"certification evidence contains a non-JSON value: {type(value).__name__}"
    )


def stable_payload_digest(value: Any) -> str:
    """Return a SHA-256 digest over a canonical JSON projection."""

    projection = json_serialisable_projection(value)
    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_operational_certification_campaign_result_integrity(
    value: Any,
) -> list[dict[str, Any]]:
    """Validate campaign identity, digest, gates, blockers, and verdict coherence.

    This is a hard evidence-interface check.  It does not reinterpret any
    represented matcher, budget, or operational threshold.
    """

    if not isinstance(value, Mapping):
        return [{"code": "campaign_result_mapping_required"}]
    report = json_serialisable_projection(value)
    errors: list[dict[str, Any]] = []
    if report.get("schema_version") != (
        OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION
    ):
        errors.append({"code": "campaign_result_schema_invalid"})
    if not _safe_text(report.get("suite_id")):
        errors.append({"code": "campaign_suite_id_missing"})
    contract_sha256 = _safe_text(report.get("contract_sha256"))
    if not (
        len(contract_sha256) == 64
        and all(
            character in "0123456789abcdef" for character in contract_sha256.lower()
        )
    ):
        errors.append({"code": "campaign_contract_digest_invalid"})
    certified = report.get("certified")
    if not isinstance(certified, bool):
        errors.append({"code": "campaign_certified_boolean_required"})

    raw_gates = report.get("certification_gate_results")
    gates = _mapping_sequence(raw_gates)
    if not _is_sequence(raw_gates) or not gates:
        errors.append({"code": "campaign_gate_results_required"})
    gate_ids: list[str] = []
    failed_gate_ids: list[str] = []
    for index, gate in enumerate(gates):
        gate_id = _safe_text(gate.get("gate_id"))
        if not gate_id:
            errors.append({"code": "campaign_gate_id_missing", "gate_index": index})
            continue
        if gate_id in gate_ids:
            errors.append({"code": "campaign_gate_id_duplicate", "gate_id": gate_id})
        gate_ids.append(gate_id)
        if not isinstance(gate.get("passed"), bool):
            errors.append({"code": "campaign_gate_passed_invalid", "gate_id": gate_id})
        elif gate.get("passed") is not True:
            failed_gate_ids.append(gate_id)
        observed_gate_digest = _safe_text(gate.get("result_sha256"))
        if observed_gate_digest:
            gate_without_digest = {
                key: item for key, item in gate.items() if key != "result_sha256"
            }
            if observed_gate_digest != stable_payload_digest(gate_without_digest):
                errors.append(
                    {"code": "campaign_gate_digest_mismatch", "gate_id": gate_id}
                )

    reported_failed_gate_ids = [
        _safe_text(item)
        for item in (report.get("failed_certification_gate_ids") or [])
        if _safe_text(item)
    ]
    if reported_failed_gate_ids != failed_gate_ids:
        errors.append(
            {
                "code": "campaign_failed_gate_projection_mismatch",
                "expected": failed_gate_ids,
                "observed": reported_failed_gate_ids,
            }
        )

    raw_blockers = report.get("blockers")
    blockers = _mapping_sequence(raw_blockers)
    if raw_blockers is not None and not _is_sequence(raw_blockers):
        errors.append({"code": "campaign_blockers_invalid"})
    blocking_blocker_count = sum(
        1 for blocker in blockers if blocker.get("blocking") is True
    )
    expected_certified = bool(
        gates
        and all(gate.get("passed") is True for gate in gates)
        and blocking_blocker_count == 0
    )
    if isinstance(certified, bool) and certified != expected_certified:
        errors.append(
            {
                "code": "campaign_verdict_inconsistent",
                "expected": expected_certified,
                "observed": certified,
            }
        )

    observed_report_digest = _safe_text(report.get("report_sha256"))
    report_without_digest = {
        key: item for key, item in report.items() if key != "report_sha256"
    }
    if not observed_report_digest:
        errors.append({"code": "campaign_report_digest_missing"})
    elif observed_report_digest != stable_payload_digest(report_without_digest):
        errors.append({"code": "campaign_report_digest_mismatch"})
    return errors


@dataclass(frozen=True)
class CertificationBlocker:
    """Machine-readable reason certification cannot safely be claimed."""

    code: str
    scope: str
    message: str
    recoverable: bool
    blocking: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "operational_certification_blocker.v1",
            "code": self.code,
            "scope": self.scope,
            "message": self.message,
            "recoverable": self.recoverable,
            "blocking": self.blocking,
            "details": json_serialisable_projection(self.details),
        }


class CertificationContractValidationError(ValueError):
    """Raised when represented certification authority is missing or invalid."""

    def __init__(self, blockers: Sequence[CertificationBlocker]):
        self.blockers = tuple(blockers)
        summary = "; ".join(blocker.code for blocker in self.blockers)
        super().__init__(summary or "operational_certification_contract_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": "operational_certification_contract_invalid",
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True)
class CertificationScenarioContract:
    scenario_id: str
    family_id: str
    depends_on: tuple[str, ...]
    execution: Mapping[str, Any]
    evaluator_specs: tuple[Mapping[str, Any], ...]
    checks: tuple[Mapping[str, Any], ...]
    minefields: tuple[Mapping[str, Any], ...]
    budgets: tuple[Mapping[str, Any], ...]
    initial_state: Mapping[str, Any]
    reset_policy: Mapping[str, Any]
    acceptable_goal_states: tuple[Mapping[str, Any], ...]
    milestone_dag: tuple[Mapping[str, Any], ...]
    permitted_effects: tuple[Mapping[str, Any], ...]
    target_scope: Mapping[str, Any]
    security_scope: Mapping[str, Any]
    fault_injection: Mapping[str, Any]
    metadata: Mapping[str, Any]
    contract_sha256: str

    def to_projection(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATIONAL_CERTIFICATION_SCENARIO_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "family_id": self.family_id,
            "depends_on": list(self.depends_on),
            "execution": json_serialisable_projection(self.execution),
            "evaluator_specs": json_serialisable_projection(self.evaluator_specs),
            "checks": json_serialisable_projection(self.checks),
            "minefields": json_serialisable_projection(self.minefields),
            "budgets": json_serialisable_projection(self.budgets),
            "initial_state": json_serialisable_projection(self.initial_state),
            "reset_policy": json_serialisable_projection(self.reset_policy),
            "acceptable_goal_states": json_serialisable_projection(
                self.acceptable_goal_states
            ),
            "milestone_dag": json_serialisable_projection(self.milestone_dag),
            "permitted_effects": json_serialisable_projection(self.permitted_effects),
            "target_scope": json_serialisable_projection(self.target_scope),
            "security_scope": json_serialisable_projection(self.security_scope),
            "fault_injection": json_serialisable_projection(self.fault_injection),
            "metadata": json_serialisable_projection(self.metadata),
            "contract_sha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class OperationalCertificationContract:
    suite_id: str
    suite_concept_id: str | None
    case_set: str
    policy: Mapping[str, Any]
    scenarios: tuple[CertificationScenarioContract, ...]
    topological_scenario_ids: tuple[str, ...]
    source: str | None
    source_definition_sha256: str
    contract_sha256: str

    @property
    def scenario_by_id(self) -> dict[str, CertificationScenarioContract]:
        return {scenario.scenario_id: scenario for scenario in self.scenarios}

    def to_projection(self) -> dict[str, Any]:
        return {
            "schema_version": (
                OPERATIONAL_CERTIFICATION_CONTRACT_PROJECTION_SCHEMA_VERSION
            ),
            "suite_id": self.suite_id,
            "suite_concept_id": self.suite_concept_id,
            "case_set": self.case_set,
            "source": self.source,
            "source_definition_sha256": self.source_definition_sha256,
            "policy": json_serialisable_projection(self.policy),
            "scenarios": [scenario.to_projection() for scenario in self.scenarios],
            "topological_scenario_ids": list(self.topological_scenario_ids),
            "contract_sha256": self.contract_sha256,
        }


def _blocker(
    code: str,
    scope: str,
    message: str,
    *,
    recoverable: bool,
    blocking: bool = True,
    details: Mapping[str, Any] | None = None,
) -> CertificationBlocker:
    return CertificationBlocker(
        code=code,
        scope=scope,
        message=message,
        recoverable=recoverable,
        blocking=blocking,
        details=dict(details or {}),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if not _is_sequence(value):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not _is_sequence(value):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _safe_text(item)
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return tuple(output)


def _normalise_path(path: Any) -> str:
    if path is None:
        return ""
    if not isinstance(path, str):
        raise ValueError("matcher_path_must_be_string")
    return path.strip()


def _path_parts(path: str) -> tuple[str, ...]:
    if not path:
        return ()
    if path.startswith("/"):
        return tuple(
            part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]
        )
    return tuple(part for part in path.split(".") if part)


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for part in _path_parts(path):
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        if _is_sequence(current):
            try:
                index = int(part)
            except ValueError:
                return _MISSING
            if index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
            continue
        return _MISSING
    return current


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return stable_payload_digest(left) == stable_payload_digest(right)
    except TypeError:
        return False


def _is_structural_subset(expected: Any, observed: Any) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(observed, Mapping):
            return False
        return all(
            key in observed and _is_structural_subset(item, observed[key])
            for key, item in expected.items()
        )
    if _is_sequence(expected):
        if not _is_sequence(observed):
            return False
        remaining = list(observed)
        for expected_item in expected:
            match_index = next(
                (
                    index
                    for index, observed_item in enumerate(remaining)
                    if _is_structural_subset(expected_item, observed_item)
                ),
                None,
            )
            if match_index is None:
                return False
            remaining.pop(match_index)
        return True
    return _json_equal(expected, observed)


def _compare_numbers(actual: int | float, operator: str, expected: int | float) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "lt":
        return actual < expected
    if operator == "lte":
        return actual <= expected
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    raise ValueError(f"unsupported_comparison_operator:{operator}")


def _container_count(value: Any) -> int | None:
    if isinstance(value, Mapping) or _is_sequence(value) or isinstance(value, str):
        return len(value)
    return None


def validate_matcher_spec(
    spec: Mapping[str, Any],
    *,
    scope: str,
) -> list[CertificationBlocker]:
    blockers: list[CertificationBlocker] = []
    matcher_id = _safe_text(spec.get("matcher_id"))
    if not matcher_id:
        blockers.append(
            _blocker(
                "matcher_id_missing",
                scope,
                "Represented matcher is missing matcher_id.",
                recoverable=True,
            )
        )
    kind = _safe_text(spec.get("kind")).lower()
    if kind not in _MATCHER_KINDS:
        blockers.append(
            _blocker(
                "matcher_kind_invalid",
                scope,
                "Represented matcher has an unsupported kind.",
                recoverable=True,
                details={"matcher_id": matcher_id or None, "kind": kind or None},
            )
        )
    try:
        _normalise_path(spec.get("path"))
    except ValueError:
        blockers.append(
            _blocker(
                "matcher_path_invalid",
                scope,
                "Represented matcher path must be a string.",
                recoverable=True,
                details={"matcher_id": matcher_id or None},
            )
        )
    if kind in {"exact", "subset", "exists", "count"} and "expected" not in spec:
        blockers.append(
            _blocker(
                "matcher_expected_value_missing",
                scope,
                "Represented matcher is missing its expected value.",
                recoverable=True,
                details={"matcher_id": matcher_id or None},
            )
        )
    if (
        kind == "exists"
        and "expected" in spec
        and not isinstance(spec.get("expected"), bool)
    ):
        blockers.append(
            _blocker(
                "exists_matcher_expected_value_invalid",
                scope,
                "Exists matcher expected value must be boolean.",
                recoverable=True,
                details={"matcher_id": matcher_id or None},
            )
        )
    if kind == "count":
        expected = spec.get("expected")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            blockers.append(
                _blocker(
                    "count_matcher_expected_value_invalid",
                    scope,
                    "Count matcher expected value must be a non-negative integer.",
                    recoverable=True,
                    details={"matcher_id": matcher_id or None},
                )
            )
        operator = _safe_text(spec.get("operator")).lower()
        if operator not in _COMPARISON_OPERATORS:
            blockers.append(
                _blocker(
                    "count_matcher_operator_invalid",
                    scope,
                    "Count matcher has an unsupported comparison operator.",
                    recoverable=True,
                    details={
                        "matcher_id": matcher_id or None,
                        "operator": operator or None,
                    },
                )
            )
    try:
        json_serialisable_projection(spec)
    except TypeError as exc:
        blockers.append(
            _blocker(
                "matcher_not_json_serialisable",
                scope,
                str(exc),
                recoverable=True,
                details={"matcher_id": matcher_id or None},
            )
        )
    return blockers


def evaluate_matcher(spec: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """Evaluate a neutral represented matcher against JSON-compatible evidence."""

    blockers = validate_matcher_spec(spec, scope="matcher")
    if blockers:
        raise CertificationContractValidationError(blockers)
    matcher_id = _safe_text(spec.get("matcher_id"))
    kind = _safe_text(spec.get("kind")).lower()
    path = _normalise_path(spec.get("path"))
    observed = _resolve_path(value, path)
    exists = observed is not _MISSING
    expected = spec.get("expected")
    observed_projection: Any = None
    actual_count: int | None = None
    reason_code: str | None = None

    if kind == "exists":
        matched = exists is expected
        observed_projection = exists
    elif not exists:
        matched = False
        reason_code = "matcher_path_missing"
    elif kind == "exact":
        matched = _json_equal(expected, observed)
        observed_projection = json_serialisable_projection(observed)
    elif kind == "subset":
        matched = _is_structural_subset(expected, observed)
        observed_projection = json_serialisable_projection(observed)
    else:
        count_expected = spec.get("expected")
        assert isinstance(count_expected, int) and not isinstance(count_expected, bool)
        actual_count = _container_count(observed)
        if actual_count is None:
            matched = False
            reason_code = "count_matcher_target_not_countable"
        else:
            matched = _compare_numbers(
                actual_count,
                _safe_text(spec.get("operator")).lower(),
                count_expected,
            )
        observed_projection = json_serialisable_projection(observed) if exists else None

    result = {
        "schema_version": "operational_certification_matcher_result.v1",
        "matcher_id": matcher_id,
        "kind": kind,
        "path": path,
        "matched": bool(matched),
        "path_exists": exists,
        "expected": json_serialisable_projection(expected),
        "observed": observed_projection,
        "actual_count": actual_count,
        "operator": _safe_text(spec.get("operator")).lower() or None,
        "reason_code": reason_code,
        "matcher_sha256": stable_payload_digest(spec),
    }
    result["result_sha256"] = stable_payload_digest(result)
    return result


def validate_budget_spec(
    spec: Mapping[str, Any],
    *,
    scope: str,
) -> list[CertificationBlocker]:
    blockers: list[CertificationBlocker] = []
    budget_id = _safe_text(spec.get("budget_id"))
    if not budget_id:
        blockers.append(
            _blocker(
                "budget_id_missing",
                scope,
                "Represented budget is missing budget_id.",
                recoverable=True,
            )
        )
    try:
        _normalise_path(spec.get("measurement_path"))
    except ValueError:
        blockers.append(
            _blocker(
                "budget_measurement_path_invalid",
                scope,
                "Represented budget measurement_path must be a string.",
                recoverable=True,
                details={"budget_id": budget_id or None},
            )
        )
    operator = _safe_text(spec.get("operator")).lower()
    if operator not in _COMPARISON_OPERATORS:
        blockers.append(
            _blocker(
                "budget_operator_invalid",
                scope,
                "Represented budget has an unsupported comparison operator.",
                recoverable=True,
                details={"budget_id": budget_id or None, "operator": operator or None},
            )
        )
    limit = spec.get("limit")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(float(limit))
    ):
        blockers.append(
            _blocker(
                "budget_limit_invalid",
                scope,
                "Represented budget limit must be a finite number.",
                recoverable=True,
                details={"budget_id": budget_id or None},
            )
        )
    if "blocking" not in spec or not isinstance(spec.get("blocking"), bool):
        blockers.append(
            _blocker(
                "budget_blocking_policy_missing",
                scope,
                "Represented budget must declare a boolean blocking policy.",
                recoverable=True,
                details={"budget_id": budget_id or None},
            )
        )
    return blockers


def evaluate_budget(spec: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """Evaluate a numeric budget whose threshold is supplied by represented policy."""

    blockers = validate_budget_spec(spec, scope="budget")
    if blockers:
        raise CertificationContractValidationError(blockers)
    path = _normalise_path(spec.get("measurement_path"))
    observed = _resolve_path(value, path)
    operator = _safe_text(spec.get("operator")).lower()
    limit = spec.get("limit")
    assert isinstance(limit, (int, float)) and not isinstance(limit, bool)
    actual: int | float | None = None
    reason_code: str | None = None
    if observed is _MISSING:
        within_budget = False
        reason_code = "budget_measurement_missing"
    elif isinstance(observed, bool) or not isinstance(observed, (int, float)):
        within_budget = False
        reason_code = "budget_measurement_not_numeric"
    elif not math.isfinite(float(observed)):
        within_budget = False
        reason_code = "budget_measurement_not_finite"
    else:
        actual = observed
        within_budget = _compare_numbers(actual, operator, limit)
    result = {
        "schema_version": "operational_certification_budget_result.v1",
        "budget_id": _safe_text(spec.get("budget_id")),
        "measurement_path": path,
        "operator": operator,
        "limit": limit,
        "actual": actual,
        "within_budget": bool(within_budget),
        "blocking": bool(spec.get("blocking")),
        "reason_code": reason_code,
        "budget_sha256": stable_payload_digest(spec),
    }
    result["result_sha256"] = stable_payload_digest(result)
    return result


def _validate_evaluator_spec(
    spec: Mapping[str, Any],
    *,
    scope: str,
) -> list[CertificationBlocker]:
    blockers: list[CertificationBlocker] = []
    evaluator_id = _safe_text(spec.get("evaluator_id"))
    if not evaluator_id:
        blockers.append(
            _blocker(
                "evaluator_id_missing",
                scope,
                "Represented evaluator specification is missing evaluator_id.",
                recoverable=True,
            )
        )
    if not _safe_text(spec.get("result_schema_version")):
        blockers.append(
            _blocker(
                "evaluator_result_schema_version_missing",
                scope,
                "Represented evaluator specification is missing result_schema_version.",
                recoverable=True,
                details={"evaluator_id": evaluator_id or None},
            )
        )
    allowed = _string_sequence(spec.get("allowed_verdicts"))
    passing = _string_sequence(spec.get("passing_verdicts"))
    if not allowed:
        blockers.append(
            _blocker(
                "evaluator_allowed_verdicts_missing",
                scope,
                "Represented evaluator must declare allowed_verdicts.",
                recoverable=True,
                details={"evaluator_id": evaluator_id or None},
            )
        )
    if not passing:
        blockers.append(
            _blocker(
                "evaluator_passing_verdicts_missing",
                scope,
                "Represented evaluator must declare passing_verdicts.",
                recoverable=True,
                details={"evaluator_id": evaluator_id or None},
            )
        )
    if passing and not set(passing).issubset(set(allowed)):
        blockers.append(
            _blocker(
                "evaluator_passing_verdict_not_allowed",
                scope,
                "Passing verdicts must be a subset of allowed verdicts.",
                recoverable=True,
                details={"evaluator_id": evaluator_id or None},
            )
        )
    if "evidence_required" not in spec or not isinstance(
        spec.get("evidence_required"), bool
    ):
        blockers.append(
            _blocker(
                "evaluator_evidence_policy_missing",
                scope,
                "Represented evaluator must declare evidence_required as boolean.",
                recoverable=True,
                details={"evaluator_id": evaluator_id or None},
            )
        )
    checks = spec.get("checks", [])
    if not _is_sequence(checks):
        blockers.append(
            _blocker(
                "evaluator_checks_invalid",
                scope,
                "Represented evaluator checks must be a list.",
                recoverable=True,
                details={"evaluator_id": evaluator_id or None},
            )
        )
    else:
        for index, check in enumerate(checks):
            if not isinstance(check, Mapping):
                blockers.append(
                    _blocker(
                        "evaluator_check_invalid",
                        scope,
                        "Represented evaluator check must be an object.",
                        recoverable=True,
                        details={"evaluator_id": evaluator_id or None, "index": index},
                    )
                )
                continue
            blockers.extend(
                validate_matcher_spec(
                    check,
                    scope=f"{scope}.evaluator[{evaluator_id}].check[{index}]",
                )
            )
    return blockers


def _normalise_scenario(
    raw_case: Mapping[str, Any],
    *,
    index: int,
) -> tuple[CertificationScenarioContract | None, list[CertificationBlocker]]:
    raw = _mapping(raw_case.get("operational_certification_scenario")) or dict(raw_case)
    scope = f"scenario[{index}]"
    blockers: list[CertificationBlocker] = []
    schema_version = _safe_text(raw.get("schema_version"))
    if schema_version != OPERATIONAL_CERTIFICATION_SCENARIO_SCHEMA_VERSION:
        blockers.append(
            _blocker(
                "scenario_schema_version_invalid",
                scope,
                "Scenario has an unsupported schema_version.",
                recoverable=True,
                details={"schema_version": schema_version or None},
            )
        )
    scenario_id = _safe_text(raw.get("scenario_id"))
    family_id = _safe_text(raw.get("family_id"))
    if not scenario_id:
        blockers.append(
            _blocker(
                "scenario_id_missing",
                scope,
                "Scenario is missing scenario_id.",
                recoverable=True,
            )
        )
    if not family_id:
        blockers.append(
            _blocker(
                "scenario_family_id_missing",
                scope,
                "Scenario is missing family_id.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
    raw_dependencies = raw.get("depends_on", [])
    depends_on = _string_sequence(raw_dependencies)
    if not _is_sequence(raw_dependencies):
        blockers.append(
            _blocker(
                "scenario_dependencies_invalid",
                scope,
                "Scenario depends_on must be a list.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
    if scenario_id and scenario_id in depends_on:
        blockers.append(
            _blocker(
                "scenario_self_dependency",
                scope,
                "Scenario cannot depend on itself.",
                recoverable=True,
                details={"scenario_id": scenario_id},
            )
        )

    execution = _mapping(raw.get("execution"))
    if not execution or not _safe_text(execution.get("adapter_id")):
        blockers.append(
            _blocker(
                "scenario_execution_adapter_missing",
                scope,
                "Scenario execution must name a represented adapter_id.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )

    raw_evaluators = raw.get("evaluator_specs")
    evaluators = _mapping_sequence(raw_evaluators)
    if not _is_sequence(raw_evaluators) or not evaluators:
        blockers.append(
            _blocker(
                "scenario_evaluator_specs_missing",
                scope,
                "Scenario must declare at least one represented evaluator.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
    evaluator_ids: list[str] = []
    for evaluator_index, evaluator in enumerate(evaluators):
        evaluator_id = _safe_text(evaluator.get("evaluator_id"))
        if evaluator_id in evaluator_ids:
            blockers.append(
                _blocker(
                    "duplicate_evaluator_id",
                    scope,
                    "Scenario evaluator IDs must be unique.",
                    recoverable=True,
                    details={
                        "scenario_id": scenario_id or None,
                        "evaluator_id": evaluator_id,
                    },
                )
            )
        evaluator_ids.append(evaluator_id)
        blockers.extend(
            _validate_evaluator_spec(
                evaluator,
                scope=f"{scope}.evaluator[{evaluator_index}]",
            )
        )

    checks = _mapping_sequence(raw.get("checks", []))
    if not _is_sequence(raw.get("checks", [])):
        blockers.append(
            _blocker(
                "scenario_checks_invalid",
                scope,
                "Scenario checks must be a list.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
    for check_index, check in enumerate(checks):
        blockers.extend(
            validate_matcher_spec(
                check,
                scope=f"{scope}.check[{check_index}]",
            )
        )

    minefields = _mapping_sequence(raw.get("minefields", []))
    if not _is_sequence(raw.get("minefields", [])):
        blockers.append(
            _blocker(
                "scenario_minefields_invalid",
                scope,
                "Scenario minefields must be a list.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
    minefield_ids: set[str] = set()
    for minefield_index, minefield in enumerate(minefields):
        minefield_id = _safe_text(minefield.get("minefield_id"))
        minefield_scope = f"{scope}.minefield[{minefield_index}]"
        if not minefield_id:
            blockers.append(
                _blocker(
                    "minefield_id_missing",
                    minefield_scope,
                    "Represented minefield is missing minefield_id.",
                    recoverable=True,
                )
            )
        elif minefield_id in minefield_ids:
            blockers.append(
                _blocker(
                    "duplicate_minefield_id",
                    minefield_scope,
                    "Scenario minefield IDs must be unique.",
                    recoverable=True,
                    details={"minefield_id": minefield_id},
                )
            )
        minefield_ids.add(minefield_id)
        if "blocking" not in minefield or not isinstance(
            minefield.get("blocking"), bool
        ):
            blockers.append(
                _blocker(
                    "minefield_blocking_policy_missing",
                    minefield_scope,
                    "Represented minefield must declare a boolean blocking policy.",
                    recoverable=True,
                    details={"minefield_id": minefield_id or None},
                )
            )
        trigger = minefield.get("trigger")
        if not isinstance(trigger, Mapping):
            blockers.append(
                _blocker(
                    "minefield_trigger_missing",
                    minefield_scope,
                    "Represented minefield must declare a matcher trigger.",
                    recoverable=True,
                    details={"minefield_id": minefield_id or None},
                )
            )
        else:
            blockers.extend(validate_matcher_spec(trigger, scope=minefield_scope))

    budgets = _mapping_sequence(raw.get("budgets", []))
    if not _is_sequence(raw.get("budgets", [])):
        blockers.append(
            _blocker(
                "scenario_budgets_invalid",
                scope,
                "Scenario budgets must be a list.",
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
    budget_ids: set[str] = set()
    for budget_index, budget in enumerate(budgets):
        budget_id = _safe_text(budget.get("budget_id"))
        if budget_id and budget_id in budget_ids:
            blockers.append(
                _blocker(
                    "duplicate_budget_id",
                    f"{scope}.budget[{budget_index}]",
                    "Scenario budget IDs must be unique.",
                    recoverable=True,
                    details={"budget_id": budget_id},
                )
            )
        budget_ids.add(budget_id)
        blockers.extend(
            validate_budget_spec(
                budget,
                scope=f"{scope}.budget[{budget_index}]",
            )
        )

    initial_state = _mapping(raw.get("initial_state"))
    reset_policy = _mapping(raw.get("reset_policy"))
    acceptable_goal_states = _mapping_sequence(raw.get("acceptable_goal_states", []))
    milestone_dag = _mapping_sequence(raw.get("milestone_dag", []))
    permitted_effects = _mapping_sequence(raw.get("permitted_effects", []))
    target_scope = _mapping(raw.get("target_scope"))
    security_scope = _mapping(raw.get("security_scope"))
    fault_injection = _mapping(raw.get("fault_injection"))
    metadata = _mapping(raw.get("metadata"))
    try:
        projected = json_serialisable_projection(raw)
    except TypeError as exc:
        blockers.append(
            _blocker(
                "scenario_not_json_serialisable",
                scope,
                str(exc),
                recoverable=True,
                details={"scenario_id": scenario_id or None},
            )
        )
        projected = {}
    if blockers:
        return None, blockers
    scenario_digest_payload = {
        "schema_version": schema_version,
        "scenario_id": scenario_id,
        "family_id": family_id,
        "depends_on": list(depends_on),
        "execution": execution,
        "evaluator_specs": evaluators,
        "checks": checks,
        "minefields": minefields,
        "budgets": budgets,
        "initial_state": initial_state,
        "reset_policy": reset_policy,
        "acceptable_goal_states": acceptable_goal_states,
        "milestone_dag": milestone_dag,
        "permitted_effects": permitted_effects,
        "target_scope": target_scope,
        "security_scope": security_scope,
        "fault_injection": fault_injection,
        "metadata": metadata,
        "represented_case": projected,
    }
    return (
        CertificationScenarioContract(
            scenario_id=scenario_id,
            family_id=family_id,
            depends_on=depends_on,
            execution=execution,
            evaluator_specs=tuple(evaluators),
            checks=tuple(checks),
            minefields=tuple(minefields),
            budgets=tuple(budgets),
            initial_state=initial_state,
            reset_policy=reset_policy,
            acceptable_goal_states=tuple(acceptable_goal_states),
            milestone_dag=tuple(milestone_dag),
            permitted_effects=tuple(permitted_effects),
            target_scope=target_scope,
            security_scope=security_scope,
            fault_injection=fault_injection,
            metadata=metadata,
            contract_sha256=stable_payload_digest(scenario_digest_payload),
        ),
        [],
    )


def _validate_policy(policy: Mapping[str, Any]) -> list[CertificationBlocker]:
    scope = "campaign_policy"
    blockers: list[CertificationBlocker] = []
    schema_version = _safe_text(policy.get("schema_version"))
    if schema_version != OPERATIONAL_CERTIFICATION_POLICY_SCHEMA_VERSION:
        blockers.append(
            _blocker(
                "campaign_policy_schema_version_invalid",
                scope,
                "Campaign policy has an unsupported schema_version.",
                recoverable=True,
                details={"schema_version": schema_version or None},
            )
        )
    trial_count = policy.get("trial_count")
    if (
        isinstance(trial_count, bool)
        or not isinstance(trial_count, int)
        or trial_count < 1
    ):
        blockers.append(
            _blocker(
                "campaign_trial_count_invalid",
                scope,
                "Campaign trial_count must be a positive integer.",
                recoverable=True,
            )
        )
        trial_count = 0
    raw_windows = policy.get("pass_windows")
    if not _is_sequence(raw_windows) or not raw_windows:
        blockers.append(
            _blocker(
                "campaign_pass_windows_missing",
                scope,
                "Campaign policy must declare pass_windows.",
                recoverable=True,
            )
        )
    else:
        windows: list[int] = []
        for value in raw_windows:
            if isinstance(value, bool) or not isinstance(value, int):
                blockers.append(
                    _blocker(
                        "campaign_pass_window_invalid",
                        scope,
                        "Each pass window must be an integer.",
                        recoverable=True,
                        details={"value": value},
                    )
                )
                continue
            windows.append(value)
            if value < 1 or (trial_count and value > trial_count):
                blockers.append(
                    _blocker(
                        "campaign_pass_window_out_of_range",
                        scope,
                        "Pass window must be between one and trial_count.",
                        recoverable=True,
                        details={"value": value, "trial_count": trial_count},
                    )
                )
        if len(set(windows)) != len(windows):
            blockers.append(
                _blocker(
                    "campaign_pass_windows_duplicate",
                    scope,
                    "Campaign pass_windows must be unique.",
                    recoverable=True,
                )
            )
    if policy.get("strict_represented_evaluator_results") is not True:
        blockers.append(
            _blocker(
                "strict_represented_evaluator_results_required",
                scope,
                "Operational certification requires represented evaluator results.",
                recoverable=True,
            )
        )
    raw_gates = policy.get("certification_gates")
    gates = _mapping_sequence(raw_gates)
    if not _is_sequence(raw_gates) or not gates:
        blockers.append(
            _blocker(
                "represented_certification_gates_missing",
                scope,
                "Campaign policy must provide represented certification gates.",
                recoverable=True,
            )
        )
    gate_ids: set[str] = set()
    for index, gate in enumerate(gates):
        gate_scope = f"{scope}.gate[{index}]"
        gate_id = _safe_text(gate.get("gate_id"))
        if not gate_id:
            blockers.append(
                _blocker(
                    "certification_gate_id_missing",
                    gate_scope,
                    "Represented certification gate is missing gate_id.",
                    recoverable=True,
                )
            )
        elif gate_id in gate_ids:
            blockers.append(
                _blocker(
                    "duplicate_certification_gate_id",
                    gate_scope,
                    "Certification gate IDs must be unique.",
                    recoverable=True,
                    details={"gate_id": gate_id},
                )
            )
        gate_ids.add(gate_id)
        matcher = gate.get("matcher")
        budget = gate.get("budget")
        if isinstance(matcher, Mapping) == isinstance(budget, Mapping):
            blockers.append(
                _blocker(
                    "certification_gate_check_invalid",
                    gate_scope,
                    "Certification gate must declare exactly one matcher or budget.",
                    recoverable=True,
                    details={"gate_id": gate_id or None},
                )
            )
        elif isinstance(matcher, Mapping):
            blockers.extend(validate_matcher_spec(matcher, scope=gate_scope))
        elif isinstance(budget, Mapping):
            blockers.extend(validate_budget_spec(budget, scope=gate_scope))
    try:
        json_serialisable_projection(policy)
    except TypeError as exc:
        blockers.append(
            _blocker(
                "campaign_policy_not_json_serialisable",
                scope,
                str(exc),
                recoverable=True,
            )
        )
    return blockers


def _topological_order(
    scenarios: Sequence[CertificationScenarioContract],
) -> tuple[tuple[str, ...], list[CertificationBlocker]]:
    scenario_ids = [scenario.scenario_id for scenario in scenarios]
    id_set = set(scenario_ids)
    blockers: list[CertificationBlocker] = []
    dependencies = {
        scenario.scenario_id: list(scenario.depends_on) for scenario in scenarios
    }
    for scenario in scenarios:
        for dependency in scenario.depends_on:
            if dependency not in id_set:
                blockers.append(
                    _blocker(
                        "scenario_dependency_missing",
                        f"scenario[{scenario.scenario_id}]",
                        "Scenario dependency does not exist in the selected case set.",
                        recoverable=True,
                        details={
                            "scenario_id": scenario.scenario_id,
                            "dependency_id": dependency,
                        },
                    )
                )
    if blockers:
        return (), blockers

    remaining = {
        scenario_id: set(dependencies[scenario_id]) for scenario_id in scenario_ids
    }
    order: list[str] = []
    while remaining:
        ready = sorted(
            scenario_id
            for scenario_id, dependency_ids in remaining.items()
            if not dependency_ids
        )
        if not ready:
            cycle = _find_dependency_cycle(dependencies)
            blockers.append(
                _blocker(
                    "scenario_dependency_cycle",
                    "scenario_graph",
                    "Scenario dependency graph contains a cycle.",
                    recoverable=True,
                    details={"cycle": cycle},
                )
            )
            return (), blockers
        for scenario_id in ready:
            order.append(scenario_id)
            remaining.pop(scenario_id)
        ready_set = set(ready)
        for dependency_ids in remaining.values():
            dependency_ids.difference_update(ready_set)
    return tuple(order), []


def _find_dependency_cycle(dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for dependency in sorted(dependencies.get(node, ())):
            if dependency not in dependencies:
                continue
            if state.get(dependency, 0) == 0:
                found = visit(dependency)
                if found:
                    return found
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(dependencies):
        if state.get(node, 0) == 0:
            found = visit(node)
            if found:
                return found
    return []


def parse_operational_certification_contract(
    suite_definition: Mapping[str, Any],
    *,
    case_set: str | None = None,
) -> OperationalCertificationContract:
    """Parse a represented benchmark suite into an executable support contract."""

    try:
        source_projection = json_serialisable_projection(suite_definition)
    except TypeError as exc:
        raise CertificationContractValidationError(
            [
                _blocker(
                    "suite_definition_not_json_serialisable",
                    "suite",
                    str(exc),
                    recoverable=True,
                )
            ]
        ) from exc

    suite_id = _safe_text(suite_definition.get("suite_id"))
    suite_concept_id = _safe_text(suite_definition.get("suite_concept_id")) or None
    selected_case_set = (
        _safe_text(case_set)
        or _safe_text(suite_definition.get("case_set"))
        or _safe_text(suite_definition.get("default_case_set"))
    )
    rubric = _mapping(suite_definition.get("rubric"))
    policy: dict[str, Any] = {}
    for key in _POLICY_KEYS:
        candidate = rubric.get(key)
        if isinstance(candidate, Mapping):
            policy = dict(candidate)
            break
    if not policy:
        for key in _POLICY_KEYS:
            candidate = suite_definition.get(key)
            if isinstance(candidate, Mapping):
                policy = dict(candidate)
                break

    raw_cases: Any = suite_definition.get("cases")
    case_sets = suite_definition.get("case_sets")
    if isinstance(case_sets, Mapping):
        raw_cases = case_sets.get(selected_case_set)
    blockers: list[CertificationBlocker] = []
    if not suite_id:
        blockers.append(
            _blocker(
                "certification_suite_id_missing",
                "suite",
                "Represented certification suite is missing suite_id.",
                recoverable=True,
            )
        )
    if not selected_case_set:
        blockers.append(
            _blocker(
                "certification_case_set_missing",
                "suite",
                "Represented certification suite is missing a selected case set.",
                recoverable=True,
            )
        )
    if not policy:
        blockers.append(
            _blocker(
                "operational_certification_policy_missing",
                "suite",
                "Represented certification policy is missing from the suite rubric.",
                recoverable=True,
            )
        )
    else:
        blockers.extend(_validate_policy(policy))
    if not _is_sequence(raw_cases) or not raw_cases:
        blockers.append(
            _blocker(
                "certification_scenarios_missing",
                "suite",
                "Selected certification case set has no scenarios.",
                recoverable=True,
                details={"case_set": selected_case_set or None},
            )
        )
        raw_cases = []

    scenarios: list[CertificationScenarioContract] = []
    scenario_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            blockers.append(
                _blocker(
                    "certification_scenario_invalid",
                    f"scenario[{index}]",
                    "Certification scenario must be an object.",
                    recoverable=True,
                )
            )
            continue
        scenario, scenario_blockers = _normalise_scenario(raw_case, index=index)
        blockers.extend(scenario_blockers)
        if scenario is None:
            continue
        if scenario.scenario_id in scenario_ids:
            blockers.append(
                _blocker(
                    "duplicate_scenario_id",
                    f"scenario[{index}]",
                    "Scenario IDs must be unique within a case set.",
                    recoverable=True,
                    details={"scenario_id": scenario.scenario_id},
                )
            )
            continue
        scenario_ids.add(scenario.scenario_id)
        scenarios.append(scenario)

    order, graph_blockers = _topological_order(scenarios)
    blockers.extend(graph_blockers)
    if blockers:
        raise CertificationContractValidationError(blockers)

    contract_basis = {
        "suite_id": suite_id,
        "suite_concept_id": suite_concept_id,
        "case_set": selected_case_set,
        "policy": policy,
        "scenario_contract_sha256s": {
            scenario.scenario_id: scenario.contract_sha256 for scenario in scenarios
        },
        "topological_scenario_ids": list(order),
    }
    return OperationalCertificationContract(
        suite_id=suite_id,
        suite_concept_id=suite_concept_id,
        case_set=selected_case_set,
        policy=policy,
        scenarios=tuple(scenarios),
        topological_scenario_ids=order,
        source=_safe_text(suite_definition.get("source")) or None,
        source_definition_sha256=stable_payload_digest(source_projection),
        contract_sha256=stable_payload_digest(contract_basis),
    )


def _normalise_external_blockers(
    value: Any,
    *,
    scope: str,
) -> list[CertificationBlocker]:
    if not _is_sequence(value):
        return []
    blockers: list[CertificationBlocker] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            blockers.append(
                _blocker(
                    "external_blocker_invalid",
                    scope,
                    "Observed typed blocker must be an object.",
                    recoverable=True,
                    details={"index": index},
                )
            )
            continue
        code = _safe_text(item.get("code"))
        if not code:
            blockers.append(
                _blocker(
                    "external_blocker_code_missing",
                    scope,
                    "Observed typed blocker is missing code.",
                    recoverable=True,
                    details={"index": index},
                )
            )
            continue
        blockers.append(
            _blocker(
                code,
                _safe_text(item.get("scope")) or scope,
                _safe_text(item.get("message")) or code,
                recoverable=bool(item.get("recoverable")),
                blocking=item.get("blocking") is not False,
                details=_mapping(item.get("details")),
            )
        )
    return blockers


def _evaluate_represented_evaluator(
    *,
    scenario: CertificationScenarioContract,
    evaluator_spec: Mapping[str, Any],
    result: Any,
    trial_index: int,
) -> tuple[dict[str, Any] | None, list[CertificationBlocker], bool, bool]:
    evaluator_id = _safe_text(evaluator_spec.get("evaluator_id"))
    scope = f"scenario[{scenario.scenario_id}].trial[{trial_index}].evaluator[{evaluator_id}]"
    if not isinstance(result, Mapping):
        return (
            None,
            [
                _blocker(
                    "represented_evaluator_result_missing",
                    scope,
                    "Required represented evaluator result is missing.",
                    recoverable=True,
                    details={"evaluator_id": evaluator_id},
                )
            ],
            False,
            False,
        )
    payload = dict(result)
    blockers: list[CertificationBlocker] = []
    expected_schema = _safe_text(evaluator_spec.get("result_schema_version"))
    schema_version = _safe_text(payload.get("schema_version"))
    if schema_version != expected_schema:
        blockers.append(
            _blocker(
                "represented_evaluator_result_schema_mismatch",
                scope,
                "Represented evaluator result schema does not match its contract.",
                recoverable=True,
                details={
                    "expected": expected_schema,
                    "observed": schema_version or None,
                },
            )
        )
    if _safe_text(payload.get("evaluator_id")) != evaluator_id:
        blockers.append(
            _blocker(
                "represented_evaluator_result_identity_mismatch",
                scope,
                "Represented evaluator result references a different evaluator.",
                recoverable=True,
                details={
                    "expected": evaluator_id,
                    "observed": _safe_text(payload.get("evaluator_id")) or None,
                },
            )
        )
    if _safe_text(payload.get("scenario_id")) != scenario.scenario_id:
        blockers.append(
            _blocker(
                "represented_evaluator_result_scenario_mismatch",
                scope,
                "Represented evaluator result references a different scenario.",
                recoverable=True,
                details={
                    "expected": scenario.scenario_id,
                    "observed": _safe_text(payload.get("scenario_id")) or None,
                },
            )
        )
    observed_trial_index = payload.get("trial_index")
    if observed_trial_index != trial_index:
        blockers.append(
            _blocker(
                "represented_evaluator_result_trial_mismatch",
                scope,
                "Represented evaluator result references a different trial.",
                recoverable=True,
                details={"expected": trial_index, "observed": observed_trial_index},
            )
        )
    verdict = _safe_text(payload.get("verdict"))
    allowed = _string_sequence(evaluator_spec.get("allowed_verdicts"))
    passing = _string_sequence(evaluator_spec.get("passing_verdicts"))
    if verdict not in allowed:
        blockers.append(
            _blocker(
                "represented_evaluator_verdict_invalid",
                scope,
                "Represented evaluator verdict is not authorised by its specification.",
                recoverable=True,
                details={"verdict": verdict or None, "allowed_verdicts": list(allowed)},
            )
        )
    evidence = payload.get("evidence")
    if evaluator_spec.get("evidence_required") is True and (
        not _is_sequence(evidence) or not evidence
    ):
        blockers.append(
            _blocker(
                "represented_evaluator_evidence_missing",
                scope,
                "Represented evaluator result lacks required evidence.",
                recoverable=True,
                details={"evaluator_id": evaluator_id},
            )
        )
    check_results: list[dict[str, Any]] = []
    for check in _mapping_sequence(evaluator_spec.get("checks", [])):
        check_results.append(evaluate_matcher(check, payload))
    evaluator_passed = (
        not blockers
        and verdict in passing
        and all(item["matched"] for item in check_results)
    )
    evaluator_valid = not blockers
    normalised = json_serialisable_projection(payload)
    normalised["verdict"] = verdict
    normalised["check_results"] = check_results
    normalised["represented_result_sha256"] = stable_payload_digest(payload)
    return normalised, blockers, evaluator_passed, evaluator_valid


def evaluate_scenario_trial(
    contract: OperationalCertificationContract,
    *,
    scenario_id: str,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one trial without inventing semantic success policy in Python."""

    scenario = contract.scenario_by_id.get(_safe_text(scenario_id))
    if scenario is None:
        raise CertificationContractValidationError(
            [
                _blocker(
                    "certification_scenario_not_found",
                    "trial",
                    "Requested scenario is not in the represented contract.",
                    recoverable=True,
                    details={"scenario_id": _safe_text(scenario_id) or None},
                )
            ]
        )
    trial_index = observation.get("trial_index")
    if (
        isinstance(trial_index, bool)
        or not isinstance(trial_index, int)
        or trial_index < 1
    ):
        raise CertificationContractValidationError(
            [
                _blocker(
                    "trial_index_invalid",
                    f"scenario[{scenario.scenario_id}]",
                    "Trial observation must contain a positive integer trial_index.",
                    recoverable=True,
                )
            ]
        )
    observed_scenario_id = _safe_text(observation.get("scenario_id"))
    if observed_scenario_id != scenario.scenario_id:
        raise CertificationContractValidationError(
            [
                _blocker(
                    "trial_observation_scenario_mismatch",
                    f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
                    "Trial observation references a different scenario.",
                    recoverable=True,
                    details={
                        "expected": scenario.scenario_id,
                        "observed": observed_scenario_id or None,
                    },
                )
            ]
        )
    policy_trial_count = contract.policy.get("trial_count")
    if isinstance(policy_trial_count, int) and trial_index > policy_trial_count:
        raise CertificationContractValidationError(
            [
                _blocker(
                    "trial_index_out_of_range",
                    f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
                    "Trial index exceeds represented campaign trial_count.",
                    recoverable=True,
                    details={"trial_count": policy_trial_count},
                )
            ]
        )
    try:
        observation_projection = json_serialisable_projection(observation)
    except TypeError as exc:
        raise CertificationContractValidationError(
            [
                _blocker(
                    "trial_observation_not_json_serialisable",
                    f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
                    str(exc),
                    recoverable=True,
                )
            ]
        ) from exc

    blockers = _normalise_external_blockers(
        observation.get("typed_blockers"),
        scope=f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
    )
    raw_evaluator_results = _mapping(observation.get("represented_evaluator_results"))
    evaluator_results: list[dict[str, Any]] = []
    evaluator_passes: list[bool] = []
    evaluator_validities: list[bool] = []
    for evaluator_spec in scenario.evaluator_specs:
        evaluator_id = _safe_text(evaluator_spec.get("evaluator_id"))
        normalised, evaluator_blockers, evaluator_passed, evaluator_valid = (
            _evaluate_represented_evaluator(
                scenario=scenario,
                evaluator_spec=evaluator_spec,
                result=raw_evaluator_results.get(evaluator_id),
                trial_index=trial_index,
            )
        )
        blockers.extend(evaluator_blockers)
        evaluator_passes.append(evaluator_passed)
        evaluator_validities.append(evaluator_valid)
        if normalised is not None:
            evaluator_results.append(normalised)

    check_results = [
        evaluate_matcher(check, observation_projection) for check in scenario.checks
    ]
    minefield_results: list[dict[str, Any]] = []
    for minefield in scenario.minefields:
        trigger = evaluate_matcher(
            _mapping(minefield.get("trigger")), observation_projection
        )
        triggered = bool(trigger["matched"])
        minefield_result = {
            "minefield_id": _safe_text(minefield.get("minefield_id")),
            "triggered": triggered,
            "blocking": bool(minefield.get("blocking")),
            "trigger_result": trigger,
        }
        minefield_result["result_sha256"] = stable_payload_digest(minefield_result)
        minefield_results.append(minefield_result)
        if bool(minefield.get("blocking")) and trigger.get("path_exists") is not True:
            blockers.append(
                _blocker(
                    "minefield_evidence_missing",
                    f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
                    "A blocking minefield cannot be cleared because its audit evidence is missing.",
                    recoverable=True,
                    blocking=True,
                    details={
                        "minefield_id": _safe_text(minefield.get("minefield_id")),
                        "matcher_path": trigger.get("path"),
                        "reason_code": trigger.get("reason_code"),
                    },
                )
            )
        if triggered:
            blockers.append(
                _blocker(
                    "minefield_triggered",
                    f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
                    "A represented certification minefield was triggered.",
                    recoverable=False,
                    blocking=bool(minefield.get("blocking")),
                    details={
                        "minefield_id": _safe_text(minefield.get("minefield_id")),
                    },
                )
            )

    budget_results: list[dict[str, Any]] = []
    for budget in scenario.budgets:
        budget_result = evaluate_budget(budget, observation_projection)
        budget_results.append(budget_result)
        if not budget_result["within_budget"]:
            reason_code = _safe_text(budget_result.get("reason_code"))
            blocker_code = (
                f"scenario_{reason_code}" if reason_code else "scenario_budget_exceeded"
            )
            blocker_message = (
                "A represented scenario budget could not be evaluated from the "
                "required measurement."
                if reason_code
                else "A represented scenario budget was exceeded."
            )
            blockers.append(
                _blocker(
                    blocker_code,
                    f"scenario[{scenario.scenario_id}].trial[{trial_index}]",
                    blocker_message,
                    recoverable=True,
                    blocking=bool(budget_result["blocking"]),
                    details={
                        "budget_id": budget_result["budget_id"],
                        "reason_code": reason_code or None,
                    },
                )
            )

    passed = (
        bool(evaluator_passes)
        and all(evaluator_passes)
        and all(result["matched"] for result in check_results)
        and not any(blocker.blocking for blocker in blockers)
    )
    operational_metrics = json_serialisable_projection(
        _mapping(observation.get("operational_metrics"))
    )
    result: dict[str, Any] = {
        "schema_version": OPERATIONAL_CERTIFICATION_TRIAL_RESULT_SCHEMA_VERSION,
        "suite_id": contract.suite_id,
        "case_set": contract.case_set,
        "contract_sha256": contract.contract_sha256,
        "scenario_id": scenario.scenario_id,
        "family_id": scenario.family_id,
        "scenario_contract_sha256": scenario.contract_sha256,
        "trial_index": trial_index,
        "passed": passed,
        "represented_evaluator_results": evaluator_results,
        "required_evaluator_count": len(scenario.evaluator_specs),
        "observed_evaluator_result_count": len(evaluator_results),
        "valid_evaluator_result_count": sum(evaluator_validities),
        "passing_evaluator_count": sum(evaluator_passes),
        "check_results": check_results,
        "minefield_results": minefield_results,
        "budget_results": budget_results,
        "operational_metrics": operational_metrics,
        "blockers": [blocker.to_dict() for blocker in blockers],
        "observation_sha256": stable_payload_digest(observation_projection),
    }
    result["result_sha256"] = stable_payload_digest(result)
    return result


def _normalise_trial_results_by_scenario(
    trial_results: (
        Mapping[str, Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]
    ),
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if isinstance(trial_results, Mapping):
        for scenario_id, values in trial_results.items():
            if not _is_sequence(values):
                continue
            grouped[str(scenario_id)] = [
                dict(item) for item in values if isinstance(item, Mapping)
            ]
        return grouped
    if _is_sequence(trial_results):
        for item in trial_results:
            if not isinstance(item, Mapping):
                continue
            scenario_id = _safe_text(item.get("scenario_id"))
            if scenario_id:
                grouped.setdefault(scenario_id, []).append(dict(item))
    return grouped


def _trial_sort_index(item: Mapping[str, Any]) -> int:
    value = item.get("trial_index")
    return value if isinstance(value, int) and not isinstance(value, bool) else 10_000


def _validate_trial_result_integrity(
    *,
    contract: OperationalCertificationContract,
    scenario: CertificationScenarioContract,
    trial: Mapping[str, Any],
) -> list[CertificationBlocker]:
    trial_index = trial.get("trial_index")
    scope = f"scenario[{scenario.scenario_id}].trial[{trial_index}]"
    blockers: list[CertificationBlocker] = []
    expected_identity = {
        "schema_version": OPERATIONAL_CERTIFICATION_TRIAL_RESULT_SCHEMA_VERSION,
        "suite_id": contract.suite_id,
        "case_set": contract.case_set,
        "contract_sha256": contract.contract_sha256,
        "scenario_id": scenario.scenario_id,
        "family_id": scenario.family_id,
        "scenario_contract_sha256": scenario.contract_sha256,
    }
    for field_name, expected in expected_identity.items():
        observed = trial.get(field_name)
        if observed != expected:
            blockers.append(
                _blocker(
                    "trial_result_identity_mismatch",
                    scope,
                    "Trial result does not belong to this represented contract.",
                    recoverable=True,
                    details={
                        "field": field_name,
                        "expected": expected,
                        "observed": observed,
                    },
                )
            )
    if not isinstance(trial.get("passed"), bool):
        blockers.append(
            _blocker(
                "trial_result_passed_invalid",
                scope,
                "Trial result passed field must be boolean.",
                recoverable=True,
            )
        )
    required_evaluator_count = trial.get("required_evaluator_count")
    if required_evaluator_count != len(scenario.evaluator_specs):
        blockers.append(
            _blocker(
                "trial_result_evaluator_requirement_mismatch",
                scope,
                "Trial result evaluator requirement differs from the scenario contract.",
                recoverable=True,
                details={
                    "expected": len(scenario.evaluator_specs),
                    "observed": required_evaluator_count,
                },
            )
        )
    valid_evaluator_count = trial.get("valid_evaluator_result_count")
    if (
        isinstance(valid_evaluator_count, bool)
        or not isinstance(valid_evaluator_count, int)
        or valid_evaluator_count < 0
        or valid_evaluator_count > len(scenario.evaluator_specs)
    ):
        blockers.append(
            _blocker(
                "trial_result_valid_evaluator_count_invalid",
                scope,
                "Trial result has an invalid valid-evaluator count.",
                recoverable=True,
                details={"observed": valid_evaluator_count},
            )
        )
    claimed_digest = _safe_text(trial.get("result_sha256"))
    digest_basis = dict(trial)
    digest_basis.pop("result_sha256", None)
    try:
        observed_digest = stable_payload_digest(digest_basis)
    except TypeError as exc:
        blockers.append(
            _blocker(
                "trial_result_not_json_serialisable",
                scope,
                str(exc),
                recoverable=True,
            )
        )
    else:
        if not claimed_digest or claimed_digest != observed_digest:
            blockers.append(
                _blocker(
                    "trial_result_digest_mismatch",
                    scope,
                    "Trial result digest is missing or does not match its evidence.",
                    recoverable=True,
                    details={
                        "claimed": claimed_digest or None,
                        "observed": observed_digest,
                    },
                )
            )
    return blockers


def aggregate_five_trial_campaign(
    contract: OperationalCertificationContract,
    trial_results: (
        Mapping[str, Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]
    ),
    *,
    represented_campaign_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate five trials into pass^1, pass^3, and pass^5 evidence.

    Which aggregate values constitute release acceptance is still decided by
    the represented ``certification_gates`` in the suite policy.
    """

    trial_count = contract.policy.get("trial_count")
    pass_windows = tuple(contract.policy.get("pass_windows") or ())
    required_windows = (1, 3, 5)
    blockers: list[CertificationBlocker] = []
    if trial_count != 5 or not set(required_windows).issubset(set(pass_windows)):
        blockers.append(
            _blocker(
                "five_trial_campaign_policy_missing",
                "campaign",
                "Five-trial aggregation requires trial_count=5 and pass windows 1, 3, 5.",
                recoverable=True,
                details={
                    "trial_count": trial_count,
                    "pass_windows": list(pass_windows),
                },
            )
        )

    try:
        campaign_evidence_projection = json_serialisable_projection(
            represented_campaign_evidence or {}
        )
    except TypeError as exc:
        campaign_evidence_projection = {}
        blockers.append(
            _blocker(
                "represented_campaign_evidence_not_json_serialisable",
                "campaign",
                str(exc),
                recoverable=True,
            )
        )

    grouped = _normalise_trial_results_by_scenario(trial_results)
    scenario_aggregates: dict[str, dict[str, Any]] = {}
    family_scenario_ids: dict[str, list[str]] = {}
    represented_evaluator_result_count = 0
    required_evaluator_result_count = 0
    minefield_trigger_count = 0
    blocking_minefield_trigger_count = 0
    budget_violation_count = 0
    blocking_budget_violation_count = 0
    budget_threshold_exceedance_count = 0
    blocking_budget_threshold_exceedance_count = 0
    budget_measurement_failure_count = 0
    blocking_budget_measurement_failure_count = 0
    duration_ms_values: list[float] = []
    model_cost_values: list[float] = []
    tool_cost_values: list[float] = []
    timeout_count = 0
    clarification_count = 0
    correction_count = 0
    false_success_count = 0
    namespace_violation_count = 0

    known_scenario_ids = set(contract.scenario_by_id)
    for unexpected_scenario_id in sorted(set(grouped) - known_scenario_ids):
        blockers.append(
            _blocker(
                "unexpected_scenario_trial_results",
                f"scenario[{unexpected_scenario_id}]",
                "Campaign includes trial results outside the represented case set.",
                recoverable=True,
                details={"scenario_id": unexpected_scenario_id},
            )
        )

    for scenario in contract.scenarios:
        required_evaluator_result_count += len(scenario.evaluator_specs) * 5
        family_scenario_ids.setdefault(scenario.family_id, []).append(
            scenario.scenario_id
        )
        scenario_trials = sorted(
            grouped.get(scenario.scenario_id, []),
            key=_trial_sort_index,
        )
        indices = [item.get("trial_index") for item in scenario_trials]
        expected_indices = list(range(1, 6))
        complete = len(scenario_trials) == 5 and indices == expected_indices
        if not complete:
            blockers.append(
                _blocker(
                    "scenario_trials_incomplete",
                    f"scenario[{scenario.scenario_id}]",
                    "Scenario does not contain exactly one result for each of five trials.",
                    recoverable=True,
                    details={
                        "expected_indices": expected_indices,
                        "observed_indices": indices,
                    },
                )
            )
        integrity_blockers_by_trial = [
            _validate_trial_result_integrity(
                contract=contract,
                scenario=scenario,
                trial=trial,
            )
            for trial in scenario_trials
        ]
        for trial_integrity_blockers in integrity_blockers_by_trial:
            blockers.extend(trial_integrity_blockers)
        passes = [
            item.get("passed") is True and not trial_integrity_blockers
            for item, trial_integrity_blockers in zip(
                scenario_trials,
                integrity_blockers_by_trial,
                strict=True,
            )
        ]
        window_results = {
            f"pass^{window}": complete and all(passes[:window])
            for window in required_windows
        }
        trial_blocker_count = 0
        for trial in scenario_trials:
            metrics = _mapping(trial.get("operational_metrics"))
            if (duration_ms := _finite_number(metrics.get("duration_ms"))) is not None:
                duration_ms_values.append(duration_ms)
            if (
                model_cost := _finite_number(metrics.get("model_cost_units"))
            ) is not None:
                model_cost_values.append(model_cost)
            if (
                tool_cost := _finite_number(metrics.get("tool_cost_units"))
            ) is not None:
                tool_cost_values.append(tool_cost)
            timeout_count += int(metrics.get("timeout") is True)
            for metric_key, target_name in (
                ("clarification_count", "clarification_count"),
                ("correction_count", "correction_count"),
                ("false_success_count", "false_success_count"),
                ("namespace_violation_count", "namespace_violation_count"),
            ):
                value = metrics.get(metric_key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    if target_name == "clarification_count":
                        clarification_count += value
                    elif target_name == "correction_count":
                        correction_count += value
                    elif target_name == "false_success_count":
                        false_success_count += value
                    else:
                        namespace_violation_count += value
            valid_evaluator_count = trial.get("valid_evaluator_result_count")
            if (
                isinstance(valid_evaluator_count, int)
                and not isinstance(valid_evaluator_count, bool)
                and 0 <= valid_evaluator_count <= len(scenario.evaluator_specs)
            ):
                represented_evaluator_result_count += valid_evaluator_count
            for minefield in _mapping_sequence(trial.get("minefield_results", [])):
                if minefield.get("triggered") is True:
                    minefield_trigger_count += 1
                    if minefield.get("blocking") is True:
                        blocking_minefield_trigger_count += 1
            for budget in _mapping_sequence(trial.get("budget_results", [])):
                if budget.get("within_budget") is not True:
                    budget_violation_count += 1
                    if budget.get("blocking") is True:
                        blocking_budget_violation_count += 1
                    if _safe_text(budget.get("reason_code")):
                        budget_measurement_failure_count += 1
                        if budget.get("blocking") is True:
                            blocking_budget_measurement_failure_count += 1
                    else:
                        budget_threshold_exceedance_count += 1
                        if budget.get("blocking") is True:
                            blocking_budget_threshold_exceedance_count += 1
            for raw_blocker in _mapping_sequence(trial.get("blockers", [])):
                if raw_blocker.get("blocking") is True:
                    trial_blocker_count += 1
                    blockers.append(
                        _blocker(
                            _safe_text(raw_blocker.get("code"))
                            or "trial_blocker_unspecified",
                            _safe_text(raw_blocker.get("scope"))
                            or f"scenario[{scenario.scenario_id}]",
                            _safe_text(raw_blocker.get("message"))
                            or "Trial reported a blocking condition.",
                            recoverable=bool(raw_blocker.get("recoverable")),
                            blocking=True,
                            details=_mapping(raw_blocker.get("details")),
                        )
                    )
        scenario_aggregates[scenario.scenario_id] = {
            "scenario_id": scenario.scenario_id,
            "family_id": scenario.family_id,
            "depends_on": list(scenario.depends_on),
            "scenario_contract_sha256": scenario.contract_sha256,
            "complete": complete,
            "trial_count": len(scenario_trials),
            "successful_trial_count": sum(passes),
            "trial_passes": passes,
            "pass_windows": window_results,
            "blocking_trial_blocker_count": trial_blocker_count,
            "trial_result_sha256s": [
                _safe_text(item.get("result_sha256")) or stable_payload_digest(item)
                for item in scenario_trials
            ],
        }

    scenario_count = len(contract.scenarios)
    passed_scenario_ids_by_window: dict[str, list[str]] = {}
    pass_rates: dict[str, float] = {}
    for window in required_windows:
        key = f"pass^{window}"
        passed_ids = [
            scenario_id
            for scenario_id, aggregate in scenario_aggregates.items()
            if aggregate["pass_windows"][key]
        ]
        passed_scenario_ids_by_window[key] = passed_ids
        pass_rates[key] = len(passed_ids) / scenario_count if scenario_count else 0.0

    family_aggregates: dict[str, dict[str, Any]] = {}
    for family_id, scenario_ids in family_scenario_ids.items():
        family_passed: dict[str, list[str]] = {}
        family_rates: dict[str, float] = {}
        for window in required_windows:
            key = f"pass^{window}"
            passed_ids = [
                scenario_id
                for scenario_id in scenario_ids
                if scenario_aggregates[scenario_id]["pass_windows"][key]
            ]
            family_passed[key] = passed_ids
            family_rates[key] = len(passed_ids) / len(scenario_ids)
        family_aggregates[family_id] = {
            "family_id": family_id,
            "scenario_ids": scenario_ids,
            "scenario_count": len(scenario_ids),
            "passed_scenario_ids_by_window": family_passed,
            "pass_rates": family_rates,
        }

    report: dict[str, Any] = {
        "schema_version": OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION,
        "suite_id": contract.suite_id,
        "suite_concept_id": contract.suite_concept_id,
        "case_set": contract.case_set,
        "suite_source": contract.source,
        "contract_sha256": contract.contract_sha256,
        "source_definition_sha256": contract.source_definition_sha256,
        "trial_count": 5,
        "pass_windows": [1, 3, 5],
        "topological_scenario_ids": list(contract.topological_scenario_ids),
        "scenario_count": scenario_count,
        "scenarios": scenario_aggregates,
        "families": family_aggregates,
        "passed_scenario_ids_by_window": passed_scenario_ids_by_window,
        "pass_rates": pass_rates,
        "represented_evaluator_result_count": represented_evaluator_result_count,
        "required_evaluator_result_count": required_evaluator_result_count,
        "represented_evaluator_coverage_rate": (
            represented_evaluator_result_count / required_evaluator_result_count
            if required_evaluator_result_count
            else 0.0
        ),
        "minefield_trigger_count": minefield_trigger_count,
        "blocking_minefield_trigger_count": blocking_minefield_trigger_count,
        "budget_violation_count": budget_violation_count,
        "blocking_budget_violation_count": blocking_budget_violation_count,
        "budget_threshold_exceedance_count": budget_threshold_exceedance_count,
        "blocking_budget_threshold_exceedance_count": (
            blocking_budget_threshold_exceedance_count
        ),
        "budget_measurement_failure_count": budget_measurement_failure_count,
        "blocking_budget_measurement_failure_count": (
            blocking_budget_measurement_failure_count
        ),
        "represented_campaign_evidence": campaign_evidence_projection,
        "represented_campaign_evidence_sha256": stable_payload_digest(
            campaign_evidence_projection
        ),
        "operational_metrics": {
            "latency_ms": {
                "observation_count": len(duration_ms_values),
                "p50": _percentile(duration_ms_values, 0.50),
                "p95": _percentile(duration_ms_values, 0.95),
                "timeout_count": timeout_count,
                "timeout_rate": (
                    timeout_count / len(duration_ms_values)
                    if duration_ms_values
                    else None
                ),
            },
            "model_cost_units": {
                "observation_count": len(model_cost_values),
                "total": sum(model_cost_values) if model_cost_values else None,
            },
            "tool_cost_units": {
                "observation_count": len(tool_cost_values),
                "total": sum(tool_cost_values) if tool_cost_values else None,
            },
            "user_burden": {
                "clarification_count": clarification_count,
                "correction_count": correction_count,
            },
            "false_success_count": false_success_count,
            "namespace_violation_count": namespace_violation_count,
        },
    }

    gate_results: list[dict[str, Any]] = []
    for gate in _mapping_sequence(contract.policy.get("certification_gates", [])):
        gate_id = _safe_text(gate.get("gate_id"))
        if isinstance(gate.get("matcher"), Mapping):
            check = evaluate_matcher(_mapping(gate.get("matcher")), report)
            passed = bool(check["matched"])
            gate_result = {
                "gate_id": gate_id,
                "kind": "matcher",
                "passed": passed,
                "check": check,
            }
        else:
            check = evaluate_budget(_mapping(gate.get("budget")), report)
            passed = bool(check["within_budget"])
            gate_result = {
                "gate_id": gate_id,
                "kind": "budget",
                "passed": passed,
                "check": check,
            }
        gate_result["result_sha256"] = stable_payload_digest(gate_result)
        gate_results.append(gate_result)

    report["certification_gate_results"] = gate_results
    report["failed_certification_gate_ids"] = [
        result["gate_id"] for result in gate_results if not result["passed"]
    ]
    report["blockers"] = [blocker.to_dict() for blocker in blockers]
    report["certified"] = (
        bool(gate_results)
        and all(result["passed"] for result in gate_results)
        and not any(blocker.blocking for blocker in blockers)
    )
    report["report_sha256"] = stable_payload_digest(report)
    return report


__all__ = [
    "OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION",
    "OPERATIONAL_CERTIFICATION_CONTRACT_PROJECTION_SCHEMA_VERSION",
    "OPERATIONAL_CERTIFICATION_POLICY_SCHEMA_VERSION",
    "OPERATIONAL_CERTIFICATION_SCENARIO_SCHEMA_VERSION",
    "OPERATIONAL_CERTIFICATION_TRIAL_RESULT_SCHEMA_VERSION",
    "REPRESENTED_OPERATIONAL_EVALUATOR_RESULT_SCHEMA_VERSION",
    "CertificationBlocker",
    "CertificationContractValidationError",
    "CertificationScenarioContract",
    "OperationalCertificationContract",
    "aggregate_five_trial_campaign",
    "evaluate_budget",
    "evaluate_matcher",
    "evaluate_scenario_trial",
    "json_serialisable_projection",
    "parse_operational_certification_contract",
    "stable_payload_digest",
    "validate_operational_certification_campaign_result_integrity",
    "validate_budget_spec",
    "validate_matcher_spec",
]
